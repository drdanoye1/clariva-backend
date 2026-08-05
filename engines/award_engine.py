"""
Engine 16 — Award & Project Management
Turns a won pipeline opportunity (Phase 4's FOARecord.pipeline_stage ==
"awarded") into an active Award: budget administration (burn-rate/variance
against the Budget Builder's BudgetRecord baseline), a terms/conditions
compliance checklist, amendments routed through Phase 3's generic
ApprovalRequest workflow, project execution status rolled up from Phase 2's
Scope of Work Engine, issue tracking, KPI performance actuals against
ProjectKnowledge.kpis targets, structured reports, closeout (feeding
Organizational Memory), and renewals (new Phase 4 pipeline entries)
(Clariva Enterprise™ PRD §16-17).

Design notes (same discipline as engines/scope_of_work_engine.py and
engines/collaboration_engine.py):
- Every method takes an already-open AsyncSession and only flushes, never
  commits — the caller's request-scoped session commits once, after the
  router handler returns.
- No permission checks live here — routers/awards.py gates access via
  workspace_access.py's proposal-access resolver (an Award is 1:1 with a
  Proposal, so "can edit this proposal" is exactly "can manage this award").
- Award creation is always explicit (a router action), never automatic on a
  pipeline-stage change — reaching "awarded" doesn't carry an award number,
  exact period of performance, or total value, so auto-creating an
  incomplete Award would be worse than requiring one deliberate step.
- Amendments reuse ApprovalRequest/Notification models directly (imported
  from db_models, not via CollaborationEngine) — the same
  "engines interoperate through shared models, not each other's classes"
  precedent set by funding_intelligence_engine.py's Notification usage.
  The actual decision — approve/reject, and applying `effective_changes` to
  the Award — happens inside collaboration_engine.py::decide_approval_request
  so there is exactly one place amendments get decided, whichever router the
  call came through.
- Project execution status is a live rollup read directly from Phase 2's
  WorkPackage/Task/Milestone/Deliverable/ProjectKnowledge tables — Phase 5
  does not duplicate a WBS or maintain its own copy.
- Every create/update path that returns a row for serialization calls
  `await db.refresh(obj)` after `await db.flush()` (server_default/onupdate
  timestamps are not eagerly fetched by AsyncSession + aiosqlite — see the
  identical fix first documented in credit_engine.py).
"""
from __future__ import annotations

import logging
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

import openai
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.db_models import (
    ApprovalRequest, Award, AwardAmendment, AwardCloseout, AwardComplianceItem,
    AwardExpenditure, AwardPerformanceRecord, BudgetRecord, Deliverable, FOARecord,
    MemoryRecord, Milestone, Notification, ProjectIssue, ProjectKnowledge, Proposal,
    ScopeOfWork, Task, WorkPackage, new_uuid,
)
from models.schemas import AwardReportOut, BudgetStatusOut, ProjectExecutionStatusOut

_log = logging.getLogger(__name__)


