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

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import Award, OrgMembership, User
from models.schemas import (
    AwardAmendmentCreate, AwardAmendmentOut, AwardCloseoutOut, AwardCloseoutRequest,
    AwardComplianceItemCreate, AwardComplianceItemOut, AwardComplianceItemUpdate,
    AwardCreate, AwardExpenditureCreate, AwardExpenditureOut, AwardOut,
    AwardPerformanceRecordCreate, AwardPerformanceRecordOut, AwardReportOut,
    AwardUpdate, BudgetStatusOut, FOARecordOut, ProjectExecutionStatusOut,
    ProjectIssueCreate, ProjectIssueOut, ProjectIssueUpdate, RenewalCreate,
    ReportNarrativeRequest,
)
from routers.auth import get_current_user
from routers.organizations import _assert_member
from workspace_access import ProposalAccess, assert_can_edit, assert_can_view
from engines.award_engine import AwardEngine
from engines.credit_engine import CreditEngine, GENERATION_COST, debit_or_402

router = APIRouter()
engine = AwardEngine()
credit_engine = CreditEngine()


def _to_award_out(award: Award, proposal_title: Optional[str] = None) -> AwardOut:
    return AwardOut(
        id=award.id, proposal_id=award.proposal_id, foa_id=award.foa_id, org_id=award.org_id,
        budget_record_id=award.budget_record_id, award_number=award.award_number,
        funding_agency=award.funding_agency,
        period_of_performance_start=award.period_of_performance_start,
        period_of_performance_end=award.period_of_performance_end,
        total_award_value=award.total_award_value, terms=award.terms, status=award.status,
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
    await _get_award_and_access(award_id, current_user, db, require_edit=True)
    record = await engine.create_renewal_opportunity(db, award_id, payload.model_dump(), uploaded_by=current_user.id)
    await db.commit()
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
    )
