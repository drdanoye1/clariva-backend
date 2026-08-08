"""
FOA router — upload, parse, retrieve funding opportunity announcements.

Phase 4 (Clariva Enterprise™ PRD §15) extends this router additively with
the pre-award pipeline surface — pipeline-stage transitions, Bid/No-Go
decisions, org-sharing, filtered listing, solicitation comparison, and
"enrich" (AI-parse a lightweight Grants.gov-synced record into a full
template on demand). The three existing creation endpoints below
(upload/parse-text/parse-url) are untouched: every new FOARecord column is
nullable/defaulted at the model layer, so they keep inserting exactly as
before. See engines/funding_intelligence_engine.py (Engine 15) and
routers/funding_intelligence.py for sync/watchlists/reporting.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from html.parser import HTMLParser
from typing import List, Optional

import httpx
import openai
from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models.db_models import Award, FOARecord, Proposal, User
from models.schemas import (
    BidNoGoRequest, FOARecordOut, FOATemplate, FOAUploadResponse,
    PipelineStageEventOut, PipelineStageUpdateRequest,
)
from routers.auth import get_current_user
from routers.organizations import _assert_member, _assert_permission
from engines.foa_parser import FOAParserEngine
from engines.template_builder import FOATemplateBuilderEngine
from engines.funding_intelligence_engine import FundingIntelligenceEngine

router = APIRouter()
parser  = FOAParserEngine()
builder = FOATemplateBuilderEngine()
funding = FundingIntelligenceEngine()


async def _to_foa_out(record: FOARecord, db: AsyncSession) -> FOARecordOut:
    """
    Version 3.0 architecture upgrade, Phase 14 (Renewal Loop Closure) — now
    async so it can resolve `originating_award_id` into a human-readable
    label/link target for the (rare) records created as a renewal. This is
    a single extra query only when that field is set — the overwhelming
    majority of FOARecords have it None and pay no extra cost.
    """
    originating_proposal_id: Optional[str] = None
    originating_award_label: Optional[str] = None
    if record.originating_award_id:
        result = await db.execute(
            select(Award, Proposal.title)
            .join(Proposal, Proposal.id == Award.proposal_id)
            .where(Award.id == record.originating_award_id)
        )
        row = result.first()
        if row:
            orig_award, proposal_title = row
            originating_proposal_id = orig_award.proposal_id
            originating_award_label = f"{proposal_title} · {orig_award.funding_agency}" + (
                f" ({orig_award.award_number})" if orig_award.award_number else ""
            )
    return FOARecordOut(
        id=record.id, org_id=record.org_id, agency=record.agency, program_title=record.program_title,
        solicitation_number=record.solicitation_number, phase=record.phase, grant_type=record.grant_type,
        total_page_limit=record.total_page_limit, deadline=record.deadline, source=record.source,
        external_id=record.external_id, external_url=record.external_url,
        estimated_award_floor=record.estimated_award_floor, estimated_award_ceiling=record.estimated_award_ceiling,
        eligibility_summary=record.eligibility_summary, pipeline_stage=record.pipeline_stage,
        bid_no_go_decision=record.bid_no_go_decision, bid_no_go_rationale=record.bid_no_go_rationale,
        assigned_to=record.assigned_to, uploaded_by=record.uploaded_by, last_synced_at=record.last_synced_at,
        created_at=record.created_at, has_parsed_template=record.parsed_template is not None,
        originating_award_id=record.originating_award_id, originating_proposal_id=originating_proposal_id,
        originating_award_label=originating_award_label, renewal_notes=record.renewal_notes,
    )


async def _get_foa_or_404(db: AsyncSession, foa_id: str) -> FOARecord:
    result = await db.execute(select(FOARecord).where(FOARecord.id == foa_id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return record


async def _assert_foa_access(record: FOARecord, current_user: User, db: AsyncSession, permission: Optional[str] = None) -> None:
    """
    The uploader always has access — this preserves today's personal-use
    behavior unchanged. For an org-shared record (org_id set), any org
    member may view it; pass `permission` to additionally require a
    specific rbac permission (e.g. "manage_pipeline") for mutating actions.
    """
    if record.uploaded_by == current_user.id:
        return
    if record.org_id:
        if permission:
            await _assert_permission(record.org_id, current_user.id, permission, db)
        else:
            await _assert_member(record.org_id, current_user.id, db)
        return
    raise HTTPException(status_code=404, detail="Opportunity not found")


def _clean_parse_error(exc: Exception) -> HTTPException:
    """
    Convert any FOA parse exception into a safe, user-facing HTTPException.
    Full technical details are written to server logs only — never exposed to users.
    """
    import logging, traceback
    log = logging.getLogger(__name__)

    if isinstance(exc, openai.RateLimitError):
        log.error("AI service rate limit exceeded: %s", exc)
        return HTTPException(
            status_code=503,
            detail=(
                "The AI parsing service is temporarily unavailable due to high demand. "
                "Please try again in a few minutes. If the problem persists, contact support."
            ),
        )
    if isinstance(exc, openai.AuthenticationError):
        log.critical("AI service authentication failure: %s", exc)
        return HTTPException(
            status_code=503,
            detail="The AI parsing service is not configured correctly. Please contact support.",
        )
    if isinstance(exc, openai.APIConnectionError):
        log.error("AI service connection error: %s", exc)
        return HTTPException(
            status_code=503,
            detail="The AI parsing service is unreachable. Please try again in a moment.",
        )
    if isinstance(exc, openai.APIStatusError):
        log.error("AI service API error %s: %s", exc.status_code, exc.message)
        return HTTPException(
            status_code=503,
            detail="The AI parsing service returned an unexpected error. Please try again.",
        )
    # Unknown error — log full traceback for admin, return generic message to user
    log.error("Unexpected FOA parse error:\n%s", traceback.format_exc())
    return HTTPException(
        status_code=500,
        detail="Could not process the document. Please try again or contact support.",
    )


# ── HTML → plain text helper ──────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    """Lightweight HTML-to-text extractor using stdlib only."""
    SKIP_TAGS = {"script", "style", "head", "nav", "footer", "header", "noscript"}

    def __init__(self):
        super().__init__()
        self._skip = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag.lower() in self.SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag.lower() in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self._skip:
            stripped = data.strip()
            if stripped:
                self.chunks.append(stripped + " ")

    def get_text(self) -> str:
        raw = "".join(self.chunks)
        # Collapse runs of blank lines
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return extractor.get_text()


@router.post("/upload", response_model=FOAUploadResponse)
async def upload_foa(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload a FOA PDF, DOCX, or TXT file. Parses it and builds a dynamic template."""
    ACCEPTED = {
        "application/pdf",
        "text/plain",
        "application/octet-stream",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "application/x-pdf",
    }
    ct = (file.content_type or "").split(";")[0].strip()
    fname = (file.filename or "").lower()
    if ct not in ACCEPTED and not any(fname.endswith(e) for e in (".pdf", ".txt", ".docx", ".doc")):
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ct}. Use PDF, DOCX, or TXT.")

    try:
        raw_bytes = await file.read()
        raw_text  = parser.extract_text(raw_bytes, file.filename or "foa.pdf")
        parsed    = await parser.parse(raw_text)
        template  = builder.build(parsed)

        record = FOARecord(
            id=str(uuid.uuid4()),
            agency=template.agency.value,
            program_title=template.program_title,
            solicitation_number=template.solicitation_number,
            phase=template.phase.value,
            raw_text=raw_text[:50000],
            parsed_template=template.model_dump(mode="json"),
            total_page_limit=template.total_page_limit,
            deadline=template.deadline,
            uploaded_by=current_user.id,
        )
        db.add(record)
        await db.flush()
    except Exception as exc:
        raise _clean_parse_error(exc)

    return FOAUploadResponse(
        foa_id=record.id,
        parsed=True,
        template=template,
        raw_text_preview=raw_text[:500],
    )