def _ai_error(exc: Exception) -> HTTPException:
    """See the identical helper in engines/scope_of_work_engine.py and
    engines/proposal_generator.py — this codebase's convention is a local
    copy per module rather than a shared import."""
    if isinstance(exc, openai.RateLimitError):
        _log.error("AI service rate limit exceeded: %s", exc)
        return HTTPException(status_code=503, detail="The AI writing service is temporarily unavailable due to high demand. Please try again in a few minutes.")
    if isinstance(exc, openai.AuthenticationError):
        _log.critical("AI service authentication failure: %s", exc)
        return HTTPException(status_code=503, detail="The AI writing service is not configured correctly. Please contact support.")
    if isinstance(exc, openai.APIConnectionError):
        _log.error("AI service connection error: %s", exc)
        return HTTPException(status_code=503, detail="The AI writing service is unreachable. Please try again in a moment.")
    if isinstance(exc, openai.APIStatusError):
        _log.error("AI service API error %s: %s", getattr(exc, "status_code", "?"), getattr(exc, "message", str(exc)))
        return HTTPException(status_code=503, detail="The AI writing service returned an unexpected error. Please try again.")
    _log.error("Unexpected report-narrative generation error:\n%s", traceback.format_exc())
    return HTTPException(status_code=500, detail="Content generation failed. Please try again or contact support.")


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Strip tzinfo so period-of-performance math works whether the value
    came back tz-aware (PostgreSQL) or naive (SQLite)."""
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


class AwardEngine:
    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    async def _notify(self, db: AsyncSession, user_id: str, type_: str, message: str,
                       object_type: Optional[str] = None, object_id: Optional[str] = None) -> Notification:
        n = Notification(id=new_uuid(), user_id=user_id, type=type_, message=message,
                          object_type=object_type, object_id=object_id)
        db.add(n)
        await db.flush()
        return n

    # ── Awards ───────────────────────────────────────────────────────────────

    async def get_award_or_404(self, db: AsyncSession, award_id: str) -> Award:
        result = await db.execute(select(Award).where(Award.id == award_id))
        award = result.scalar_one_or_none()
        if not award:
            raise HTTPException(status_code=404, detail="Award not found")
        return award

    async def get_award_by_proposal(self, db: AsyncSession, proposal_id: str) -> Optional[Award]:
        result = await db.execute(select(Award).where(Award.proposal_id == proposal_id))
        return result.scalar_one_or_none()

    async def create_award(
        self, db: AsyncSession, proposal_id: str, data: Dict[str, Any],
        created_by: str, org_id: Optional[str],
    ) -> Award:
        existing = await self.get_award_by_proposal(db, proposal_id)
        if existing:
            raise HTTPException(status_code=400, detail="This proposal already has an award.")

        budget_record_id = None
        if data.get("link_budget", True):
            budget_result = await db.execute(select(BudgetRecord).where(BudgetRecord.proposal_id == proposal_id))
            budget = budget_result.scalar_one_or_none()
            if budget:
                budget_record_id = budget.id

        foa_id = None
        proposal_result = await db.execute(select(Proposal).where(Proposal.id == proposal_id))
        proposal = proposal_result.scalar_one_or_none()
        if proposal and proposal.foa_id:
            foa_id = proposal.foa_id

        award = Award(
            id=new_uuid(), proposal_id=proposal_id, foa_id=foa_id, org_id=org_id,
            budget_record_id=budget_record_id,
            award_number=data.get("award_number"), funding_agency=data["funding_agency"],
            period_of_performance_start=data.get("period_of_performance_start"),
            period_of_performance_end=data.get("period_of_performance_end"),
            total_award_value=data.get("total_award_value"), terms=data.get("terms"),
            status="active", created_by=created_by,
        )
        db.add(award)
        await db.flush()
        await db.refresh(award)
        return award

    async def update_award(self, db: AsyncSession, award_id: str, data: Dict[str, Any]) -> Award:
        award = await self.get_award_or_404(db, award_id)
        for field in ("award_number", "period_of_performance_start", "period_of_performance_end",
                      "total_award_value", "terms", "status"):
            if field in data and data[field] is not None:
                setattr(award, field, data[field])
        await db.flush()
        await db.refresh(award)
        return award

    async def list_awards_for_user(self, db: AsyncSession, user_id: str, org_ids: List[str]) -> List[Award]:
        owned = await db.execute(
            select(Award).join(Proposal, Proposal.id == Award.proposal_id).where(Proposal.owner_id == user_id)
        )
        by_id: Dict[str, Award] = {a.id: a for a in owned.scalars().all()}
        if org_ids:
            shared = await db.execute(select(Award).where(Award.org_id.in_(org_ids)))
            for a in shared.scalars().all():
                by_id[a.id] = a
        return sorted(by_id.values(), key=lambda a: a.created_at or datetime.min, reverse=True)

    # ── Budget administration / burn-rate (PRD §16) ─────────────────────────

    async def create_expenditure(self, db: AsyncSession, award_id: str, data: Dict[str, Any], recorded_by: str) -> AwardExpenditure:
        await self.get_award_or_404(db, award_id)
        exp = AwardExpenditure(
            id=new_uuid(), award_id=award_id, category=data["category"],
            description=data.get("description"), amount=data["amount"],
            incurred_date=data.get("incurred_date"), recorded_by=recorded_by,
        )
        db.add(exp)
        await db.flush()
        await db.refresh(exp)
        return exp

    async def list_expenditures(self, db: AsyncSession, award_id: str) -> List[AwardExpenditure]:
        result = await db.execute(
            select(AwardExpenditure).where(AwardExpenditure.award_id == award_id).order_by(AwardExpenditure.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_budget_status(self, db: AsyncSession, award_id: str) -> BudgetStatusOut:
        award = await self.get_award_or_404(db, award_id)
        baseline_total_cost = 0.0
        if award.budget_record_id:
            budget_result = await db.execute(select(BudgetRecord).where(BudgetRecord.id == award.budget_record_id))
            budget = budget_result.scalar_one_or_none()
            if budget:
                baseline_total_cost = budget.total_cost or 0.0

        expenditures = await self.list_expenditures(db, award_id)
        total_expended = sum(e.amount for e in expenditures)
        by_category: Dict[str, float] = {}
        for e in expenditures:
            by_category[e.category] = by_category.get(e.category, 0.0) + e.amount

        burn_rate_pct = round(total_expended / baseline_total_cost * 100, 1) if baseline_total_cost > 0 else None

        elapsed_pct = None
        start = _naive(award.period_of_performance_start)
        end = _naive(award.period_of_performance_end)
        if start and end and end > start:
            total_seconds = (end - start).total_seconds()
            elapsed_seconds = max(0.0, min(total_seconds, (datetime.utcnow() - start).total_seconds()))
            elapsed_pct = round(elapsed_seconds / total_seconds * 100, 1)

        variance_pct = round(burn_rate_pct - elapsed_pct, 1) if burn_rate_pct is not None and elapsed_pct is not None else None

        return BudgetStatusOut(
            award_id=award_id, baseline_total_cost=baseline_total_cost, total_expended=total_expended,
            burn_rate_pct=burn_rate_pct, elapsed_pct=elapsed_pct, variance_pct=variance_pct, by_category=by_category,
        )

    # ── Compliance checklist (PRD §16) ──────────────────────────────────────

    async def get_compliance_item_or_404(self, db: AsyncSession, item_id: str) -> AwardComplianceItem:
        result = await db.execute(select(AwardComplianceItem).where(AwardComplianceItem.id == item_id))
        item = result.scalar_one_or_none()
        if not item:
            raise HTTPException(status_code=404, detail="Compliance item not found")
        return item

    async def create_compliance_item(self, db: AsyncSession, award_id: str, data: Dict[str, Any]) -> AwardComplianceItem:
        await self.get_award_or_404(db, award_id)
        item = AwardComplianceItem(
            id=new_uuid(), award_id=award_id, obligation=data["obligation"],
            category=data.get("category"), due_date=data.get("due_date"), notes=data.get("notes"),
        )
        db.add(item)
        await db.flush()
        await db.refresh(item)
        return item

    async def list_compliance_items(self, db: AsyncSession, award_id: str) -> List[AwardComplianceItem]:
        result = await db.execute(
            select(AwardComplianceItem).where(AwardComplianceItem.award_id == award_id).order_by(AwardComplianceItem.due_date)
        )
        return list(result.scalars().all())

    async def update_compliance_item(self, db: AsyncSession, item_id: str, data: Dict[str, Any], completed_by: Optional[str] = None) -> AwardComplianceItem:
        item = await self.get_compliance_item_or_404(db, item_id)
        for field in ("obligation", "category", "due_date", "status", "notes"):
            if field in data and data[field] is not None:
                setattr(item, field, data[field])
        if data.get("status") == "complete" and not item.completed_at:
            item.completed_at = datetime.utcnow()
            item.completed_by = completed_by
        await db.flush()
        await db.refresh(item)
        return item

    async def delete_compliance_item(self, db: AsyncSession, item_id: str) -> None:
        item = await self.get_compliance_item_or_404(db, item_id)
        await db.delete(item)
        await db.flush()

    # ── Amendments (PRD §16 — approval routing via Phase 3's ApprovalRequest) ──

    async def get_amendment_or_404(self, db: AsyncSession, amendment_id: str) -> AwardAmendment:
        result = await db.execute(select(AwardAmendment).where(AwardAmendment.id == amendment_id))
        amendment = result.scalar_one_or_none()
        if not amendment:
            raise HTTPException(status_code=404, detail="Amendment not found")
        return amendment

    async def create_amendment(
        self, db: AsyncSession, award_id: str, data: Dict[str, Any], requested_by: str, org_id: Optional[str],
    ) -> AwardAmendment:
        await self.get_award_or_404(db, award_id)
        amendment = AwardAmendment(
            id=new_uuid(), award_id=award_id, amendment_type=data["amendment_type"],
            description=data["description"], effective_changes=data.get("effective_changes"),
            requested_by=requested_by, status="pending",
        )
        db.add(amendment)
        await db.flush()
        await db.refresh(amendment)

        approver_id = data.get("approver_id")
        approval = ApprovalRequest(
            id=new_uuid(), org_id=org_id, object_type="award_amendment", object_id=amendment.id,
            requested_by=requested_by, approver_id=approver_id, notes=data["description"],
        )
        db.add(approval)
        await db.flush()
        await db.refresh(approval)
        amendment.approval_request_id = approval.id
        await db.flush()
        await db.refresh(amendment)

        if approver_id:
            await self._notify(db, approver_id, "approval_requested", "An award amendment needs your decision.",
                                object_type="award_amendment", object_id=amendment.id)
        return amendment

    async def list_amendments(self, db: AsyncSession, award_id: str) -> List[AwardAmendment]:
        result = await db.execute(
            select(AwardAmendment).where(AwardAmendment.award_id == award_id).order_by(AwardAmendment.created_at.desc())
        )
        return list(result.scalars().all())

    # ── Issues (PRD §17) ─────────────────────────────────────────────────────

    async def get_issue_or_404(self, db: AsyncSession, issue_id: str) -> ProjectIssue:
        result = await db.execute(select(ProjectIssue).where(ProjectIssue.id == issue_id))
        issue = result.scalar_one_or_none()
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")
        return issue

    async def create_issue(self, db: AsyncSession, award_id: str, data: Dict[str, Any], raised_by: str) -> ProjectIssue:
        await self.get_award_or_404(db, award_id)
        issue = ProjectIssue(
            id=new_uuid(), award_id=award_id, work_package_id=data.get("work_package_id"),
            title=data["title"], description=data.get("description"),
            severity=data.get("severity") or "medium", raised_by=raised_by,
        )
        db.add(issue)
        await db.flush()
        await db.refresh(issue)
        return issue

    async def list_issues(self, db: AsyncSession, award_id: str) -> List[ProjectIssue]:
        result = await db.execute(
            select(ProjectIssue).where(ProjectIssue.award_id == award_id).order_by(ProjectIssue.created_at.desc())
        )
        return list(result.scalars().all())

    async def update_issue(self, db: AsyncSession, issue_id: str, data: Dict[str, Any]) -> ProjectIssue:
        issue = await self.get_issue_or_404(db, issue_id)
        for field in ("title", "description", "severity", "status"):
            if field in data and data[field] is not None:
                setattr(issue, field, data[field])
        if data.get("status") == "resolved" and not issue.resolved_at:
            issue.resolved_at = datetime.utcnow()
        await db.flush()
        await db.refresh(issue)
        return issue

    # ── Project execution status (PRD §17 — read from Phase 2's SOW Engine) ──

    async def get_execution_status(self, db: AsyncSession, award_id: str) -> ProjectExecutionStatusOut:
        award = await self.get_award_or_404(db, award_id)

        pk_result = await db.execute(select(ProjectKnowledge).where(ProjectKnowledge.proposal_id == award.proposal_id))
        pk = pk_result.scalar_one_or_none()
        open_issues_result = await db.execute(
            select(ProjectIssue).where(ProjectIssue.award_id == award_id, ProjectIssue.status == "open")
        )
        open_issues = len(list(open_issues_result.scalars().all()))

        if not pk:
            return ProjectExecutionStatusOut(award_id=award_id, open_issues=open_issues)

        sow_result = await db.execute(select(ScopeOfWork).where(ScopeOfWork.project_knowledge_id == pk.id))
        sow = sow_result.scalar_one_or_none()
        if not sow:
            return ProjectExecutionStatusOut(award_id=award_id, open_issues=open_issues, stale_flags=pk.stale_flags or {})

        wp_result = await db.execute(select(WorkPackage).where(WorkPackage.scope_of_work_id == sow.id))
        work_packages = list(wp_result.scalars().all())
        wp_ids = [wp.id for wp in work_packages]

        tasks: List[Task] = []
        if wp_ids:
            tasks_result = await db.execute(select(Task).where(Task.work_package_id.in_(wp_ids)))
            tasks = list(tasks_result.scalars().all())

        milestones_result = await db.execute(select(Milestone).where(Milestone.scope_of_work_id == sow.id))
        milestones = list(milestones_result.scalars().all())

        deliverables_result = await db.execute(select(Deliverable).where(Deliverable.scope_of_work_id == sow.id))
        deliverables = list(deliverables_result.scalars().all())

        def _tally(items, attr="status") -> Dict[str, int]:
            counts: Dict[str, int] = {}
            for item in items:
                key = getattr(item, attr) or "unknown"
                counts[key] = counts.get(key, 0) + 1
            return counts

        return ProjectExecutionStatusOut(
            award_id=award_id,
            total_work_packages=len(work_packages),
            total_tasks=len(tasks), tasks_by_status=_tally(tasks),
            total_milestones=len(milestones), milestones_by_status=_tally(milestones),
            total_deliverables=len(deliverables), deliverables_by_status=_tally(deliverables),
            open_issues=open_issues, stale_flags=pk.stale_flags or {},
        )

    # ── Performance / KPI actuals (PRD §17) ─────────────────────────────────

    async def create_performance_record(self, db: AsyncSession, award_id: str, data: Dict[str, Any], recorded_by: Optional[str]) -> AwardPerformanceRecord:
        await self.get_award_or_404(db, award_id)
        record = AwardPerformanceRecord(
            id=new_uuid(), award_id=award_id, kpi_name=data["kpi_name"],
            target=data.get("target"), actual_value=data.get("actual_value"),
            unit=data.get("unit"), period_label=data.get("period_label"),
            notes=data.get("notes"), recorded_by=recorded_by,
        )
        db.add(record)
        await db.flush()
        await db.refresh(record)
        return record

    async def list_performance_records(self, db: AsyncSession, award_id: str) -> List[AwardPerformanceRecord]:
        result = await db.execute(
            select(AwardPerformanceRecord).where(AwardPerformanceRecord.award_id == award_id).order_by(AwardPerformanceRecord.created_at.desc())
        )
        return list(result.scalars().all())

    # ── Reports (PRD §17 — "generated as views, not authored from scratch") ──

    async def generate_report(self, db: AsyncSession, award_id: str, report_type: str, award_out: Any) -> AwardReportOut:
        budget_status = await self.get_budget_status(db, award_id)
        execution_status = await self.get_execution_status(db, award_id)
        performance = await self.list_performance_records(db, award_id)

        compliance_items = await self.list_compliance_items(db, award_id)
        compliance_summary: Dict[str, int] = {}
        for item in compliance_items:
            compliance_summary[item.status] = compliance_summary.get(item.status, 0) + 1

        from models.schemas import AwardPerformanceRecordOut
        return AwardReportOut(
            award_id=award_id, report_type=report_type, generated_at=datetime.utcnow(),
            award=award_out, budget_status=budget_status, execution_status=execution_status,
            performance=[AwardPerformanceRecordOut.model_validate(p) for p in performance],
            compliance_summary=compliance_summary or None,
        )

    async def generate_report_narrative(self, report: AwardReportOut, proposal: Proposal, additional_context: Optional[str] = None) -> str:
        exec_status = report.execution_status
        budget_status = report.budget_status
        prompt = f"""
