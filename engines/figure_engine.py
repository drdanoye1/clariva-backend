"""
Engine 29 — AI Figure Generation & Technical Illustration System.

AI Figure Generation & Technical Illustration Development Specification
(docs/2_Upgrades_Clariva_AI_Figure_Specification_V01.docx). Phase 9 built
the data model (models.db_models.ProposalFigureSet/ProposalFigure); Phase
10 added §3's AI Visual Communication Plan and §4-§8's Figure 1 functional/
process diagram (content extraction into structured nodes, a
deterministically-rendered image, a coordinated caption per §25). Phase 11
added §9-§16's Figure 2 — the technical/physical illustration — and
§10/§11's mandatory functional-to-physical traceability back to Figure 1.
Phase 12 adds §24's Automated Cross-Figure QA (an AI pass checking each
figure and the Figure 1 ↔ Figure 2 relationship against the checklists
that section defines), human annotation edits (§20/§22's "Edit
Illustration Brief" / caption-and-callout editing actions), and the
approval-status state machine §22 requires before an AI-generated figure
can become final ("Use" / reject / regenerate-request).

Design notes (same discipline as the other Phase-numbered engines):
- Every method takes an already-open AsyncSession and only flushes, never
  commits — the caller's request-scoped session commits once (see
  routers/figures.py).
- Local `_ai_error()`/`_parse_json_response()` copies rather than a shared
  import — this codebase's established per-module convention (see
  engines/scope_of_work_engine.py's identical helpers).
- Figure 1's image is rendered deterministically (utils/figure_renderer.py,
  matplotlib box-and-arrow diagram), not AI-image-generated — see
  figure_renderer.py's module docstring for the full reasoning. Figure 2's
  panel images ARE genuinely AI-generated (DALL-E) — this is where an
  image-generation call actually earns its cost, since §12 asks for a
  photorealistic/illustrative engineering visualization of physical
  hardware, not a diagram. What's still deterministic in Figure 2: laying
  the two generated panel images out side by side with labels
  (utils/figure_renderer.py::compose_two_panel_figure) and the numbered
  callout legend (structured data, not burned into image pixels — see
  that function's docstring for why).
- Regenerating a figure replaces the existing row for that figure_number
  in place (not a new row) and resets approval_status back to "pending" —
  a regenerated figure is unreviewed content again, even if a user had
  previously approved the old version (Phase 12 enforces this state
  machine; this phase just resets the flag honestly rather than leaving a
  stale "approved" status on new content).
- A rendered/generated image is best-effort: if rendering, an image-gen
  call, or the R2 upload fails, the figure's structured data (nodes/
  callouts/caption/etc.) is still saved and `stored_file_id` simply stays
  null — "degrade gracefully" rather than losing the whole AI extraction
  because of a downstream image error.
- Figure 2 requires Figure 1 to already exist in the same figure set —
  §10's Figure Relationship Map and §11's traceability are meaningless
  without a Figure 1 to trace back to. Enforced as a 400 before any
  charge, same "cheap checks before spending credits" ordering Phase 10
  established for the missing-section-content check.
- Cross-figure QA (§24) never auto-regenerates a figure — Clariva's own
  governing principle (§22) is that no AI-generated illustration becomes
  final without human review, so a QA failure sets `approval_status` to
  "needs_regeneration" (flagging it for a human decision) rather than
  looping back into another AI call on its own. The status flip only
  happens when the figure is still "pending" — QA never overwrites a
  human's own "approved"/"rejected" decision.
- Annotation edits (caption/alt_text/concept_disclosure/callouts) are
  free, unmetered human actions, not AI calls — no `catalog_engine`
  charge applies to them, same as this codebase's other human-edit
  endpoints (e.g. collaboration.py's comment/task CRUD).
"""
from __future__ import annotations

import base64
import json
import logging
import re
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import openai
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import storage
from config import settings
from engines import usage_tracking
from engines.usage_tracking import usage_from_response
from models.db_models import (
    Proposal, ProposalFigure, ProposalFigureSet, ProposalSection, StoredFile, new_uuid,
)
from utils.figure_renderer import compose_two_panel_figure, render_functional_diagram_png

_log = logging.getLogger(__name__)

_VALID_CLASSIFICATIONS = {"confirmed", "inferred", "conceptual"}
_VALID_LAYOUTS = {"horizontal", "vertical", "hierarchical", "circular_feedback"}
_VALID_VIEW_TYPES = {"perspective", "sectional", "perspective_sectional", "exploded", "architecture"}
_TWO_PANEL_VIEW_TYPES = {"perspective_sectional"}  # the only view_type that produces 2 images (§14)
_VALID_VISUAL_STYLES_F2 = {"photorealistic", "black_white", "line_diagram"}
_DEFAULT_CONCEPT_DISCLOSURE = (
    "Conceptual illustration; final configuration will be determined during "
    "design and prototype development."
)
_IMAGE_GEN_MODEL = "dall-e-3"
_IMAGE_GEN_SIZE = "1024x1024"
_VALID_APPROVAL_STATUSES = {"pending", "approved", "rejected", "needs_regeneration"}


# ── Local helpers (per-module convention — see scope_of_work_engine.py) ─────

