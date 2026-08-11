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
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from config import settings
from database import get_db
from models.db_models import Award, FOARecord, Proposal, User
from models.schemas import (
    BidNoGoRequest, BulkAnalyzeRequest, BulkAnalyzeResponseOut, BulkAnalyzeResultOut,
    BulkRankRequest, BulkRankResponseOut, BulkRankResultOut, FOARecordOut, FOATemplate,
    FOAUploadResponse, PipelineStageEventOut, PipelineStageUpdateRequest,
)
from routers.auth import get_current_user
from routers.organizations import _assert_member, _assert_permission
from engines.foa_parser import FOAParserEngine
from engines.template_builder import FOATemplateBuilderEngine
from engines.funding_intelligence_engine import FundingIntelligenceEngine
from engines.service_catalog_engine import ServiceCatalogEngine
from engines.credit_engine import InsufficientCreditsError
from engines.company_profile import get_org_context
from engines.fit_score_engine import FitScoreResult, score_opportunity
from engines import usage_tracking

router = APIRouter()
parser  = FOAParserEngine()
builder = FOATemplateBuilderEngine()
funding = FundingIntelligenceEngine()
catalog_engine = ServiceCatalogEngine()


async def _to_foa_out(record: FOARecord, db: AsyncSession, fit: Optional[FitScoreResult] = None) -> FOARecordOut:
    """
    Version 3.0 architecture upgrade, Phase 14 (Renewal Loop Closure) — now
    async so it can resolve `originating_award_id` into a human-readable
    label/link target for the (rare) records created as a renewal. This is
    a single extra query only when that field is set — the overwhelming
    majority of FOARecords have it None and pay no extra cost.

    `fit` (Funding Opportunity Intelligence, Phase 2) is precomputed by the
    caller — see `list_pipeline` below — because it depends on the
    Funding Intelligence Profile for the *request's* scope (personal vs. a
    given org), which is loaded once per request, not once per record.
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
        eligibility_summary=record.eligibility_summary, ai_summary=record.ai_summary, pipeline_stage=record.pipeline_stage,
        bid_no_go_decision=record.bid_no_go_decision, bid_no_go_rationale=record.bid_no_go_rationale,
        assigned_to=record.assigned_to, uploaded_by=record.uploaded_by, last_synced_at=record.last_synced_at,
        created_at=record.created_at, has_parsed_template=record.parsed_template is not None,
        originating_award_id=record.originating_award_id, originating_proposal_id=originating_proposal_id,
        originating_award_label=originating_award_label, renewal_notes=record.renewal_notes,
        intelligence_report=record.intelligence_report, eligibility_status=record.eligibility_status,
        complexity=record.complexity, attractiveness=record.attractiveness,
        attractiveness_reason=record.attractiveness_reason,
        fit_score=fit.overall_score if fit else None,
        fit_bucket=fit.bucket if fit else None,
        fit_recommendation=fit.recommendation if fit else None,
        fit_recommendation_reason=fit.recommendation_reason if fit else None,
        fit_categories=[c.to_dict() for c in fit.categories] if fit else None,
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


# ── Manual-add source grouping (upload / parse-text / parse-url) ────────────
#
# Grants.gov/SAM.gov sync already tags every record with `source` so
# results can be grouped/filtered by where they came from. Manual adds
# (upload a file, paste text, paste a URL) always collapsed into a single
# generic "manual" bucket regardless of what was actually added — a state
# grants portal RFP, a private foundation's call for proposals, and a World
# Bank/UN procurement notice a user found and pasted in all looked
# identical. There's no free, reliable API for any of those three (state/
# local funding has no Grants.gov equivalent; Candid's foundation API is a
# paid product; World Bank/UNGM only expose raw historical/procurement
# archives with no working "currently open" filter — see
# docs/ARCHITECTURE.md's funding-sources section), so rather than build a
# sync integration against data that isn't reliably filterable, the
# opportunity is on making the manual path fast and properly categorized.
MANUAL_SOURCE_TYPES = {"manual", "state_local", "foundation", "international"}


async def _resolve_manual_source(
    db: AsyncSession, current_user: User, org_id: Optional[str], source_type: Optional[str],
) -> tuple:
    """Shared validation for the three manual-add endpoints below.
    `source_type` groups a manually-added opportunity the same way `source`
    already groups synced ones (grants_gov/sam_gov). `org_id`, when given,
    shares the new record into that org's pipeline the same way sync's
    org_id already does — gated by the same "manage_watchlists" permission
    sync itself requires for org-scoped writes, so this doesn't open a
    looser write path than the one that already exists. Returns
    (org_id, resolved_source)."""
    if source_type and source_type not in MANUAL_SOURCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source_type '{source_type}'. Use one of: {', '.join(sorted(MANUAL_SOURCE_TYPES))}.",
        )
    if org_id:
        await _assert_permission(org_id, current_user.id, "manage_watchlists", db)
    return org_id, (source_type or "manual")


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
    org_id: Optional[str] = Form(None),
    source_type: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload a FOA PDF, DOCX, or TXT file. Parses it and builds a dynamic template.

    `source_type` (manual/state_local/foundation/international — see
    `_resolve_manual_source` above) groups this opportunity the same way
    Grants.gov/SAM.gov sync's `source` field already groups synced ones.
    `org_id` shares it straight into that org's pipeline instead of the
    caller's personal library, gated by the same permission org-scoped
    sync writes already require.
    """
    org_id, resolved_source = await _resolve_manual_source(db, current_user, org_id, source_type)
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
            org_id=org_id,
            source=resolved_source,
            agency=template.agency.value,
            program_title=template.program_title,
            solicitation_number=template.solicitation_number,
            phase=template.phase.value,
            raw_text=raw_text[:50000],
            parsed_template=template.model_dump(mode="json"),
            total_page_limit=template.total_page_limit,
            deadline=template.deadline,
            uploaded_by=current_user.id,
            ai_summary=template.summary,
            eligibility_summary=template.eligibility_summary,
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
    org_id: Optional[str] = Body(None, embed=True),
    source_type: Optional[str] = Body(None, embed=True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Parse pasted FOA text directly (no file upload needed). See
    `upload_foa` above for what `org_id`/`source_type` do."""
    org_id, resolved_source = await _resolve_manual_source(db, current_user, org_id, source_type)
    if not text or len(text.strip()) < 50:
        raise HTTPException(status_code=400, detail="Please paste at least 50 characters of FOA text.")
    try:
        parsed   = await parser.parse(text.strip())
        template = builder.build(parsed)

        record = FOARecord(
            id=str(uuid.uuid4()),
            org_id=org_id,
            source=resolved_source,
            agency=template.agency.value,
            program_title=template.program_title,
            solicitation_number=template.solicitation_number,
            phase=template.phase.value,
            raw_text=text[:50000],
            parsed_template=template.model_dump(mode="json"),
            total_page_limit=template.total_page_limit,
            deadline=template.deadline,
            uploaded_by=current_user.id,
            ai_summary=template.summary,
            eligibility_summary=template.eligibility_summary,
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
    org_id: Optional[str] = Body(None, embed=True),
    source_type: Optional[str] = Body(None, embed=True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch a URL (grants.gov, agency site, PDF link, etc.) and parse it as
    a FOA. See `upload_foa` above for what `org_id`/`source_type` do."""
    org_id, resolved_source = await _resolve_manual_source(db, current_user, org_id, source_type)
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
            org_id=org_id,
            source=resolved_source,
            agency=template.agency.value,
            program_title=template.program_title,
            solicitation_number=template.solicitation_number,
            phase=template.phase.value,
            raw_text=raw_text[:50_000],
            parsed_template=template.model_dump(mode="json"),
            total_page_limit=template.total_page_limit,
            deadline=template.deadline,
            uploaded_by=current_user.id,
            ai_summary=template.summary,
            eligibility_summary=template.eligibility_summary,
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
    keyword: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """
    The pipeline view (PRD §15) — distinct from the legacy `GET /` list
    above, which only ever returned the current user's own uploads as bare
    dicts. This returns the full FOARecordOut shape (pipeline stage,
    Bid/No-Go, sync metadata) and, when `org_id` is given, the whole org's
    shared pipeline rather than just "my" records.

    `keyword` (added alongside Funding Opportunity Intelligence Phase 1's
    keyword-filter fix) is a non-destructive substring filter over
    already-synced records — NOT the same as POST /funding/sync's
    `keyword`, which fetches new records from Grants.gov/SAM.gov. See
    FundingIntelligenceEngine.list_pipeline()'s docstring for the
    distinction; this just threads the param through.

    Funding Opportunity Intelligence, Phase 2 — each record also gets a
    Fit Score (see engines/fit_score_engine.py) computed against whichever
    Funding Intelligence Profile matches this request's scope (the org's
    shared profile when `org_id` is given, else the caller's personal
    one). The profile is loaded once for the whole list, not once per
    record. If no profile exists yet, every record's fit fields are simply
    None — no AI call, no extra cost either way.
    """
    if org_id:
        await _assert_member(org_id, current_user.id, db)
    records = await funding.list_pipeline(
        db, org_id=org_id, uploaded_by=None if org_id else current_user.id,
        pipeline_stage=pipeline_stage, source=source, assigned_to=assigned_to,
        keyword=keyword,
    )
    profile = await get_org_context(db, user_id=current_user.id, org_id=org_id)
    return [await _to_foa_out(r, db, fit=score_opportunity(r, profile)) for r in records]


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
    """
    The FOA Library list — kept as a lightweight bare-dict response (not
    the full FOARecordOut) since it can return hundreds of rows. Version
    3.0 architecture upgrade, Phase 16 (cont'd): added `ai_summary`,
    `eligibility_summary`, and `has_parsed_template` so each card can show
    an AI-generated summary/eligibility read before the user clicks "Use
    this FOA" — these are cheap scalar columns, not the full
    `parsed_template` JSON blob, so this stays a fast query even at scale.
    """
    result = await db.execute(
        select(FOARecord.id, FOARecord.program_title, FOARecord.agency,
               FOARecord.phase, FOARecord.deadline, FOARecord.created_at,
               FOARecord.ai_summary, FOARecord.eligibility_summary,
               FOARecord.parsed_template, FOARecord.estimated_award_floor,
               FOARecord.estimated_award_ceiling, FOARecord.source,
               FOARecord.external_url, FOARecord.org_id,
               FOARecord.intelligence_report, FOARecord.eligibility_status,
               FOARecord.complexity, FOARecord.attractiveness,
               FOARecord.attractiveness_reason)
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
            "ai_summary": r.ai_summary,
            "eligibility_summary": r.eligibility_summary,
            "has_parsed_template": r.parsed_template is not None,
            "estimated_award_floor": r.estimated_award_floor,
            "estimated_award_ceiling": r.estimated_award_ceiling,
            "source": r.source,
            "external_url": r.external_url,
            # Phase 2 — On-Demand AI Services Marketplace: the frontend uses
            # this to decide whether "Generate AI Summary" is free (personal
            # record) or a priced service requiring a price confirmation
            # (org-shared record). See foa.tsx's FOACard.
            "org_id": r.org_id,
            # Funding Opportunity Intelligence, Phase 1 — the structured
            # report (once analyzed) plus cached indicators so the card can
            # show eligibility/complexity/attractiveness without a second
            # request. See engines/foa_parser.py::analyze_opportunity.
            "intelligence_report": r.intelligence_report,
            "eligibility_status": r.eligibility_status,
            "complexity": r.complexity,
            "attractiveness": r.attractiveness,
            "attractiveness_reason": r.attractiveness_reason,
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
        # Prefer Grants.gov's own structured applicant-type list when
        # present (more precise than GPT's read of the synopsis text);
        # fall back to the AI-generated eligibility_summary from this same
        # parse, then to whatever was already on the record.
        record.eligibility_summary = ", ".join(
            a.get("description", "") for a in (synopsis.get("applicantTypes") or []) if a.get("description")
        ) or template.eligibility_summary or record.eligibility_summary
        record.ai_summary = template.summary or record.ai_summary
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


@router.post("/{foa_id}/summarize", response_model=FOARecordOut)
async def summarize_opportunity(
    foa_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """
    "Analyze Opportunity" — Funding Opportunity Intelligence, Phase 1 (Grant
    Finding Workspace Upgrade). Endpoint path/name kept as `/summarize` for
    backward compatibility with existing clients and the already-built
    price-quote/confirm flow (foa.tsx, pipeline.tsx), but the underlying
    service now produces a full structured pursuit-decision report instead
    of a two-field summary — see engines/foa_parser.py::analyze_opportunity.
    Superseded predecessor: Version 3.0 architecture upgrade, Phase 16
    (cont'd), which only backfilled `ai_summary`/`eligibility_summary`.

    1. If `intelligence_report` is already set, this is a no-op (returns
       as-is) — calling it repeatedly never re-spends AI credits. (Records
       analyzed before this upgrade shipped only have the old
       `ai_summary`/`eligibility_summary` fields and no
       `intelligence_report` yet, so they'll be upgraded to the full report
       the next time a user opens them — this is a one-time re-spend for
       those specific opportunities, consistent with this codebase's
       existing "backfill on next open" precedent rather than a batch
       re-processing job.)
    2. If `raw_text` is present (every upload/parse-text/parse-url record
       has this), run `FOAParserEngine.analyze_opportunity()` against it.
    3. If there's no raw_text but this is a Grants.gov-synced record with a
       link, fetch the synopsis, run it through the full parser (mirrors
       `enrich`, populating `parsed_template` too), then analyze it.
    4. Otherwise, there's nothing to analyze — 400.

    The actual work is in `_analyze_single_opportunity()` below — Phase 3
    §4.5 (Bulk Opportunity Intelligence) reuses that exact helper in a loop
    for the bulk-analyze endpoint, so single-record and bulk analysis can
    never silently diverge in behavior.
    """
    record = await _get_foa_or_404(db, foa_id)
    await _assert_foa_access(record, current_user, db)
    await _analyze_single_opportunity(db, record, current_user)
    return await _to_foa_out(record, db)


async def _analyze_single_opportunity(db: AsyncSession, record: FOARecord, current_user: User) -> None:
    """
    Core "Analyze Opportunity" logic, extracted verbatim from
    `summarize_opportunity` above (Phase 3 §4.5 — Bulk Opportunity
    Intelligence) so the single-record endpoint and the new bulk-analyze
    endpoint run the identical charge/parse/write sequence rather than two
    copies that can drift. Mutates `record` in place and raises
    HTTPException on any failure (idempotent no-op, 402 insufficient
    credits, 400 no content, or a cleaned parse error) — the single
    endpoint lets that propagate directly; the bulk endpoint catches it
    per-item so one failed opportunity doesn't abort the whole batch.
    Caller is responsible for `_assert_foa_access` beforehand.
    """
    if record.intelligence_report:
        return  # idempotent no-op — never re-spend

    # Phase 2 — On-Demand AI Services Marketplace (Enterprise Pricing spec
    # §9.2): Grant Opportunity Analysis is a centrally-priced service.
    # Charging is opt-in per org_id — the same precedent CreditEngine's
    # existing metering already established (see credit_engine.py's design
    # notes: "a proposal generated with no org_id is not charged anything")
    # — so a personal, non-org-shared FOA record stays completely free to
    # analyze, exactly as before this feature existed. For an org-shared
    # record, this also requires purchase_ai_services (owner/editor),
    # stricter than the plain view/member access _assert_foa_access already
    # granted by the caller, since this specific action spends the org's
    # shared complimentary allowance or paid AI Services balance.
    price_cents_charged = 0
    if record.org_id:
        await _assert_permission(record.org_id, current_user.id, "purchase_ai_services", db)
        try:
            txn = await catalog_engine.consume(
                db, record.org_id, current_user.id, "grant_opportunity_analysis",
                reference={"foa_id": record.id},
            )
            price_cents_charged = txn.price_cents
        except InsufficientCreditsError as exc:
            raise HTTPException(status_code=402, detail=str(exc))

    try:
        if not record.raw_text and record.source == "grants_gov" and record.external_url:
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
            ) or template.eligibility_summary or record.eligibility_summary

        if not record.raw_text:
            raise HTTPException(
                status_code=400,
                detail="No content available to analyze for this opportunity.",
            )

        result = await parser.analyze_opportunity(record.raw_text)
        record.intelligence_report = result["report"]
        record.ai_summary = result.get("summary") or record.ai_summary
        record.eligibility_summary = result.get("eligibility_summary") or record.eligibility_summary
        record.eligibility_status = result.get("eligibility_status")
        record.complexity = result.get("complexity")
        record.attractiveness = result.get("attractiveness")
        record.attractiveness_reason = result.get("attractiveness_reason")

        # Phase 3 §4.7 — Administrator-Only Engineering Economics.
        usage = result.get("_usage") or {}
        await usage_tracking.record_usage(
            db, operation="grant_opportunity_analysis", model=usage.get("model", settings.OPENAI_MODEL),
            prompt_tokens=usage.get("prompt_tokens", 0), completion_tokens=usage.get("completion_tokens", 0),
            org_id=record.org_id, user_id=current_user.id,
            price_cents_charged=price_cents_charged, reference={"foa_id": record.id},
        )

        await db.flush()
        await db.refresh(record)
    except HTTPException:
        raise
    except Exception as exc:
        raise _clean_parse_error(exc)


# ── Bulk Opportunity Intelligence (Phase 3 §4.5) ──────────────────────────────
# "Allow Team, Organization and Enterprise users to qualify sets of
# opportunities. Use low-cost ranking first, then deeper analysis only on
# selected high-potential opportunities. Architect for separately priced
# bulk analysis or institutional entitlements." Deliberately built as two
# calls, not one: /bulk/rank is free (reuses the same deterministic Fit
# Score every card already shows — engines/fit_score_engine.py), so a team
# can rank a large set of newly-synced opportunities at zero cost, then
# /bulk/analyze spends real AI credits (via the exact same
# `_analyze_single_opportunity` helper the single-record "Analyze
# Opportunity" button uses, looped) only on the subset the team actually
# selects after seeing the ranking — never the whole set automatically.
# "Separately priced... or institutional entitlements" is satisfied for
# free by reusing `ServiceCatalogEngine.consume()` per item: it already
# checks each org's complimentary allowance/entitlement before falling
# back to paid credits, so nothing bulk-specific needed inventing on the
# pricing side.

BULK_ALLOWED_PLANS = ("team", "organization", "enterprise")
BULK_MAX_ITEMS = 50  # guards against a single request looping 500+ paid AI calls


async def _assert_bulk_tier(db: AsyncSession, org_id: str) -> None:
    """Bulk qualification is a Team/Organization/Enterprise plan feature —
    the one place in this codebase that gates a feature by `Organization.
    plan` rather than by role/permission alone (see rbac.py's docstring:
    everything else here is enforced by "org_id present + role
    permission," with no prior plan-tier gate). Reuses
    ServiceCatalogEngine._get_org_plan() rather than querying
    Organization.plan directly, so this stays in sync with whatever that
    engine considers the org's plan (defaults to "free" the same way)."""
    plan = await catalog_engine._get_org_plan(db, org_id)
    if plan not in BULK_ALLOWED_PLANS:
        raise HTTPException(
            status_code=403,
            detail="Bulk Opportunity Intelligence is available on Team, Organization, and Enterprise plans.",
        )


@router.post("/bulk/rank", response_model=BulkRankResponseOut)
async def bulk_rank_opportunities(
    body: BulkRankRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Step 1 of Bulk Opportunity Intelligence — free. Ranks the given
    opportunities by the org's existing Fit Score, so a team can qualify a
    large synced batch without spending anything before deciding which
    ones are worth the paid deep-analysis step below."""
    # Auth/tier checks run BEFORE the empty-list/over-limit short-circuits
    # below — see the identical note in bulk_analyze_opportunities.
    if not body.org_id:
        raise HTTPException(status_code=400, detail="Bulk qualification requires an organization context.")
    await _assert_member(body.org_id, current_user.id, db)
    await _assert_bulk_tier(db, body.org_id)

    if not body.foa_ids:
        return BulkRankResponseOut(results=[], disclaimer="")
    if len(body.foa_ids) > BULK_MAX_ITEMS:
        raise HTTPException(status_code=400, detail=f"Bulk requests are limited to {BULK_MAX_ITEMS} opportunities at a time.")

    result = await db.execute(
        select(FOARecord).where(FOARecord.id.in_(body.foa_ids), FOARecord.org_id == body.org_id)
    )
    records = list(result.scalars().all())
    profile = await get_org_context(db, org_id=body.org_id)

    ranked: List[BulkRankResultOut] = []
    for r in records:
        fit = score_opportunity(r, profile)
        ranked.append(BulkRankResultOut(
            foa_id=r.id, program_title=r.program_title, agency=r.agency, pipeline_stage=r.pipeline_stage,
            fit_score=fit.overall_score if fit else None,
            fit_bucket=fit.bucket if fit else None,
            fit_recommendation=fit.recommendation if fit else None,
        ))
    ranked.sort(key=lambda x: x.fit_score if x.fit_score is not None else -1, reverse=True)

    return BulkRankResponseOut(
        results=ranked,
        disclaimer=(
            "Free ranking pass only — no AI credits spent. Fit Score is a deterministic "
            "comparison against your Funding Intelligence Profile, never a win-probability "
            "estimate. Select the opportunities worth a deeper look before running paid analysis."
        ),
    )


@router.post("/bulk/analyze", response_model=BulkAnalyzeResponseOut)
async def bulk_analyze_opportunities(
    body: BulkAnalyzeRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Step 2 of Bulk Opportunity Intelligence — paid, only on the
    opportunities the caller explicitly selected after seeing /bulk/rank's
    free results. Loops `_analyze_single_opportunity` (the exact same
    logic the single-record "Analyze Opportunity" endpoint runs) one FOA
    at a time; each item's charge/parse failure is caught individually so
    one bad opportunity (e.g. no content available) doesn't abort or
    partially-charge for the rest of the batch."""
    # Auth/tier checks run BEFORE the empty-list/over-limit short-circuits
    # below on purpose — an empty or absent foa_ids must never let an
    # unauthorized caller (wrong org, wrong role, wrong plan) skip past
    # authorization just because there was nothing to actually charge for.
    if not body.org_id:
        raise HTTPException(status_code=400, detail="Bulk qualification requires an organization context.")
    await _assert_permission(body.org_id, current_user.id, "purchase_ai_services", db)
    await _assert_bulk_tier(db, body.org_id)

    if not body.foa_ids:
        return BulkAnalyzeResponseOut(results=[], analyzed_count=0, failed_count=0)
    if len(body.foa_ids) > BULK_MAX_ITEMS:
        raise HTTPException(status_code=400, detail=f"Bulk requests are limited to {BULK_MAX_ITEMS} opportunities at a time.")

    result = await db.execute(
        select(FOARecord).where(FOARecord.id.in_(body.foa_ids), FOARecord.org_id == body.org_id)
    )
    records = {r.id: r for r in result.scalars().all()}

    results: List[BulkAnalyzeResultOut] = []
    analyzed_count = 0
    failed_count = 0
    for foa_id in body.foa_ids:
        record = records.get(foa_id)
        if not record:
            results.append(BulkAnalyzeResultOut(
                foa_id=foa_id, program_title="(not found)", success=False, error="Opportunity not found in this organization.",
            ))
            failed_count += 1
            continue

        already_analyzed = bool(record.intelligence_report)
        try:
            await _analyze_single_opportunity(db, record, current_user)
            results.append(BulkAnalyzeResultOut(
                foa_id=record.id, program_title=record.program_title, success=True, already_analyzed=already_analyzed,
            ))
            analyzed_count += 1
        except HTTPException as exc:
            results.append(BulkAnalyzeResultOut(
                foa_id=record.id, program_title=record.program_title, success=False, error=str(exc.detail),
            ))
            failed_count += 1

    return BulkAnalyzeResponseOut(results=results, analyzed_count=analyzed_count, failed_count=failed_count)