Write a {report.report_type} report narrative (3-5 paragraphs, plain prose, no
markdown or headers) for the following funded project, suitable for
submission to the funding agency or for internal project review.

Project title: {getattr(proposal, "title", "")}
Agency: {report.award.funding_agency}
Award number: {report.award.award_number or "Not yet assigned"}
Total award value: {report.award.total_award_value if report.award.total_award_value is not None else "Not specified"}
Period of performance: {report.award.period_of_performance_start} to {report.award.period_of_performance_end}

Budget status: {f"{budget_status.burn_rate_pct}% of baseline budget expended ({budget_status.total_expended} of {budget_status.baseline_total_cost})" if budget_status else "Not available"}
Elapsed period of performance: {f"{budget_status.elapsed_pct}%" if budget_status and budget_status.elapsed_pct is not None else "Not available"}

Work packages: {exec_status.total_work_packages if exec_status else 0}
Tasks by status: {exec_status.tasks_by_status if exec_status else {}}
Milestones by status: {exec_status.milestones_by_status if exec_status else {}}
Deliverables by status: {exec_status.deliverables_by_status if exec_status else {}}
Open issues: {exec_status.open_issues if exec_status else 0}

KPI performance records: {[(p.kpi_name, p.target, p.actual_value, p.unit) for p in report.performance]}
Compliance obligation status counts: {report.compliance_summary or {}}
{f"Additional context: {additional_context}" if additional_context else ""}