def _ai_error(exc: Exception) -> HTTPException:
    if isinstance(exc, openai.RateLimitError):
        _log.error("AI service rate limit exceeded: %s", exc)
        return HTTPException(status_code=503, detail="The AI figure-generation service is temporarily unavailable due to high demand. Please try again in a few minutes.")
    if isinstance(exc, openai.AuthenticationError):
        _log.critical("AI service authentication failure: %s", exc)
        return HTTPException(status_code=503, detail="The AI figure-generation service is not configured correctly. Please contact support.")
    if isinstance(exc, openai.APIConnectionError):
        _log.error("AI service connection error: %s", exc)
        return HTTPException(status_code=503, detail="The AI figure-generation service is unreachable. Please try again in a moment.")
    if isinstance(exc, openai.APIStatusError):
        _log.error("AI service API error %s: %s", getattr(exc, "status_code", "?"), getattr(exc, "message", str(exc)))
        return HTTPException(status_code=503, detail="The AI figure-generation service returned an unexpected error. Please try again.")
    _log.error("Unexpected figure-generation error:\n%s", traceback.format_exc())
    return HTTPException(status_code=500, detail="Figure generation failed. Please try again or contact support.")


def _parse_json_response(raw: str) -> Dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}") + 1
        try:
            return json.loads(cleaned[start:end])
        except Exception:
            raise HTTPException(status_code=500, detail="Could not parse the AI-generated figure content.")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return slug or "node"


