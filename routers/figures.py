"""
AI Figure Generation & Technical Illustration router.

docs/2_Upgrades_Clariva_AI_Figure_Specification_V01.docx. Phase 10 built
"Figure 1 functional/process diagram generation"; Phase 11 added "Figure 2
technical/physical illustration generation"; Phase 12 adds Automated
Cross-Figure QA (§24) plus human annotation-edit and approval-status
endpoints (§20/§22). Mounted at the same /api/v1/proposals prefix as
scope_of_work.py (same convention: a proposal-scoped sub-resource, not a
standalone top-level collection).

Ownership: reuses proposals.py's `_get_proposal_or_404` (owner-only),
exactly scope_of_work.py's access rule — a figure set only ever belongs to
one proposal, and proposal editing is already owner-scoped.

Pricing: `org_id` is optional and additive (same contract as every other
AI-generation endpoint in this codebase — see scope_of_work.py's `_meter`
docstring and proposals.py::generate_section). Unlike scope_of_work.py's
flat GENERATION_COST, figures use real service-catalog prices
(figure_visual_plan / figure_1_generation / figure_2_generation /
figure_qa_check — see service_catalog_engine.py's SERVICE_CATALOG_SEED)
via `catalog_engine.consume()`, called only when `org_id` is provided — a
personal/orgless proposal owner can still generate figures unmetered,
matching proposals.py::generate_all_sections's own `if org_id:` guard
around its catalog_engine.consume() call. The Phase 12 annotation-edit and
approval-status endpoints are human actions, not AI calls, so they are
never metered — no `_charge()` call at all, same as this codebase's other
free human-edit endpoints (e.g. collaboration.py's comment/task CRUD).

Cheap-checks-before-charging: figure-1 requires the source section to have
generated content (assert_section_ready()); figure-2 and the QA check
additionally require Figure 1 to already exist in the figure set
(assert_figure_1_ready()). All checks run before `_charge()`.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import User
from models.schemas import (
    Figure1GenerateRequest, Figure2GenerateRequest, FigureAnnotationUpdateRequest,
    FigureApprovalUpdateRequest, ProposalFigureOut, ProposalFigureSetOut,
    VisualCommunicationPlanRequest,
)
from routers.auth import get_current_user
from routers.proposals import _get_proposal_or_404
from routers.organizations import _assert_member
from engines.figure_engine import FigureEngine
from engines.credit_engine import InsufficientCreditsError
from engines.service_catalog_engine import ServiceCatalogEngine

router = APIRouter()
engine = FigureEngine()
catalog_engine = ServiceCatalogEngine()


async def _charge(org_id: Optional[str], user_id: str, db: AsyncSession, service_key: str, reference: dict) -> int:
    """Optional and additive — see module docstring. Returns the
    price_cents actually charged (0 for unmetered personal use), so the
    engine's usage_tracking record reflects what was really billed."""
    if not org_id:
        return 0
    await _assert_member(org_id, user_id, db)
    try:
        txn = await catalog_engine.consume(db, org_id, user_id, service_key, reference=reference)
    except InsufficientCreditsError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    return txn.price_cents