If information needed to write a specific, credible narrative is missing,
write around it generally rather than inventing specifics — do not
fabricate figures, dates, or results not given above.
""".strip()
        try:
            response = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are an expert grant/project manager producing a funder-facing project report. Output plain prose only — no markdown, no headers, no bullet characters."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5,
                max_tokens=900,
            )
        except Exception as exc:
            raise _ai_error(exc)
        return response.choices[0].message.content.strip()

    # ── Closeout (PRD §17 — feeds Organizational Memory) ────────────────────

    async def close_award(self, db: AsyncSession, award_id: str, data: Dict[str, Any], closed_by: str) -> AwardCloseout:
        award = await self.get_award_or_404(db, award_id)

        result = await db.execute(select(AwardCloseout).where(AwardCloseout.award_id == award_id))
        closeout = result.scalar_one_or_none()
        if not closeout:
            closeout = AwardCloseout(id=new_uuid(), award_id=award_id)
            db.add(closeout)

        closeout.deliverables_reconciled = data.get("deliverables_reconciled", closeout.deliverables_reconciled)
        closeout.deliverables_notes = data.get("deliverables_notes") or closeout.deliverables_notes
        closeout.equipment_disposition = data.get("equipment_disposition") or closeout.equipment_disposition
        closeout.final_report_submitted = data.get("final_report_submitted", closeout.final_report_submitted)
        lessons = data.get("lessons_learned")
        if lessons is not None:
            closeout.lessons_learned = lessons

        memory = MemoryRecord(
            id=new_uuid(), org_id=closed_by, proposal_id=award.proposal_id,
            agency=award.funding_agency, outcome="funded",
            score=data.get("outcome_score"), lessons_learned=closeout.lessons_learned or [],
        )
        db.add(memory)
        await db.flush()
        await db.refresh(memory)

        closeout.memory_record_id = memory.id
        closeout.closed_by = closed_by
        closeout.closed_at = datetime.utcnow()
        award.status = "closed"

        await db.flush()
        await db.refresh(closeout)
        return closeout

    async def get_closeout(self, db: AsyncSession, award_id: str) -> Optional[AwardCloseout]:
        result = await db.execute(select(AwardCloseout).where(AwardCloseout.award_id == award_id))
        return result.scalar_one_or_none()

    # ── Renewal (PRD §17 — new pipeline entry linked to the originating award) ──

    async def create_renewal_opportunity(self, db: AsyncSession, award_id: str, data: Dict[str, Any], uploaded_by: str) -> FOARecord:
        award = await self.get_award_or_404(db, award_id)
        proposal_result = await db.execute(select(Proposal).where(Proposal.id == award.proposal_id))
        proposal = proposal_result.scalar_one_or_none()
        if not proposal:
            raise HTTPException(status_code=404, detail="Originating proposal not found")

        record = FOARecord(
            id=new_uuid(), agency=award.funding_agency,
            program_title=data.get("program_title") or f"{proposal.title} (Renewal)",
            phase=proposal.phase, grant_type=proposal.grant_type or "sbir",
            deadline=data.get("deadline"), uploaded_by=uploaded_by, org_id=award.org_id,
            pipeline_stage="identified", source="manual", originating_award_id=award.id,
        )
        db.add(record)
        await db.flush()
        await db.refresh(record)
        return record
