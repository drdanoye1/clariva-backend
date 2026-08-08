"""
Award & Project Management router (Clariva Enterprise™ PRD §16-17, Phase 5).

An Award is 1:1 with a Proposal, so access control is exactly "can this
user edit/view this proposal" — resolved via workspace_access.py's
resolve_proposal_access(), the same resolver Phase 3's collaboration.py and
documents_library.py already use. There is no separate Award-specific RBAC
check: owner and org editor/owner roles can edit (rbac.py grants both
"manage_awards" — kept for future finer-grained gating, not actively
branched on here), org viewers and guests can only view.

Amendment decisions are NOT made here — they go through the existing
generic `POST /api/v1/approvals/{id}/decide` endpoint
(routers/collaboration.py), which also applies the amendment's
effective_changes to the Award (see
engines/collaboration_engine.py::decide_approval_request).
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import storage
from database import get_db
from models.db_models import Award, Document, OrgMembership, StoredFile, User, new_uuid
from models.schemas import (
    ActivateProjectRequest, AwardAmendmentCreate, AwardAmendmentOut, AwardCloseoutOut,
    AwardCloseoutRequest, AwardComplianceItemCreate, AwardComplianceItemOut,
    AwardComplianceItemUpdate, AwardConditionCreate, AwardConditionOut,
    AwardConditionUpdate, AwardCreate, AwardExpenditureCreate, AwardExpenditureOut,
    AwardIntelligenceApplyRequest, AwardIntelligenceDraftOut, AwardOut,
    AwardPerformanceRecordCreate, AwardPerformanceRecordOut, AwardReportOut,
    AwardUpdate, BudgetStatusOut, DocumentOut, FOARecordOut, PlannedVsActualOut,
    ProjectBaselineOut, ProjectExecutionStatusOut, ProjectIssueCreate, ProjectIssueOut,
    ProjectIssueUpdate, QuickAwardIntakeRequest, RenewalCreate, ReportNarrativeRequest,
    StoredFileOut,
)
from routers.auth import get_current_user
from routers.organizations import _assert_member, _assert_permission
from routers.documents_library import _to_document_out
from routers.extract import _extract_text_from_docx, _extract_text_from_pdf
from routers.budget import _extract_budget_from_text, apply_budget_dict
from workspace_access import ProposalAccess, assert_can_edit, assert_can_view
from engines.award_engine import AwardEngine, _naive
from engines.credit_engine import CreditEngine, GENERATION_COST, debit_or_402
from engines.document_library_engine import DocumentLibraryEngine
from engines.scope_of_work_engine import ScopeOfWorkEngine

router = APIRouter()
engine = AwardEngine()
credit_engine = CreditEngine()
document_engine = DocumentLibraryEngine()
scope_engine = ScopeOfWorkEngine()


def _to_award_out(award: Award, proposal_title: Optional[str] = None) -> AwardOut:
    return AwardOut(
        id=award.id, proposal_id=award.proposal_id, foa_id=award.foa_id, org_id=award.org_id,
        budget_record_id=award.budget_record_id, award_number=award.award_number,
        funding_agency=award.funding_agency,
        period_of_performance_start=award.period_of_performance_start,
        period_of_performance_end=award.period_of_performance_end,
        total_award_value=award.total_award_value, terms=award.terms, status=award.status,
        award_status=award.award_status,
        created_by=award.created_by, created_at=award.created_at, updated_at=award.updated_at,
        proposal_title=proposal_title,
    )


async def _get_award_and_access(award_id: str, current_user: User, db: AsyncSession, require_edit: bool) -> tuple[Award, ProposalAccess]:
    award = await engine.get_award_or_404(db, award_id)
    if require_edit:
        access = await assert_can_edit(award.proposal_id, current_user.id, db)
    else:
        access = await assert_can_view(award.proposal_id, current_user.id, db)
    return award, access


async def _meter(org_id: Optional[str], user_id: str, db: AsyncSession, reason: str) -> None:
    """See scope_of_work.py::_meter — optional/additive credit metering for
    AI calls made on behalf of an org; personal/unshared use stays free."""
    if org_id:
        await _assert_member(org_id, user_id, db)
        await debit_or_402(credit_engine, db, org_id, user_id, GENERATION_COST, reason=reason)


# ── Awards ───────────────────────────────────────────────────────────────────

@router.get("/mine", response_model=List[AwardOut])
async def list_my_awards(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    memberships = await db.execute(select(OrgMembership).where(OrgMembership.user_id == current_user.id))
    org_ids = [m.org_id for m in memberships.scalars().all()]
    awards = await engine.list_awards_for_user(db, current_user.id, org_ids)
    return [_to_award_out(a) for a in awards]


@router.post("", response_model=AwardOut)
async def create_award(
    payload: AwardCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    access = await assert_can_edit(payload.proposal_id, current_user.id, db)
    award = await engine.create_award(
        db, payload.proposal_id, payload.model_dump(exclude={"proposal_id"}),
        created_by=current_user.id, org_id=access.org_id,
    )
    await db.commit()
    return _to_award_out(award, proposal_title=access.proposal.title)


@router.post("/intake", response_model=AwardOut)
async def quick_award_intake(
    payload: QuickAwardIntakeRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """'Quick Award Intake' (Version 3.0 upgrade, Phase 15) — lets a customer
    who already has a signed/funded award get it into the system without
    ever having gone through Pre-Award. See QuickAwardIntakeRequest's and
    AwardEngine.create_award_from_intake()'s docstrings for the shell-Proposal
    mechanism this uses instead of changing Award.proposal_id's required 1:1
    relationship. Gated the same way personal (unshared) proposal creation
    always has been — no org membership required — unless org_id is given,
    in which case it's gated like every other org-level award action
    (manage_awards, via _assert_permission)."""
    if payload.org_id:
        await _assert_permission(payload.org_id, current_user.id, "manage_awards", db)
    award = await engine.create_award_from_intake(db, payload.model_dump(), created_by=current_user.id)
    await db.commit()
    return _to_award_out(award, proposal_title=payload.title)


# Document types Quick Award Intake can attach — the customer's own signed
# copy of the proposal that won the award, and/or the funder's award
# notice/letter. Kept as a small closed set (rather than a free-text label)
# so the frontend can offer exactly two clearly-labeled upload slots instead
# of a single generic one.
_INTAKE_DOC_LABELS = {"funded_proposal": "Approved/Funded Proposal", "award_notice": "Award Notice"}
_INTAKE_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
}


@router.post("/{award_id}/intake/document", response_model=DocumentOut)
async def intake_award_document(
    award_id: str, org_id: str = Form(...), file: UploadFile = File(...),
    doc_type: str = Form("award_notice"),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Attaches a document during Quick Award Intake — either the customer's
    approved/funded proposal (doc_type="funded_proposal") or the funder's
    award notice/letter (doc_type="award_notice", the default). Callable
    twice per award, once per doc_type, since a customer may have either or
    both on hand.

    This endpoint always extracts the document's TEXT content into Document
    Library (for versioning/semantic search — same as before Phase C). As of
    Version 3.0's "Real File Storage" Phase C, it ALSO uploads the original
    file bytes to R2 and records a StoredFile row (object_type=
    "award_document", object_id=award_id), so the original PDF/DOCX is
    re-downloadable via GET /awards/{award_id}/files below — not just its
    extracted text. If R2 isn't configured (storage.upload_file raises a 503),
    this degrades gracefully: the Document Library text extraction above has
    already succeeded and is kept; only the original-file preservation is
    skipped, matching this codebase's existing pattern of optional
    integrations no-op'ing rather than failing the whole request.

    org_id is required (not optional, unlike POST /intake above) because
    Document.org_id is NOT NULL by design (see document_library_engine.py's
    module docstring) — a solo user with no organization can complete Quick
    Award Intake itself but cannot attach a document until they belong to
    one; the frontend should skip/hide this step for orgless users rather
    than surface this 422/403 to them.
    """
    if doc_type not in _INTAKE_DOC_LABELS:
        raise HTTPException(status_code=422, detail=f"doc_type must be one of {sorted(_INTAKE_DOC_LABELS)}.")

    _, access = await _get_award_and_access(award_id, current_user, db, require_edit=True)
    await _assert_permission(org_id, current_user.id, "manage_documents", db)

    content = await file.read()
    filename = file.filename or "document"
    lower = filename.lower()
    if lower.endswith(".pdf"):
        text = _extract_text_from_pdf(content)
    elif lower.endswith(".docx"):
        text = _extract_text_from_docx(content)
    elif lower.endswith(".txt"):
        text = content.decode("utf-8", errors="ignore")
    else:
        raise HTTPException(status_code=422, detail="Unsupported file type — upload a PDF, DOCX, or plain text file.")

    doc = await document_engine.create_document(
        db, org_id, current_user.id,
        {
            "title": f"{_INTAKE_DOC_LABELS[doc_type]} — {filename}",
            "library_type": doc_type,
            "proposal_id": access.proposal.id,
            "content": text,
            "format": "txt",
            "change_note": "Imported via Quick Award Intake",
        },
    )

    # Phase C: also preserve the original file bytes in R2 so they can be
    # re-downloaded later (GET /awards/{award_id}/files) — not just the
    # extracted text saved above. Best-effort: if R2 isn't configured, the
    # Document Library entry already succeeded and is kept regardless.
    ext = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
    content_type = _INTAKE_CONTENT_TYPES.get(ext, "application/octet-stream")
    try:
        storage_key = await storage.upload_file(org_id, f"award_document_{doc_type}", content, filename, content_type)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        storage_key = None
    if storage_key:
        db.add(StoredFile(
            id=new_uuid(), org_id=org_id, object_type="award_document", object_id=award_id,
            storage_key=storage_key, original_filename=filename, content_type=content_type,
            size_bytes=len(content), checksum=storage.sha256_hex(content), created_by=current_user.id,
        ))

    await db.commit()
    return await _to_document_out(db, doc)