@router.get("/{proposal_id}/figures", response_model=List[ProposalFigureSetOut])
async def list_figure_sets(
    proposal_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    return await engine.list_figure_sets(db, proposal_id)


@router.get("/{proposal_id}/figures/{figure_set_id}", response_model=ProposalFigureSetOut)
async def get_figure_set(
    proposal_id: str, figure_set_id: str,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    return await engine.get_figure_set_or_404(db, figure_set_id, proposal_id)


@router.post("/{proposal_id}/figures/plan", response_model=ProposalFigureSetOut)
async def plan_visual_communication(
    proposal_id: str, body: VisualCommunicationPlanRequest, org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """§3 — AI Visual Planning Before Figure Generation. Creates the
    figureSet for this section if it doesn't exist yet, or re-plans an
    existing one (re-running this does not touch any already-generated
    figures — only the visual_communication_plan field)."""
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    await engine.assert_section_ready(db, proposal_id, body.source_section)
    price_cents = await _charge(
        org_id, current_user.id, db, "figure_visual_plan",
        reference={"proposal_id": proposal_id, "source_section": body.source_section},
    )
    return await engine.plan_visual_communication(
        db, proposal, body.source_section,
        org_id=org_id, user_id=current_user.id, price_cents_charged=price_cents,
    )


@router.post("/{proposal_id}/figures/{figure_set_id}/figure-1", response_model=ProposalFigureOut)
async def generate_figure_1(
    proposal_id: str, figure_set_id: str, body: Figure1GenerateRequest, org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """§4-§8 — Figure 1 functional/process diagram generation. Calling this
    again on the same figure set regenerates Figure 1 in place (§22's
    "Regenerate" user action) and resets its approval_status to "pending"
    — see engines/figure_engine.py's module docstring."""
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    figure_set = await engine.get_figure_set_or_404(db, figure_set_id, proposal_id)
    await engine.assert_section_ready(db, proposal_id, figure_set.source_section)
    price_cents = await _charge(
        org_id, current_user.id, db, "figure_1_generation",
        reference={"proposal_id": proposal_id, "figure_set_id": figure_set_id},
    )
    return await engine.generate_figure_1(
        db, figure_set, proposal,
        diagram_family=body.diagram_family, layout=body.layout,
        detail_level=body.detail_level, visual_style=body.visual_style,
        org_id=org_id, user_id=current_user.id, price_cents_charged=price_cents,
    )


@router.post("/{proposal_id}/figures/{figure_set_id}/figure-2", response_model=ProposalFigureOut)
async def generate_figure_2(
    proposal_id: str, figure_set_id: str, body: Figure2GenerateRequest, org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """§9-§16, §19 — Figure 2 technical/physical illustration generation.
    Figure 1 must already exist in this figure set (§10/§11 traceability —
    checked via assert_figure_1_ready() BEFORE charging, same "cheap
    checks before spending credits" ordering assert_section_ready()
    established in Phase 10). Calling this again on the same figure set
    regenerates Figure 2 in place and resets its approval_status to
    "pending" — see engines/figure_engine.py's module docstring."""
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    figure_set = await engine.get_figure_set_or_404(db, figure_set_id, proposal_id)
    figure_1 = await engine.assert_figure_1_ready(db, figure_set_id)
    price_cents = await _charge(
        org_id, current_user.id, db, "figure_2_generation",
        reference={"proposal_id": proposal_id, "figure_set_id": figure_set_id},
    )
    return await engine.generate_figure_2(
        db, figure_set, proposal, figure_1,
        view_type=body.view_type, visual_style=body.visual_style,
        org_id=org_id, user_id=current_user.id, price_cents_charged=price_cents,
    )


@router.post("/{proposal_id}/figures/{figure_set_id}/qa", response_model=List[ProposalFigureOut])
async def run_cross_figure_qa(
    proposal_id: str, figure_set_id: str, org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """§24 — Automated Cross-Figure QA. Requires Figure 1 to already exist
    (checked via assert_figure_1_ready() BEFORE charging, same ordering as
    the figure-2 endpoint). Runs Figure 1 QA alone if Figure 2 hasn't been
    generated yet, or all three checklists (Figure 1, Figure 2, Figure 1
    <-> Figure 2) once it has. Never auto-regenerates on failure — flips a
    still-"pending" figure's approval_status to "needs_regeneration" so a
    human decides what to do next (§22). Returns the updated figure(s)."""
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    figure_set = await engine.get_figure_set_or_404(db, figure_set_id, proposal_id)
    figure_1 = await engine.assert_figure_1_ready(db, figure_set_id)
    price_cents = await _charge(
        org_id, current_user.id, db, "figure_qa_check",
        reference={"proposal_id": proposal_id, "figure_set_id": figure_set_id},
    )
    return await engine.run_cross_figure_qa(
        db, figure_set, proposal, figure_1,
        org_id=org_id, user_id=current_user.id, price_cents_charged=price_cents,
    )


@router.patch("/{proposal_id}/figures/{figure_set_id}/figures/{figure_number}/annotations", response_model=ProposalFigureOut)
async def update_figure_annotations(
    proposal_id: str, figure_set_id: str, figure_number: int, body: FigureAnnotationUpdateRequest,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """§20/§22 — human "Edit Illustration Brief" actions (caption,
    alt_text, concept_disclosure, callouts). Free/unmetered — a human
    edit, not an AI call. Does not change approval_status; use the
    /approval endpoint below for that."""
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    await engine.get_figure_set_or_404(db, figure_set_id, proposal_id)
    figure = await engine.get_figure_or_404(db, figure_set_id, figure_number)
    # mode="json" — plain `model_dump()` leaves `classification` as a
    # TechnicalAccuracyClassification enum instance, and str(enum_member)
    # on a (str, Enum) mixin renders as "ClassName.MEMBER" (not
    # enum_member.value) under Python's default Enum.__str__, which
    # _normalize_callouts's str(...).lower() call would then fail to
    # match against _VALID_CLASSIFICATIONS and silently overwrite with
    # the "confirmed" fallback — discarding the caller's actual choice.
    # mode="json" serializes the enum to its plain string value instead.
    callouts = [c.model_dump(mode="json") for c in body.callouts] if body.callouts is not None else None
    return await engine.update_annotations(
        db, figure,
        caption=body.caption, alt_text=body.alt_text,
        concept_disclosure=body.concept_disclosure, callouts=callouts,
    )


@router.post("/{proposal_id}/figures/{figure_set_id}/figures/{figure_number}/approval", response_model=ProposalFigureOut)
async def update_figure_approval(
    proposal_id: str, figure_set_id: str, figure_number: int, body: FigureApprovalUpdateRequest,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """§22 — "No AI-generated technical illustration shall automatically
    become the final proposal figure without user review." Sets
    approval_status to pending/approved/rejected/needs_regeneration. Free/
    unmetered — a human decision, not an AI call."""
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    await engine.get_figure_set_or_404(db, figure_set_id, proposal_id)
    figure = await engine.get_figure_or_404(db, figure_set_id, figure_number)
    return await engine.set_approval_status(db, figure, body.status.value)