def _normalize_nodes(raw_nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """§5 content extraction -> FigureNode shape, tolerant of a slightly
    malformed AI response (missing id/order/bad classification value) —
    degrade gracefully rather than 500 on an otherwise-usable response."""
    normalized: List[Dict[str, Any]] = []
    for i, n in enumerate(raw_nodes or []):
        if not isinstance(n, dict):
            continue
        label = str(n.get("label") or "").strip()
        if not label:
            continue
        node_id = str(n.get("id") or "").strip() or _slugify(label)
        order = n.get("order")
        order = int(order) if isinstance(order, (int, float)) and not isinstance(order, bool) else i + 1
        classification = str(n.get("classification") or "confirmed").strip().lower()
        if classification not in _VALID_CLASSIFICATIONS:
            classification = "confirmed"
        normalized.append({"id": node_id, "label": label, "order": order, "classification": classification})
    normalized.sort(key=lambda n: n["order"])
    return normalized


def _normalize_callouts(raw_callouts: List[Dict[str, Any]], figure_1_node_ids: set) -> List[Dict[str, Any]]:
    """§10/§11 traceability — Figure 2's numbered legend, each optionally
    tied back to a Figure 1 node id. A `relates_to_node` the AI invented
    (not a real Figure 1 node id) is dropped to null rather than kept as a
    dangling reference — §11 explicitly allows a callout with no clean
    Figure 1 mapping (e.g. a purely structural/support component), so this
    isn't a data-loss concern, just refusing to fabricate a trace link."""
    normalized: List[Dict[str, Any]] = []
    for i, c in enumerate(raw_callouts or []):
        if not isinstance(c, dict):
            continue
        label = str(c.get("label") or "").strip()
        if not label:
            continue
        number = c.get("number")
        number = int(number) if isinstance(number, (int, float)) and not isinstance(number, bool) else i + 1
        relates_to_node = c.get("relates_to_node")
        if relates_to_node not in figure_1_node_ids:
            relates_to_node = None
        classification = str(c.get("classification") or "confirmed").strip().lower()
        if classification not in _VALID_CLASSIFICATIONS:
            classification = "confirmed"
        normalized.append({
            "number": number, "label": label, "relates_to_node": relates_to_node, "classification": classification,
        })
    normalized.sort(key=lambda c: c["number"])
    return normalized


def _normalize_qa_checks(raw_checks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """§24 — tolerant of a slightly malformed AI response, same discipline
    as _normalize_nodes/_normalize_callouts above: a check with no
    criterion text is dropped rather than surfaced as a blank row, and
    `passed` defaults to True (missing != failing) rather than raising."""
    normalized: List[Dict[str, Any]] = []
    for c in (raw_checks or []):
        if not isinstance(c, dict):
            continue
        criterion = str(c.get("criterion") or "").strip()
        if not criterion:
            continue
        notes = str(c.get("notes") or "").strip() or None
        normalized.append({"criterion": criterion, "passed": bool(c.get("passed", True)), "notes": notes})
    return normalized


class FigureEngine:
    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    # ── Lookups ──────────────────────────────────────────────────────────

    async def _get_section_or_400(self, db: AsyncSession, proposal_id: str, source_section: str) -> ProposalSection:
        result = await db.execute(
            select(ProposalSection).where(
                ProposalSection.proposal_id == proposal_id,
                ProposalSection.section_id == source_section,
            )
        )
        section = result.scalar_one_or_none()
        if not section or not (section.content or "").strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Section '{source_section}' doesn't have generated content yet — "
                    "generate this proposal section before requesting figures for it."
                ),
            )
        return section

    async def assert_section_ready(self, db: AsyncSession, proposal_id: str, source_section: str) -> None:
        """Public wrapper around _get_section_or_400 so routers/figures.py
        can run this cheap check BEFORE calling catalog_engine.consume() —
        same "cheap checks before spending credits" ordering
        generate_supporting_document/routers/documents_library.py already
        establishes for doc_type validation. Without this, a request for a
        section that hasn't been generated yet would charge the user and
        then immediately 400."""
        await self._get_section_or_400(db, proposal_id, source_section)

    async def _get_figure(self, db: AsyncSession, figure_set_id: str, figure_number: int) -> Optional[ProposalFigure]:
        result = await db.execute(
            select(ProposalFigure).where(
                ProposalFigure.figure_set_id == figure_set_id, ProposalFigure.figure_number == figure_number,
            )
        )
        return result.scalar_one_or_none()

    async def assert_figure_1_ready(self, db: AsyncSession, figure_set_id: str) -> ProposalFigure:
        """§10/§11 — Figure 2 must trace back to a real Figure 1; there is
        nothing to trace to otherwise. Public so routers/figures.py can run
        this cheap check before catalog_engine.consume(), same ordering as
        assert_section_ready() above."""
        figure_1 = await self._get_figure(db, figure_set_id, 1)
        if not figure_1:
            raise HTTPException(
                status_code=400,
                detail="Figure 1 must be generated for this figure set before Figure 2 can be generated — Figure 2 traces back to Figure 1's functional stages (spec §10/§11).",
            )
        return figure_1

    async def get_figure_or_404(self, db: AsyncSession, figure_set_id: str, figure_number: int) -> ProposalFigure:
        """Public 404 wrapper around _get_figure, for the Phase 12
        annotation/approval endpoints below — those act on one specific
        already-generated figure, unlike figure-1/figure-2 which
        find-or-create."""
        figure = await self._get_figure(db, figure_set_id, figure_number)
        if not figure:
            raise HTTPException(status_code=404, detail=f"Figure {figure_number} not found in this figure set.")
        return figure

    async def get_or_create_figure_set(
        self, db: AsyncSession, proposal_id: str, source_section: str, user_id: Optional[str] = None,
    ) -> ProposalFigureSet:
        """One figureSet per (proposal, source_section) — spec §23's own
        example id ("technical_approach_visual_set") is section-scoped.
        Eager-loads `figures` on the existing-row branch (same
        MissingGreenlet reasoning as list_figure_sets()/get_figure_set_
        or_404() above) — callers (e.g. plan_visual_communication(),
        whose result flows straight back out through
        response_model=ProposalFigureSetOut) must never receive an
        object whose `figures` collection isn't already loaded. The
        newly-created branch below needs no such eager-load: a
        just-constructed ORM object's collection relationships are
        already tracked as loaded-empty by SQLAlchemy's unit of work,
        with no SELECT required to read them."""
        result = await db.execute(
            select(ProposalFigureSet)
            .options(selectinload(ProposalFigureSet.figures))
            .where(
                ProposalFigureSet.proposal_id == proposal_id,
                ProposalFigureSet.source_section == source_section,
            )
        )
        fset = result.scalar_one_or_none()
        if fset:
            return fset
        fset = ProposalFigureSet(
            id=new_uuid(), proposal_id=proposal_id, source_section=source_section,
            status="planning", created_by_user_id=user_id,
        )
        db.add(fset)
        await db.flush()
        return fset

    async def list_figure_sets(self, db: AsyncSession, proposal_id: str) -> List[ProposalFigureSet]:
        # selectinload(figures) — router's response_model is
        # ProposalFigureSetOut, whose `figures` field reads the ORM
        # relationship during Pydantic serialization, which happens after
        # this method returns and outside any single `await`ed call. The
        # relationship's default lazy="select" load would otherwise attempt
        # a synchronous SELECT at that point with no active async greenlet
        # context, raising sqlalchemy.exc.MissingGreenlet — eager-loading it
        # here, inside an awaited query, avoids that entirely.
        result = await db.execute(
            select(ProposalFigureSet)
            .options(selectinload(ProposalFigureSet.figures))
            .where(ProposalFigureSet.proposal_id == proposal_id)
            .order_by(ProposalFigureSet.created_at)
        )
        return list(result.scalars().all())

    async def get_approved_figures_by_section(
        self, db: AsyncSession, proposal_id: str,
    ) -> Dict[str, List[ProposalFigure]]:
        """Export-time consumer (engines/document_output.py) — the ONLY
        approved (human-reviewed, §22) figures for a proposal, grouped by
        the section they belong to (ProposalFigureSet.source_section,
        which is ProposalSection.section_id — see get_or_create_figure_
        set()'s docstring). A figure that's still "pending", "rejected",
        or "needs_regeneration" is deliberately excluded: §22's governing
        principle is that no AI-generated illustration becomes part of
        the final document without a human approving it first, and
        export is exactly the "becomes part of the final document"
        moment that principle protects. Figures within a section are
        ordered by figure_number so Figure 1 always renders before
        Figure 2."""
        result = await db.execute(
            select(ProposalFigureSet)
            .options(selectinload(ProposalFigureSet.figures))
            .where(ProposalFigureSet.proposal_id == proposal_id)
        )
        by_section: Dict[str, List[ProposalFigure]] = {}
        for fset in result.scalars().all():
            approved = sorted(
                (f for f in fset.figures if f.approval_status == "approved"),
                key=lambda f: f.figure_number,
            )
            if approved:
                by_section[fset.source_section] = approved
        return by_section

    async def get_figure_set_or_404(self, db: AsyncSession, figure_set_id: str, proposal_id: str) -> ProposalFigureSet:
        # Same MissingGreenlet reasoning as list_figure_sets() above — this
        # is also returned through response_model=ProposalFigureSetOut.
        result = await db.execute(
            select(ProposalFigureSet)
            .options(selectinload(ProposalFigureSet.figures))
            .where(
                ProposalFigureSet.id == figure_set_id, ProposalFigureSet.proposal_id == proposal_id,
            )
        )
        fset = result.scalar_one_or_none()
        if not fset:
            raise HTTPException(status_code=404, detail="Figure set not found")
        return fset

    # ── §3 — AI Visual Planning Before Figure Generation ────────────────────

    async def plan_visual_communication(
        self, db: AsyncSession, proposal: Proposal, source_section: str, *,
        org_id: Optional[str] = None, user_id: Optional[str] = None, price_cents_charged: int = 0,
    ) -> ProposalFigureSet:
        section = await self._get_section_or_400(db, proposal.id, source_section)
        fset = await self.get_or_create_figure_set(db, proposal.id, source_section, user_id=user_id)

        prompt = f"""You are analyzing one section of a federal/institutional grant proposal to decide whether professional figures would help a reviewer understand it, per this Visual Communication Plan standard:

- whether figures would materially improve reviewer understanding of this section
- concepts in this section that specifically require visual explanation
- the appropriate number of figures (1 if only a functional/process diagram is warranted, 2 if a paired functional diagram + technical/physical illustration is warranted; do not recommend more than 2 unless the content is unusually complex)
- Figure 1's purpose (what functional/process story it should tell)
- Figure 2's purpose, if a second figure is warranted (what physical/technical story it should tell) — otherwise null
- a short note on how Figure 1 and Figure 2 should relate to each other, if both are warranted — otherwise null

Section: "{section.title}" (proposal: "{proposal.title}")
Section content:
{section.content[:6000]}

Respond with a single JSON object only, with these exact keys:
{{
  "materially_improves_understanding": true or false,
  "concepts_requiring_visuals": ["...", "..."],
  "recommended_figure_count": 1 or 2,
  "figure_1_purpose": "...",
  "figure_2_purpose": "..." or null,
  "relationship_notes": "..." or null,
  "notes": "..." or null
}}"""

        try:
            response = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are an expert technical proposal editor deciding whether and how to use figures. Respond with a single JSON object only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
                max_tokens=900,
            )
        except Exception as exc:
            raise _ai_error(exc)

        parsed = _parse_json_response(response.choices[0].message.content or "")
        fset.visual_communication_plan = {
            "materially_improves_understanding": bool(parsed.get("materially_improves_understanding", True)),
            "concepts_requiring_visuals": [str(c) for c in (parsed.get("concepts_requiring_visuals") or [])],
            "recommended_figure_count": int(parsed.get("recommended_figure_count") or 1) if str(parsed.get("recommended_figure_count") or "1").isdigit() else 1,
            "figure_1_purpose": parsed.get("figure_1_purpose"),
            "figure_2_purpose": parsed.get("figure_2_purpose"),
            "relationship_notes": parsed.get("relationship_notes"),
            "notes": parsed.get("notes"),
        }
        await db.flush()

        prompt_tokens, completion_tokens = usage_from_response(response)
        await usage_tracking.record_usage(
            db, org_id=org_id, user_id=user_id, operation="figure:visual_communication_plan",
            model=settings.OPENAI_MODEL, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            price_cents_charged=price_cents_charged,
            reference={"proposal_id": proposal.id, "figure_set_id": fset.id},
        )
        return fset

    # ── §4-§8, §25 — Figure 1 functional/process diagram ────────────────────

    async def generate_figure_1(
        self, db: AsyncSession, figure_set: ProposalFigureSet, proposal: Proposal, *,
        diagram_family: Optional[str] = None, layout: Optional[str] = None,
        detail_level: Optional[str] = None, visual_style: Optional[str] = None,
        org_id: Optional[str] = None, user_id: Optional[str] = None, price_cents_charged: int = 0,
    ) -> ProposalFigure:
        section = await self._get_section_or_400(db, proposal.id, figure_set.source_section)

        prompt = f"""You are extracting the functional/process model from one section of a grant proposal to build "Figure 1" — a professional functional-workflow diagram — per this content-extraction standard (only extract what the content actually supports; do not invent unsupported technical claims):

- objective: what process, system, or method is being explained
- starting condition/input: what initiates the process
- functional stages: the principal functions or steps, in logical sequence
- feedback loops or decision points, if any (note them in a stage's label if relevant — the diagram itself is a flat ordered sequence, so represent a return-to-earlier-stage relationship in the "layout" recommendation of "circular_feedback" rather than in the node list)
- end state/output: what result the process produces

For each functional stage, classify it as one of exactly: "confirmed" (explicitly described in the section content), "inferred" (reasonably necessary to represent the described system but not explicitly stated), or "conceptual" (introduced only to create a coherent diagram, not yet an established design decision). Only proposal-supported relationships should be presented as "confirmed".

Also recommend:
- diagram_family: one of "functional_block", "process_flow", "workflow", "system_architecture", "scientific_mechanism", "experimental_workflow", "manufacturing_process", or another concise family name if none of these fit
- layout: one of exactly "horizontal", "vertical", "hierarchical", "circular_feedback" ("circular_feedback" only if the process genuinely has a feedback/return loop)
- detail_level: one of exactly "simplified", "standard", "detailed"
- visual_style: one of "clariva_professional", "black_white", "publication_style"

Section: "{section.title}" (proposal: "{proposal.title}")
Section content:
{section.content[:6000]}

Respond with a single JSON object only, with these exact keys:
{{
  "nodes": [{{"id": "short_snake_case_id", "label": "Short Node Label", "order": 1, "classification": "confirmed"}}, ...],
  "diagram_family": "...",
  "layout": "...",
  "detail_level": "...",
  "visual_style": "...",
  "caption": "Figure 1. ...",
  "alt_text": "...",
  "concept_disclosure": "..." or null
}}

The caption must follow this pattern: "Figure 1. Functional workflow of [subject]. [One sentence summarizing the stage sequence]." The alt_text must be a plain-language accessibility description of the diagram (not identical to the caption). Set concept_disclosure only if at least one node is classified "conceptual"; otherwise null."""

        try:
            response = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are an expert technical illustrator's assistant extracting a functional workflow from proposal text into structured JSON. Respond with a single JSON object only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
                max_tokens=1400,
            )
        except Exception as exc:
            raise _ai_error(exc)

        parsed = _parse_json_response(response.choices[0].message.content or "")
        nodes = _normalize_nodes(parsed.get("nodes") or [])
        if not nodes:
            raise HTTPException(status_code=500, detail="The AI did not return any functional stages for this section. Please try again.")

        resolved_layout = layout if layout in _VALID_LAYOUTS else (
            parsed.get("layout") if parsed.get("layout") in _VALID_LAYOUTS else "horizontal"
        )
        resolved_diagram_family = diagram_family or parsed.get("diagram_family") or "functional_block"
        resolved_detail_level = detail_level or parsed.get("detail_level") or "standard"
        resolved_visual_style = visual_style or parsed.get("visual_style") or "clariva_professional"

        has_conceptual = any(n["classification"] == "conceptual" for n in nodes)
        concept_disclosure = (parsed.get("concept_disclosure") or _DEFAULT_CONCEPT_DISCLOSURE) if has_conceptual else None

        caption = parsed.get("caption") or f"Figure 1. Functional workflow of {proposal.title}."
        alt_text = parsed.get("alt_text") or (
            "Diagram showing a " + str(len(nodes)) + "-stage functional workflow: "
            + "; ".join(f"{n['order']} {n['label']}" for n in nodes) + "."
        )

        # Regenerating Figure 1 replaces the existing figure_number=1 row in
        # place (same figure_number, same figure identity) rather than
        # creating a new row — see module docstring for why this also
        # resets approval_status.
        result = await db.execute(
            select(ProposalFigure).where(
                ProposalFigure.figure_set_id == figure_set.id, ProposalFigure.figure_number == 1,
            )
        )
        figure = result.scalar_one_or_none()
        is_new = figure is None
        if is_new:
            figure = ProposalFigure(
                id=new_uuid(), figure_set_id=figure_set.id, figure_number=1, created_by_user_id=user_id,
            )
            db.add(figure)

        figure.figure_type = "functional_workflow"
        figure.role = "functional_reference_model"
        figure.purpose = (figure_set.visual_communication_plan or {}).get("figure_1_purpose")
        figure.diagram_family = resolved_diagram_family
        figure.layout = resolved_layout
        figure.detail_level = resolved_detail_level
        figure.visual_style = resolved_visual_style
        figure.nodes = nodes
        figure.callouts = []
        figure.panels = []
        figure.caption = caption
        figure.alt_text = alt_text
        figure.concept_disclosure = concept_disclosure
        figure.requires_user_approval = True
        figure.approval_status = "pending"
        prompt_tokens, completion_tokens = usage_from_response(response)
        figure.generation_metadata = {
            "stage": "content_extraction",
            "model": settings.OPENAI_MODEL,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "regenerated": not is_new,
        }
        await db.flush()

        # Best-effort deterministic render + upload — a rendering/storage
        # failure never loses the AI-extracted structured content above.
        try:
            png_bytes = render_functional_diagram_png(nodes, layout=resolved_layout, title=caption)
            filename = f"figure_1_{figure.id}.png"
            storage_key = await storage.upload_file(org_id, "proposal_figure", png_bytes, filename, "image/png")
            stored_file = StoredFile(
                id=new_uuid(), org_id=org_id, object_type="proposal_figure", object_id=figure.id,
                storage_key=storage_key, original_filename=filename, content_type="image/png",
                size_bytes=len(png_bytes), checksum=storage.sha256_hex(png_bytes), created_by=user_id,
            )
            db.add(stored_file)
            await db.flush()
            figure.stored_file_id = stored_file.id
        except Exception as exc:
            _log.warning("Figure 1 image render/upload failed (structured figure data still saved): %s", exc, exc_info=True)

        # Set-level status rollup — "ready" once Figure 1 is the only
        # planned figure; "generating" if the Visual Communication Plan
        # recommended a Figure 2 that hasn't been generated yet (Phase 11).
        recommended_count = (figure_set.visual_communication_plan or {}).get("recommended_figure_count") or 2
        figure_set.status = "ready" if recommended_count <= 1 else "generating"
        await db.flush()

        await usage_tracking.record_usage(
            db, org_id=org_id, user_id=user_id, operation="figure:figure_1_generation",
            model=settings.OPENAI_MODEL, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            price_cents_charged=price_cents_charged,
            reference={"proposal_id": proposal.id, "figure_set_id": figure_set.id, "figure_id": figure.id},
        )
        return figure

    # ── §9-§16, §19, §25 — Figure 2 technical/physical illustration ────────

    async def _generate_image(self, prompt: str) -> Optional[bytes]:
        """§19 Stage 1 — Visual Generation. Returns PNG bytes, or None on
        any failure (rate limit, content-policy rejection, network error)
        — degrade gracefully, same convention as Figure 1's render/upload
        try/except. Uses response_format="b64_json" (not the default URL
        response) so this never needs a second network round-trip to fetch
        the image, unlike engines/image_gen.py's legacy generate_dalle_image
        helper — one fewer failure point for a call this codebase already
        treats as best-effort."""
        try:
            response = await self.client.images.generate(
                model=_IMAGE_GEN_MODEL, prompt=prompt[:4000], size=_IMAGE_GEN_SIZE,
                quality="standard", n=1, response_format="b64_json",
            )
            b64_data = response.data[0].b64_json
            if not b64_data:
                return None
            return base64.b64decode(b64_data)
        except Exception as exc:
            _log.warning("Figure 2 image generation failed: %s", exc, exc_info=True)
            return None

    async def generate_figure_2(
        self, db: AsyncSession, figure_set: ProposalFigureSet, proposal: Proposal, figure_1: ProposalFigure, *,
        view_type: Optional[str] = None, visual_style: Optional[str] = None,
        org_id: Optional[str] = None, user_id: Optional[str] = None, price_cents_charged: int = 0,
    ) -> ProposalFigure:
        section = await self._get_section_or_400(db, proposal.id, figure_set.source_section)
        figure_1_nodes = figure_1.nodes or []
        figure_1_node_ids = {n["id"] for n in figure_1_nodes if isinstance(n, dict) and n.get("id")}
        figure_1_summary = "; ".join(f"{n.get('order')} {n.get('label')}" for n in figure_1_nodes)

        prompt = f"""You are extracting a technical/physical illustration brief for "Figure 2" of a grant proposal — a companion to the already-generated "Figure 1" functional workflow. Figure 2 must answer "What physically or technically implements the functions shown in Figure 1?" (spec §9), built from Figure 1 + this section's content, preserving Figure 1's numbering wherever a function maps onto a physical component (spec §10/§11 traceability). Not every functional stage necessarily resides inside the physical device — some (e.g. offline AI training) may occur in an external environment; do not imply otherwise.

Figure 1's functional stages, in order: {figure_1_summary}

Section: "{section.title}" (proposal: "{proposal.title}")
Section content:
{section.content[:6000]}

Determine:
- view_type: one of exactly "perspective", "sectional", "perspective_sectional", "exploded", "architecture". Prefer "perspective_sectional" for complex physical technologies (perspective answers what it looks like, sectional answers what's inside it) unless the content clearly calls for a single view.
- visual_style: one of exactly "photorealistic", "black_white", "line_diagram".
- callouts: a numbered legend of the physical/technical components visible in the illustration, each classified confirmed/inferred/conceptual (same definitions as Figure 1) and, where a component implements one of Figure 1's functional stages, "relates_to_node" set to that stage's exact id from this list: {sorted(figure_1_node_ids)} (use null if no clean mapping exists — do not force one).
- panel_a_image_prompt: a detailed, professional prompt for an AI image generator to produce Panel A — a perspective view showing the complete proposed system in a representative operating environment. Minimal-to-no embedded text/labels in the image itself (numbering is added separately) — describe the physical form, materials, and setting concretely.
- panel_b_image_prompt: only if view_type is "perspective_sectional" — a detailed prompt for Panel B, a sectional/cutaway view exposing the major internal subsystems and their spatial relationships. Null if view_type is not "perspective_sectional".
- caption: Figure 2's caption, following this pattern: "Figure 2. Physical and technical implementation of the functional workflow shown in Figure 1. [One sentence connecting specific numbered functions to how they're physically implemented.] [If any conceptual elements: concept disclosure sentence.]"
- alt_text: a plain-language accessibility description (not identical to the caption).
- concept_disclosure: "..." if at least one callout is classified "conceptual", otherwise null.

Respond with a single JSON object only, with these exact keys: view_type, visual_style, callouts (list of {{"number","label","relates_to_node","classification"}}), panel_a_image_prompt, panel_b_image_prompt, caption, alt_text, concept_disclosure."""

        try:
            response = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are an expert technical illustrator's assistant preparing an image-generation brief for a physical/technical proposal figure, grounded in an existing functional diagram. Respond with a single JSON object only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
                max_tokens=1600,
            )
        except Exception as exc:
            raise _ai_error(exc)

        parsed = _parse_json_response(response.choices[0].message.content or "")

        resolved_view_type = view_type if view_type in _VALID_VIEW_TYPES else (
            parsed.get("view_type") if parsed.get("view_type") in _VALID_VIEW_TYPES else "perspective_sectional"
        )
        resolved_visual_style = visual_style if visual_style in _VALID_VISUAL_STYLES_F2 else (
            parsed.get("visual_style") if parsed.get("visual_style") in _VALID_VISUAL_STYLES_F2 else "photorealistic"
        )
        callouts = _normalize_callouts(parsed.get("callouts") or [], figure_1_node_ids)

        has_conceptual = any(c["classification"] == "conceptual" for c in callouts)
        concept_disclosure = (parsed.get("concept_disclosure") or _DEFAULT_CONCEPT_DISCLOSURE) if has_conceptual else None

        caption = parsed.get("caption") or (
            f"Figure 2. Physical and technical implementation of the functional workflow shown in Figure 1 of {proposal.title}."
        )
        alt_text = parsed.get("alt_text") or (
            "Technical illustration of the physical implementation, with numbered components: "
            + "; ".join(f"{c['number']} {c['label']}" for c in callouts) + "."
        )

        panels: List[Dict[str, Any]] = [{
            "panel": "A", "role": "perspective",
            "description": parsed.get("panel_a_image_prompt") or "Perspective view of the complete proposed system.",
        }]
        panel_b_prompt = parsed.get("panel_b_image_prompt") if resolved_view_type in _TWO_PANEL_VIEW_TYPES else None
        if panel_b_prompt:
            panels.append({"panel": "B", "role": "sectional", "description": panel_b_prompt})

        relationship_data = {
            "depends_on_figure_number": 1,
            "relationship_type": "physical_implementation_of_functional_model",
            "preserve_functional_sequence": True,
            "preserve_numbering": True,
        }

        # Regenerating Figure 2 replaces the existing figure_number=2 row in
        # place — same convention as Figure 1 (see module docstring).
        figure = await self._get_figure(db, figure_set.id, 2)
        is_new = figure is None
        if is_new:
            figure = ProposalFigure(
                id=new_uuid(), figure_set_id=figure_set.id, figure_number=2, created_by_user_id=user_id,
            )
            db.add(figure)

        figure.figure_type = "technical_illustration"
        figure.role = "physical_implementation"
        figure.purpose = (figure_set.visual_communication_plan or {}).get("figure_2_purpose")
        figure.view_type = resolved_view_type
        figure.visual_style = resolved_visual_style
        figure.nodes = []
        figure.callouts = callouts
        figure.panels = panels
        figure.relationship_data = relationship_data
        figure.caption = caption
        figure.alt_text = alt_text
        figure.concept_disclosure = concept_disclosure
        figure.requires_user_approval = True
        figure.approval_status = "pending"
        prompt_tokens, completion_tokens = usage_from_response(response)

        # §19 Stage 1 — Visual Generation. Best-effort: an image failure
        # still leaves the structured brief above saved (degrade
        # gracefully), same as Figure 1's render/upload try/except.
        image_prompt_a = f"Professional engineering visualization, {resolved_visual_style.replace('_', ' ')} style. {panels[0]['description']}"
        panel_a_bytes = await self._generate_image(image_prompt_a)
        panel_b_bytes = None
        if len(panels) > 1:
            image_prompt_b = f"Professional engineering visualization, {resolved_visual_style.replace('_', ' ')} style. {panels[1]['description']}"
            panel_b_bytes = await self._generate_image(image_prompt_b)

        image_generation_succeeded = panel_a_bytes is not None
        figure.generation_metadata = {
            "stage": "visual_generation",
            "model": _IMAGE_GEN_MODEL if image_generation_succeeded else None,
            "text_model": settings.OPENAI_MODEL,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "regenerated": not is_new,
            "panel_count": 2 if panel_b_bytes else 1,
            "image_generation_succeeded": image_generation_succeeded,
        }
        await db.flush()

        # §19 Stage 2 — Programmatic Annotation. "Annotation" here is the
        # deterministic two-panel composite + this figure's `callouts`
        # legend (rendered adjacent to the image by a document renderer,
        # not burned into pixels) — see
        # utils/figure_renderer.py::compose_two_panel_figure's docstring.
        if panel_a_bytes:
            try:
                composite_png = compose_two_panel_figure(
                    panel_a_bytes, panel_b_bytes,
                    panel_a_label="Panel A - Perspective View",
                    panel_b_label="Panel B - Sectional / Cutaway View",
                )
                filename = f"figure_2_{figure.id}.png"
                storage_key = await storage.upload_file(org_id, "proposal_figure", composite_png, filename, "image/png")
                stored_file = StoredFile(
                    id=new_uuid(), org_id=org_id, object_type="proposal_figure", object_id=figure.id,
                    storage_key=storage_key, original_filename=filename, content_type="image/png",
                    size_bytes=len(composite_png), checksum=storage.sha256_hex(composite_png), created_by=user_id,
                )
                db.add(stored_file)
                await db.flush()
                figure.stored_file_id = stored_file.id
            except Exception as exc:
                _log.warning("Figure 2 composite/upload failed (structured figure data still saved): %s", exc, exc_info=True)
        else:
            _log.info("Figure 2 image generation did not succeed for figure_set %s; structured brief saved without an image.", figure_set.id)

        # Both figures now exist (or Figure 2 generation was attempted) —
        # this is the last figure Phase 10/11 generate.
        figure_set.status = "ready"
        await db.flush()

        await usage_tracking.record_usage(
            db, org_id=org_id, user_id=user_id, operation="figure:figure_2_generation",
            model=settings.OPENAI_MODEL, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            price_cents_charged=price_cents_charged,
            reference={"proposal_id": proposal.id, "figure_set_id": figure_set.id, "figure_id": figure.id},
        )
        return figure

    # ── §20/§22 — human annotation edits + approval-status state machine ────

    async def update_annotations(
        self, db: AsyncSession, figure: ProposalFigure, *,
        caption: Optional[str] = None, alt_text: Optional[str] = None,
        concept_disclosure: Optional[str] = None, callouts: Optional[List[Dict[str, Any]]] = None,
    ) -> ProposalFigure:
        """§20/§22 — the "Edit Illustration Brief" / caption-and-callout
        editing actions. A free, unmetered human action (not an AI call) —
        only submitted (non-None) fields are touched, so a partial edit
        never clobbers fields the caller didn't mean to change. Does NOT
        change approval_status — editing is a distinct action from
        approving (§22); a human reviewing and tweaking a figure's text is
        not the same as signing off on it as final."""
        if caption is not None:
            figure.caption = caption
        if alt_text is not None:
            figure.alt_text = alt_text
        if concept_disclosure is not None:
            figure.concept_disclosure = concept_disclosure
        if callouts is not None:
            # Re-validate relates_to_node the same way generate_figure_2
            # does — a human hand-editing a callout could type an id that
            # no longer exists, same "never trust a dangling trace link"
            # discipline _normalize_callouts already enforces for AI
            # output (§10/§11).
            figure_1_node_ids: set = set()
            if figure.figure_number != 1:
                figure_1 = await self._get_figure(db, figure.figure_set_id, 1)
                if figure_1:
                    figure_1_node_ids = {n["id"] for n in (figure_1.nodes or []) if isinstance(n, dict) and n.get("id")}
            figure.callouts = _normalize_callouts(callouts, figure_1_node_ids)
        await db.flush()
        # ProposalFigure.updated_at has onupdate=func.now() — after flush,
        # SQLAlchemy marks that column expired pending a re-fetch from the
        # DB rather than eagerly re-selecting it. The caller's response_model
        # (ProposalFigureOut) reads `updated_at` during Pydantic
        # serialization, which happens after this coroutine returns and
        # outside any single `await`ed call — a lazy refresh attempted there
        # has no active async greenlet context and raises
        # sqlalchemy.exc.MissingGreenlet. An explicit, awaited refresh here
        # (still inside this coroutine) avoids that.
        await db.refresh(figure)
        return figure

    async def set_approval_status(self, db: AsyncSession, figure: ProposalFigure, status: str) -> ProposalFigure:
        """§22 — "No AI-generated technical illustration shall
        automatically become the final proposal figure without user
        review." This is the only place approval_status is set by a human
        action (Phase 10/11's own resets to "pending" on regeneration are
        the system's side of this same state machine)."""
        if status not in _VALID_APPROVAL_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid approval status '{status}'. Must be one of: {sorted(_VALID_APPROVAL_STATUSES)}.",
            )
        figure.approval_status = status
        await db.flush()
        # See update_annotations()'s identical comment above — same
        # onupdate=func.now() MissingGreenlet fix.
        await db.refresh(figure)
        return figure

    # ── §24 — Automated Cross-Figure QA ──────────────────────────────────────

    async def run_cross_figure_qa(
        self, db: AsyncSession, figure_set: ProposalFigureSet, proposal: Proposal, figure_1: ProposalFigure, *,
        org_id: Optional[str] = None, user_id: Optional[str] = None, price_cents_charged: int = 0,
    ) -> List[ProposalFigure]:
        """§24 — checks Figure 1 alone, and (if it exists) Figure 2 alone
        plus the Figure 1 <-> Figure 2 relationship, against the spec's own
        three checklists. Never auto-regenerates on failure (see module
        docstring) — a failing check flips a still-"pending" figure's
        approval_status to "needs_regeneration" so a human decides what to
        do next, matching §22's human-in-the-loop principle."""
        section = await self._get_section_or_400(db, proposal.id, figure_set.source_section)
        figure_2 = await self._get_figure(db, figure_set.id, 2)

        figure_1_summary = "; ".join(
            f"{n.get('order')} {n.get('label')} ({n.get('classification')})" for n in (figure_1.nodes or [])
        ) or "(no nodes)"
        figure_2_block = "Figure 2 has not been generated yet — skip Figure 2 QA and Figure 1<->Figure 2 QA."
        if figure_2:
            callouts_summary = "; ".join(
                f"{c.get('number')} {c.get('label')} (relates_to_node={c.get('relates_to_node')}, {c.get('classification')})"
                for c in (figure_2.callouts or [])
            ) or "(no callouts)"
            figure_2_block = (
                f"Figure 2 — view_type: {figure_2.view_type}, visual_style: {figure_2.visual_style}\n"
                f"Figure 2 caption: {figure_2.caption}\n"
                f"Figure 2 callouts (number, label, relates_to_node, classification): {callouts_summary}"
            )

        prompt = f"""You are performing Automated Cross-Figure QA on a grant proposal's figure set, per this checklist standard. Evaluate against the section content and the figure data below, and report which specific criteria pass or fail.

Figure 1 QA criteria:
- functional steps are supported by the proposal content
- the sequence is technically logical
- node labels are concise
- terminology and numbering are consistent
- the caption accurately describes the figure

Figure 2 QA criteria (only if Figure 2 exists):
- represents the same proposed technology as Figure 1
- each major Figure 1 function has an identifiable technical implementation in Figure 2
- external functions are distinguished from onboard/internal functions
- unsupported elements are classified appropriately (inferred/conceptual, not confirmed)
- Figure 2 adds physical/technical understanding rather than duplicating Figure 1

Figure 1 <-> Figure 2 QA criteria (only if Figure 2 exists):
- numbering is consistent between the two figures
- terminology is consistent between the two figures
- the relationships shown are technically credible
- Figure 2 visibly builds upon Figure 1
- a reviewer can understand the function-to-implementation connection
- the captions reinforce the relationship

Section: "{section.title}" (proposal: "{proposal.title}")
Section content:
{section.content[:6000]}

Figure 1 caption: {figure_1.caption}
Figure 1 nodes (order, label, classification): {figure_1_summary}

{figure_2_block}

Respond with a single JSON object only, with these exact keys:
{{
  "figure_1_qa": {{"passed": true or false, "checks": [{{"criterion": "...", "passed": true or false, "notes": "..." or null}}, ...]}},
  "figure_2_qa": {{"passed": true or false, "checks": [...]}} or null,
  "cross_figure_qa": {{"passed": true or false, "checks": [...]}} or null
}}
Set figure_2_qa and cross_figure_qa to null if Figure 2 has not been generated yet. Each "checks" list should have one entry per criterion listed above for that section."""

        try:
            response = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are a meticulous technical proposal QA reviewer checking figures against a fixed checklist. Respond with a single JSON object only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=1600,
            )
        except Exception as exc:
            raise _ai_error(exc)

        parsed = _parse_json_response(response.choices[0].message.content or "")
        checked_at = datetime.now(timezone.utc).isoformat()

        fig1_qa = parsed.get("figure_1_qa") or {}
        fig1_checks = _normalize_qa_checks(fig1_qa.get("checks"))
        fig1_passed = bool(fig1_qa.get("passed", True)) and all(c["passed"] for c in fig1_checks)
        figure_1.qa_report = {"passed": fig1_passed, "checks": fig1_checks, "checked_at": checked_at}
        if figure_1.approval_status == "pending" and not fig1_passed:
            figure_1.approval_status = "needs_regeneration"

        updated = [figure_1]
        if figure_2:
            fig2_qa = parsed.get("figure_2_qa") or {}
            cross_qa = parsed.get("cross_figure_qa") or {}
            fig2_checks = _normalize_qa_checks(fig2_qa.get("checks"))
            cross_checks = _normalize_qa_checks(cross_qa.get("checks"))
            fig2_passed = (
                bool(fig2_qa.get("passed", True)) and all(c["passed"] for c in fig2_checks)
                and bool(cross_qa.get("passed", True)) and all(c["passed"] for c in cross_checks)
            )
            figure_2.qa_report = {
                "passed": fig2_passed, "checks": fig2_checks,
                "cross_figure_checks": cross_checks, "checked_at": checked_at,
            }
            if figure_2.approval_status == "pending" and not fig2_passed:
                figure_2.approval_status = "needs_regeneration"
            updated.append(figure_2)

        await db.flush()

        prompt_tokens, completion_tokens = usage_from_response(response)
        await usage_tracking.record_usage(
            db, org_id=org_id, user_id=user_id, operation="figure:cross_figure_qa",
            model=settings.OPENAI_MODEL, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            price_cents_charged=price_cents_charged,
            reference={"proposal_id": proposal.id, "figure_set_id": figure_set.id},
        )
        return updated