@router.get("/{award_id}/files", response_model=List[StoredFileOut])
async def list_award_files(
    award_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Browsable list of original files preserved for this award (currently
    just Quick Award Intake uploads — see intake_award_document above), each
    with a fresh presigned download URL. View-level access only (matches
    every other read on this award)."""
    await _get_award_and_access(award_id, current_user, db, require_edit=False)
    result = await db.execute(
        select(StoredFile)
        .where(StoredFile.object_type == "award_document", StoredFile.object_id == award_id)
        .order_by(StoredFile.created_at.desc())
    )
    files = result.scalars().all()
    out = []
    for f in files:
        download_url = await storage.get_download_url(f.storage_key, filename=f.original_filename)
        out.append(StoredFileOut(
            id=f.id, original_filename=f.original_filename, content_type=f.content_type,
            size_bytes=f.size_bytes, created_at=f.created_at, download_url=download_url,
        ))
    return out


@router.post("/{award_id}/extract-intelligence", response_model=AwardIntelligenceDraftOut)
async def extract_award_intelligence(
    award_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Version 3.0 upgrade, Phase D — 'Award Intake Intelligence'. Reads
    whatever Quick Award Intake documents have been uploaded so far (see
    intake_award_document above) and proposes award value/dates, a work
    breakdown, and budget line items via AI — filling the gap left by
    skipping Pre-Award entirely (see engines/award_engine.py's Award Intake
    Intelligence section for why activate_award()'s baseline snapshot is
    otherwise permanently empty for these awards). Returns an unpersisted
    draft for review — nothing is saved until POST /awards/{award_id}/
    apply-intelligence. Safe to call again after uploading another
    document: each call re-reads everything currently attached, it doesn't
    accumulate incrementally."""
    award, access = await _get_award_and_access(award_id, current_user, db, require_edit=True)
    document_text = await engine.gather_intake_document_text(db, access.proposal.id)
    if not document_text.strip():
        raise HTTPException(
            status_code=400,
            detail="No documents have been uploaded for this award yet — attach the award notice or funded proposal first.",
        )
    await _meter(award.org_id, current_user.id, db, reason=f"award:extract_intelligence:{award_id}")

    scope_result = await engine.extract_scope_and_award_fields(access.proposal, document_text)
    # Budget extraction is best-effort and separate from scope/award fields
    # above — a short award notice letter often has nothing budget-shaped in
    # it at all, and that shouldn't block the (usually more reliable)
    # award value/dates/work-plan extraction from returning.
    try:
        budget_result = await _extract_budget_from_text(document_text)
    except HTTPException:
        budget_result = None

    doc_count_result = await db.execute(
        select(Document).where(
            Document.proposal_id == access.proposal.id,
            Document.library_type.in_(["funded_proposal", "award_notice"]),
            Document.status == "active",
        )
    )
    source_document_count = len(doc_count_result.scalars().all())

    return AwardIntelligenceDraftOut(
        total_award_value=scope_result.get("total_award_value"),
        period_of_performance_start=scope_result.get("period_of_performance_start"),
        period_of_performance_end=scope_result.get("period_of_performance_end"),
        work_packages=scope_result.get("work_packages") or [],
        budget=budget_result,
        extraction_notes=scope_result.get("extraction_notes"),
        source_document_count=source_document_count,
    )


@router.post("/{award_id}/apply-intelligence", response_model=AwardOut)
async def apply_award_intelligence(
    award_id: str, body: AwardIntelligenceApplyRequest,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Persists a (possibly hand-edited) draft from POST /awards/{award_id}/
    extract-intelligence. Award value/dates only fill in blanks — never
    overwrite a value already entered at intake or edited manually since, so
    running this is always safe even if the customer already typed in an
    award value themselves. Work packages/budget are additive: calling this
    more than once with overlapping content will duplicate it, the same
    caveat Scope of Work's own 'Derive from Proposal' onramp already has —
    intended for the one-time "just imported this award" moment, not
    repeated re-application."""
    import logging, traceback
    log = logging.getLogger(__name__)

    award, access = await _get_award_and_access(award_id, current_user, db, require_edit=True)
    # Captured now, before any writes/commit below — this is the actual bug
    # that was crashing this endpoint (confirmed via Heroku logs:
    # sqlalchemy.exc.MissingGreenlet). `award`'s own plain columns stay safe
    # to read after commit because database.py sets expire_on_commit=False,
    # but that doesn't extend to reaching into a *different* object
    # (access.proposal) — doing that post-commit forced SQLAlchemy to
    # silently try to refresh it, which requires IO the async ORM can't run
    # outside an awaited call, hence MissingGreenlet. Grabbing the plain
    # string now sidesteps the whole class of bug.
    proposal_title = access.proposal.title

    try:
        if body.total_award_value is not None and award.total_award_value is None:
            award.total_award_value = body.total_award_value
        # _naive() strips tzinfo — the AI extraction returns plain "YYYY-MM-DD"
        # dates with no timezone, and this codebase already has a documented
        # naive-vs-aware datetime footgun around Award.period_of_performance_*
        # (see award_engine.py's _naive() docstring); stripping it here rather
        # than trusting Pydantic's parsed value avoids re-triggering it.
        if body.period_of_performance_start is not None and award.period_of_performance_start is None:
            award.period_of_performance_start = _naive(body.period_of_performance_start)
        if body.period_of_performance_end is not None and award.period_of_performance_end is None:
            award.period_of_performance_end = _naive(body.period_of_performance_end)

        if body.work_packages:
            await scope_engine.apply_generated_work_breakdown(
                db, access.proposal.id, {"work_packages": body.work_packages}
            )

        if body.budget and body.budget.get("extracted"):
            budget_record = await apply_budget_dict(db, access.proposal.id, body.budget["extracted"])
            if not award.budget_record_id:
                award.budget_record_id = budget_record.id

        await db.commit()
        # Root cause of the actual MissingGreenlet (confirmed via Heroku
        # logs): `Award.updated_at` uses onupdate=func.now(), so once this
        # endpoint's edits above make `award` dirty, committing leaves
        # `updated_at` "expired" server-side regardless of
        # expire_on_commit=False — that flag only stops the *session* from
        # blanket-expiring everything, it doesn't cover a column whose new
        # value only the server knows. Reading it unrefreshed in
        # _to_award_out() below forced a synchronous reload, which the async
        # ORM can't do outside an awaited call. This is this codebase's
        # already-documented "Async ORM pitfall" (see credit_engine.py,
        # Phase 1, and the note earlier in this file) — the fix is the same
        # one used everywhere else: refresh after flush/commit, every time.
        await db.refresh(award)
        # Also inside the try: `award`'s own attributes are safe to read
        # post-commit (expire_on_commit=False), but keeping this here means
        # any future surprise on this path gets our detailed error message
        # instead of leaking out as FastAPI's generic, undiagnosable 500 —
        # which is exactly what let the real bug above hide from the
        # detailed logging this try/except was added for in the first place.
        return _to_award_out(award, proposal_title=proposal_title)
    except HTTPException:
        raise
    except Exception as exc:
        log.error("apply-intelligence failed for award %s:\n%s", award_id, traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Could not apply these details: {type(exc).__name__}: {exc}",
        )


@router.get("/by-proposal/{proposal_id}", response_model=AwardOut)
async def get_award_by_proposal(
    proposal_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    access = await assert_can_view(proposal_id, current_user.id, db)
    award = await engine.get_award_by_proposal(db, proposal_id)
    if not award:
        raise HTTPException(status_code=404, detail="This proposal has no award yet.")
    return _to_award_out(award, proposal_title=access.proposal.title)


@router.get("/{award_id}", response_model=AwardOut)
async def get_award(award_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    award, access = await _get_award_and_access(award_id, current_user, db, require_edit=False)
    return _to_award_out(award, proposal_title=access.proposal.title)


@router.patch("/{award_id}", response_model=AwardOut)
async def update_award(
    award_id: str, payload: AwardUpdate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    _, access = await _get_award_and_access(award_id, current_user, db, require_edit=True)
    award = await engine.update_award(db, award_id, payload.model_dump(exclude_unset=True))
    await db.commit()
    return _to_award_out(award, proposal_title=access.proposal.title)


# ── Award Received: activation & baselines (Version 3.0 upgrade, Phase 8) ──
# "Activate Project" — locks the first ProjectBaseline and flips
# award_status from "received" to "active". Gated the same way every other
# award mutation is (assert_can_edit via _get_award_and_access), not by
# rbac.py's "activate_award" permission — see that permission's docstring.

@router.post("/{award_id}/activate", response_model=ProjectBaselineOut)
async def activate_award(
    award_id: str, payload: ActivateProjectRequest = ActivateProjectRequest(),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_award_and_access(award_id, current_user, db, require_edit=True)
    baseline = await engine.activate_award(db, award_id, payload.model_dump(), created_by=current_user.id)
    await db.commit()
    return baseline


@router.post("/{award_id}/baselines/reversion", response_model=ProjectBaselineOut)
async def create_baseline_version(
    award_id: str, payload: ActivateProjectRequest = ActivateProjectRequest(),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Re-baselines an already-active award — e.g. after an approved
    AwardAmendment changed budget, scope, or schedule. A deliberate,
    explicit call rather than automatic on amendment approval: the
    amendment-decision flow (collaboration_engine.py::decide_approval_request)
    applies effective_changes to the live Award directly without importing
    AwardEngine, matching this codebase's "engines interoperate through
    shared models, not each other's classes" convention (see
    award_engine.py's module docstring) — whether re-baselining should be
    automatic-on-approval or a deliberate follow-up action is a UI decision
    left to Phase 9/11, not resolved here."""
    await _get_award_and_access(award_id, current_user, db, require_edit=True)
    baseline = await engine.create_baseline_version(db, award_id, payload.model_dump(), created_by=current_user.id)
    await db.commit()
    return baseline


@router.get("/{award_id}/baselines", response_model=List[ProjectBaselineOut])
async def list_baselines(award_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await _get_award_and_access(award_id, current_user, db, require_edit=False)
    return await engine.list_baselines(db, award_id)


@router.get("/{award_id}/baselines/current", response_model=ProjectBaselineOut)
async def get_current_baseline(award_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await _get_award_and_access(award_id, current_user, db, require_edit=False)
    baseline = await engine.get_current_baseline(db, award_id)
    if not baseline:
        raise HTTPException(status_code=404, detail="This award has not been activated yet — no baseline exists.")
    return baseline


# ── Award Received: sponsor conditions (Version 3.0 upgrade, Phase 8) ──────

@router.post("/{award_id}/conditions", response_model=AwardConditionOut)
async def create_condition(
    award_id: str, payload: AwardConditionCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_award_and_access(award_id, current_user, db, require_edit=True)
    condition = await engine.create_condition(db, award_id, payload.model_dump(), created_by=current_user.id)
    await db.commit()
    return condition


@router.get("/{award_id}/conditions", response_model=List[AwardConditionOut])
async def list_conditions(award_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await _get_award_and_access(award_id, current_user, db, require_edit=False)
    return await engine.list_conditions(db, award_id)


@router.patch("/conditions/{condition_id}", response_model=AwardConditionOut)
async def update_condition(
    condition_id: str, payload: AwardConditionUpdate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    condition = await engine.get_condition_or_404(db, condition_id)
    await _get_award_and_access(condition.award_id, current_user, db, require_edit=True)
    updated = await engine.update_condition(db, condition_id, payload.model_dump(exclude_unset=True), resolved_by=current_user.id)
    await db.commit()
    return updated


# ── Budget administration / burn-rate ───────────────────────────────────────

@router.post("/{award_id}/expenditures", response_model=AwardExpenditureOut)
async def create_expenditure(
    award_id: str, payload: AwardExpenditureCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_award_and_access(award_id, current_user, db, require_edit=True)
    exp = await engine.create_expenditure(db, award_id, payload.model_dump(), recorded_by=current_user.id)
    await db.commit()
    return exp


@router.get("/{award_id}/expenditures", response_model=List[AwardExpenditureOut])
async def list_expenditures(award_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await _get_award_and_access(award_id, current_user, db, require_edit=False)
    return await engine.list_expenditures(db, award_id)


@router.get("/{award_id}/budget-status", response_model=BudgetStatusOut)
async def get_budget_status(award_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await _get_award_and_access(award_id, current_user, db, require_edit=False)
    return await engine.get_budget_status(db, award_id)


@router.get("/{award_id}/planned-vs-actual", response_model=PlannedVsActualOut)
async def get_planned_vs_actual(award_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Version 3.0 upgrade, Phase 10: budget/scope/schedule variance against
    the locked ProjectBaseline, not the live BudgetRecord/ScopeOfWork —
    raises 400 (via the engine) if the award hasn't been activated yet."""
    await _get_award_and_access(award_id, current_user, db, require_edit=False)
    return await engine.get_planned_vs_actual(db, award_id)


# ── Compliance checklist ─────────────────────────────────────────────────────

@router.post("/{award_id}/compliance", response_model=AwardComplianceItemOut)
async def create_compliance_item(
    award_id: str, payload: AwardComplianceItemCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_award_and_access(award_id, current_user, db, require_edit=True)
    item = await engine.create_compliance_item(db, award_id, payload.model_dump())
    await db.commit()
    return item


@router.get("/{award_id}/compliance", response_model=List[AwardComplianceItemOut])
async def list_compliance_items(award_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await _get_award_and_access(award_id, current_user, db, require_edit=False)
    return await engine.list_compliance_items(db, award_id)


@router.patch("/compliance/{item_id}", response_model=AwardComplianceItemOut)
async def update_compliance_item(
    item_id: str, payload: AwardComplianceItemUpdate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    item = await engine.get_compliance_item_or_404(db, item_id)
    await _get_award_and_access(item.award_id, current_user, db, require_edit=True)
    updated = await engine.update_compliance_item(db, item_id, payload.model_dump(exclude_unset=True), completed_by=current_user.id)
    await db.commit()
    return updated


@router.delete("/compliance/{item_id}")
async def delete_compliance_item(item_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    item = await engine.get_compliance_item_or_404(db, item_id)
    await _get_award_and_access(item.award_id, current_user, db, require_edit=True)
    await engine.delete_compliance_item(db, item_id)
    await db.commit()
    return {"deleted": True}


# ── Amendments ───────────────────────────────────────────────────────────────

@router.post("/{award_id}/amendments", response_model=AwardAmendmentOut)
async def create_amendment(
    award_id: str, payload: AwardAmendmentCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    _, access = await _get_award_and_access(award_id, current_user, db, require_edit=True)
    amendment = await engine.create_amendment(db, award_id, payload.model_dump(), requested_by=current_user.id, org_id=access.org_id)
    await db.commit()
    return amendment


@router.get("/{award_id}/amendments", response_model=List[AwardAmendmentOut])
async def list_amendments(award_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await _get_award_and_access(award_id, current_user, db, require_edit=False)
    return await engine.list_amendments(db, award_id)


# ── Issues ───────────────────────────────────────────────────────────────────

@router.post("/{award_id}/issues", response_model=ProjectIssueOut)
async def create_issue(
    award_id: str, payload: ProjectIssueCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_award_and_access(award_id, current_user, db, require_edit=True)
    issue = await engine.create_issue(db, award_id, payload.model_dump(), raised_by=current_user.id)
    await db.commit()
    return issue


@router.get("/{award_id}/issues", response_model=List[ProjectIssueOut])
async def list_issues(award_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await _get_award_and_access(award_id, current_user, db, require_edit=False)
    return await engine.list_issues(db, award_id)


@router.patch("/issues/{issue_id}", response_model=ProjectIssueOut)
async def update_issue(
    issue_id: str, payload: ProjectIssueUpdate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    issue = await engine.get_issue_or_404(db, issue_id)
    await _get_award_and_access(issue.award_id, current_user, db, require_edit=True)
    updated = await engine.update_issue(db, issue_id, payload.model_dump(exclude_unset=True))
    await db.commit()
    return updated


# ── Project execution status ────────────────────────────────────────────────

@router.get("/{award_id}/execution-status", response_model=ProjectExecutionStatusOut)
async def get_execution_status(award_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await _get_award_and_access(award_id, current_user, db, require_edit=False)
    return await engine.get_execution_status(db, award_id)


# ── Performance / KPI actuals ────────────────────────────────────────────────

@router.post("/{award_id}/performance", response_model=AwardPerformanceRecordOut)
async def create_performance_record(
    award_id: str, payload: AwardPerformanceRecordCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_award_and_access(award_id, current_user, db, require_edit=True)
    record = await engine.create_performance_record(db, award_id, payload.model_dump(), recorded_by=current_user.id)
    await db.commit()
    return record


@router.get("/{award_id}/performance", response_model=List[AwardPerformanceRecordOut])
async def list_performance_records(award_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await _get_award_and_access(award_id, current_user, db, require_edit=False)
    return await engine.list_performance_records(db, award_id)


# ── Reports ──────────────────────────────────────────────────────────────────

@router.get("/{award_id}/report", response_model=AwardReportOut)
async def get_report(
    award_id: str, report_type: str = Query("progress"),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    award, access = await _get_award_and_access(award_id, current_user, db, require_edit=False)
    return await engine.generate_report(db, award_id, report_type, _to_award_out(award, proposal_title=access.proposal.title))


@router.post("/{award_id}/report/narrative")
async def generate_report_narrative(
    award_id: str, payload: ReportNarrativeRequest, report_type: str = Query("progress"),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    award, access = await _get_award_and_access(award_id, current_user, db, require_edit=False)
    await _meter(access.org_id, current_user.id, db, reason="award_report_narrative")
    report = await engine.generate_report(db, award_id, report_type, _to_award_out(award, proposal_title=access.proposal.title))
    narrative = await engine.generate_report_narrative(report, access.proposal, payload.additional_context)
    await db.commit()
    return {"narrative": narrative}


# ── Closeout ─────────────────────────────────────────────────────────────────

@router.post("/{award_id}/closeout", response_model=AwardCloseoutOut)
async def close_award(
    award_id: str, payload: AwardCloseoutRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_award_and_access(award_id, current_user, db, require_edit=True)
    closeout = await engine.close_award(db, award_id, payload.model_dump(), closed_by=current_user.id)
    await db.commit()
    return closeout


@router.get("/{award_id}/closeout", response_model=AwardCloseoutOut)
async def get_closeout(award_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await _get_award_and_access(award_id, current_user, db, require_edit=False)
    closeout = await engine.get_closeout(db, award_id)
    if not closeout:
        raise HTTPException(status_code=404, detail="This award has not been closed out yet.")
    return closeout


# ── Renewal ──────────────────────────────────────────────────────────────────

@router.post("/{award_id}/renewal", response_model=FOARecordOut)
async def create_renewal_opportunity(
    award_id: str, payload: RenewalCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    award, access = await _get_award_and_access(award_id, current_user, db, require_edit=True)
    record = await engine.create_renewal_opportunity(db, award_id, payload.model_dump(), uploaded_by=current_user.id)
    await db.commit()
    # Version 3.0 architecture upgrade, Phase 14 — the award/proposal are
    # already in scope here (no extra query needed, unlike the general
    # foa.py::_to_foa_out resolver used for every other listing endpoint),
    # so populate the provenance fields directly on the creation response.
    award_label = f"{access.proposal.title} · {award.funding_agency}" + (f" ({award.award_number})" if award.award_number else "")
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
        originating_award_id=record.originating_award_id, originating_proposal_id=award.proposal_id,
        originating_award_label=award_label, renewal_notes=record.renewal_notes,
    )