@router.post("/parse-text", response_model=FOAUploadResponse)
async def parse_foa_text(
    text: str = Body(..., embed=True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Parse pasted FOA text directly (no file upload needed)."""
    if not text or len(text.strip()) < 50:
        raise HTTPException(status_code=400, detail="Please paste at least 50 characters of FOA text.")
    try:
        parsed   = await parser.parse(text.strip())
        template = builder.build(parsed)

        record = FOARecord(
            id=str(uuid.uuid4()),
            agency=template.agency.value,
            program_title=template.program_title,
            solicitation_number=template.solicitation_number,
            phase=template.phase.value,
            raw_text=text[:50000],
            parsed_template=template.model_dump(mode="json"),
            total_page_limit=template.total_page_limit,
            deadline=template.deadline,
            uploaded_by=current_user.id,
        )
        db.add(record)
        await db.flush()
    except Exception as exc:
        raise _clean_parse_error(exc)

    return FOAUploadResponse(
        foa_id=record.id,
        parsed=True,
        template=template,
        raw_text_preview=text[:500],
    )


@router.post("/parse-url", response_model=FOAUploadResponse)
async def parse_foa_url(
    url: str = Body(..., embed=True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch a URL (grants.gov, agency site, PDF link, etc.) and parse it as a FOA."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    # ── Fetch the URL ─────────────────────────────────────────────────────────
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; Clariva-GrantBot/1.0; "
            "+https://ai-grant-generator.vercel.app)"
        ),
        "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain,*/*",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=422,
                detail=f"Could not fetch URL (HTTP {resp.status_code}). "
                       "Try downloading the PDF and uploading it directly.",
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=422, detail="Request timed out. The URL may be slow or unavailable.")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=422, detail=f"Network error fetching URL: {exc}")

    # ── Extract text based on content type ────────────────────────────────────
    ct = resp.headers.get("content-type", "").lower()
    if "pdf" in ct or url.lower().endswith(".pdf"):
        # Treat as PDF bytes
        raw_text = parser.extract_text(resp.content, "foa.pdf")
    elif "text/plain" in ct or url.lower().endswith(".txt"):
        raw_text = resp.text
    else:
        # HTML page — strip tags
        raw_text = _html_to_text(resp.text)

    raw_text = raw_text.strip()
    if len(raw_text) < 100:
        raise HTTPException(
            status_code=422,
            detail="Could not extract meaningful text from that URL. "
                   "Try downloading the PDF and uploading it directly.",
        )

    # Limit to 60 000 chars for the GPT-4o context window
    raw_text = raw_text[:60_000]

    # ── Parse & store ─────────────────────────────────────────────────────────
    try:
        parsed   = await parser.parse(raw_text)
        template = builder.build(parsed)

        record = FOARecord(
            id=str(uuid.uuid4()),
            agency=template.agency.value,
            program_title=template.program_title,
            solicitation_number=template.solicitation_number,
            phase=template.phase.value,
            raw_text=raw_text[:50_000],
            parsed_template=template.model_dump(mode="json"),
            total_page_limit=template.total_page_limit,
            deadline=template.deadline,
            uploaded_by=current_user.id,
        )
        db.add(record)
        await db.flush()
    except Exception as exc:
        raise _clean_parse_error(exc)

    return FOAUploadResponse(
        foa_id=record.id,
        parsed=True,
        template=template,
        raw_text_preview=raw_text[:500],
    )


@router.get("/pipeline", response_model=List[FOARecordOut])
async def list_pipeline(
    org_id: Optional[str] = None, pipeline_stage: Optional[str] = None,
    source: Optional[str] = None, assigned_to: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """
    The pipeline view (PRD §15) — distinct from the legacy `GET /` list
    above, which only ever returned the current user's own uploads as bare
    dicts. This returns the full FOARecordOut shape (pipeline stage,
    Bid/No-Go, sync metadata) and, when `org_id` is given, the whole org's
    shared pipeline rather than just "my" records.
    """
    if org_id:
        await _assert_member(org_id, current_user.id, db)
    records = await funding.list_pipeline(
        db, org_id=org_id, uploaded_by=None if org_id else current_user.id,
        pipeline_stage=pipeline_stage, source=source, assigned_to=assigned_to,
    )
    return [await _to_foa_out(r, db) for r in records]


@router.get("/compare", response_model=List[FOARecordOut])
async def compare_foas(
    ids: str = Query(..., description="Comma-separated FOA record IDs"),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Solicitation comparison (PRD §15) — fetch several opportunity
    records side by side in one call."""
    foa_ids = [i.strip() for i in ids.split(",") if i.strip()]
    if not foa_ids:
        raise HTTPException(status_code=400, detail="Provide at least one FOA id via ?ids=")
    records = await funding.compare(db, foa_ids)
    accessible = []
    for r in records:
        try:
            await _assert_foa_access(r, current_user, db)
            accessible.append(r)
        except HTTPException:
            continue  # silently skip records this user can't see, rather than 403ing the whole comparison
    return [await _to_foa_out(r, db) for r in accessible]


@router.get("/{foa_id}", response_model=FOATemplate)
async def get_foa(
    foa_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = await _get_foa_or_404(db, foa_id)
    await _assert_foa_access(record, current_user, db)
    if record.parsed_template is None:
        raise HTTPException(
            status_code=400,
            detail="This opportunity hasn't been AI-parsed into a template yet. "
                   "Use POST /foa/{foa_id}/enrich (or the compare/pipeline endpoints "
                   "for its metadata) first.",
        )
    return FOATemplate(**record.parsed_template)


@router.get("/", response_model=List[dict])
async def list_foas(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(FOARecord.id, FOARecord.program_title, FOARecord.agency,
               FOARecord.phase, FOARecord.deadline, FOARecord.created_at)
        .where(FOARecord.uploaded_by == current_user.id)
        .order_by(FOARecord.created_at.desc())
    )
    rows = result.all()
    return [
        {
            "foa_id": r.id,
            "program_title": r.program_title,
            "agency": r.agency,
            "phase": r.phase,
            "deadline": r.deadline,
            "created_at": r.created_at,
        }
        for r in rows
    ]


# ── Phase 4 — Pre-award pipeline (PRD §15) ───────────────────────────────────

@router.patch("/{foa_id}/pipeline-stage", response_model=FOARecordOut)
async def update_pipeline_stage(
    foa_id: str, body: PipelineStageUpdateRequest,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    record = await _get_foa_or_404(db, foa_id)
    await _assert_foa_access(record, current_user, db, permission="manage_pipeline")
    updated = await funding.update_pipeline_stage(db, foa_id, body.stage, current_user.id, body.notes)
    return await _to_foa_out(updated, db)


@router.patch("/{foa_id}/bid-no-go", response_model=FOARecordOut)
async def set_bid_no_go(
    foa_id: str, body: BidNoGoRequest,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    record = await _get_foa_or_404(db, foa_id)
    await _assert_foa_access(record, current_user, db, permission="manage_pipeline")
    updated = await funding.set_bid_no_go(db, foa_id, body.decision, body.rationale, current_user.id)
    return await _to_foa_out(updated, db)


@router.patch("/{foa_id}/assign", response_model=FOARecordOut)
async def assign_opportunity(
    foa_id: str, assigned_to: Optional[str] = Body(None, embed=True),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    record = await _get_foa_or_404(db, foa_id)
    await _assert_foa_access(record, current_user, db, permission="manage_pipeline")
    record.assigned_to = assigned_to
    await db.flush()
    await db.refresh(record)
    return await _to_foa_out(record, db)


@router.post("/{foa_id}/share", response_model=FOARecordOut)
async def share_opportunity(
    foa_id: str, org_id: str = Body(..., embed=True),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Share a personal opportunity record into an org-wide pipeline —
    same "must be the owner + hold share_proposals on the target org"
    contract as organizations.py::share_proposal."""
    record = await _get_foa_or_404(db, foa_id)
    if record.uploaded_by != current_user.id:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    if record.org_id:
        raise HTTPException(status_code=409, detail="This opportunity is already shared with an organization.")
    await _assert_permission(org_id, current_user.id, "share_proposals", db)
    record.org_id = org_id
    await db.flush()
    await db.refresh(record)
    return await _to_foa_out(record, db)


@router.get("/{foa_id}/stage-history", response_model=List[PipelineStageEventOut])
async def get_stage_history(
    foa_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    record = await _get_foa_or_404(db, foa_id)
    await _assert_foa_access(record, current_user, db)
    events = await funding.list_stage_events(db, foa_id)
    return events


@router.post("/{foa_id}/enrich", response_model=FOATemplate)
async def enrich_opportunity(
    foa_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """
    Upgrade a lightweight, sync-created opportunity record (source =
    grants_gov, no parsed_template yet) into a fully AI-parsed template —
    fetches the opportunity's full synopsis from Grants.gov's public
    fetchOpportunity API, then runs it through the same parser/builder
    pipeline as upload/parse-text/parse-url. Updates the existing record
    in place rather than creating a duplicate.
    """
    record = await _get_foa_or_404(db, foa_id)
    await _assert_foa_access(record, current_user, db, permission="manage_pipeline")
    if record.source != "grants_gov":
        raise HTTPException(status_code=400, detail="Enrichment is only available for Grants.gov-synced opportunities.")
    if not record.external_url:
        raise HTTPException(status_code=400, detail="This record has no Grants.gov link to enrich from.")

    try:
        opp_id = record.external_url.rstrip("/").split("/")[-1]
        detail = await funding.fetch_grants_gov_detail(opp_id)
        synopsis = detail.get("synopsis") or {}
        raw_text = _html_to_text(synopsis.get("synopsisDesc") or "") or record.program_title
        parsed   = await parser.parse(raw_text)
        template = builder.build(parsed)

        record.raw_text = raw_text[:50000]
        record.parsed_template = template.model_dump(mode="json")
        record.total_page_limit = template.total_page_limit
        if template.deadline:
            record.deadline = template.deadline
        record.eligibility_summary = ", ".join(
            a.get("description", "") for a in (synopsis.get("applicantTypes") or []) if a.get("description")
        ) or record.eligibility_summary
        try:
            record.estimated_award_floor = float(synopsis.get("awardFloor")) if synopsis.get("awardFloor") else record.estimated_award_floor
            record.estimated_award_ceiling = float(synopsis.get("awardCeiling")) if synopsis.get("awardCeiling") else record.estimated_award_ceiling
        except (TypeError, ValueError):
            pass
        await db.flush()
    except HTTPException:
        raise
    except Exception as exc:
        raise _clean_parse_error(exc)

    return FOATemplate(**record.parsed_template)
