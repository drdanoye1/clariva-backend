"""
Engine 16 — Award & Project Management.

AI narrative generation (generate_report_narrative) needs a real OpenAI call
and is intentionally out of scope here, same policy as every other engine's
AI-generation methods (see conftest.py's module docstring). Everything else
— award CRUD, budget burn-rate/variance math, compliance checklist,
amendments (including the cross-engine hook in
collaboration_engine.py::decide_approval_request that applies
effective_changes on approval), issues, the execution-status rollup read
from Phase 2's Scope of Work Engine, performance actuals, deterministic
report aggregation, closeout, and renewal — is fully deterministic and
covered directly.

Like the other engine test files, methods are async and need a real DB
session, so each test wraps its body in asyncio.run(). Fake proposal_id/
user_id strings are used throughout without creating real Proposal/User
rows — SQLite's FK enforcement is off by default in this app's setup (see
test_scope_of_work_engine.py). Every literal ID that could collide across
test functions in the shared, run-persistent test DB (see
test_collaboration_engine.py's and test_funding_intelligence_engine.py's
notes on this) is generated fresh per test via `_id()`.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from database import AsyncSessionLocal
from engines.award_engine import AwardEngine
from engines.collaboration_engine import CollaborationEngine
from engines.scope_of_work_engine import ScopeOfWorkEngine
from models.db_models import BudgetRecord, new_uuid


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def engine():
    return AwardEngine()


def _id(prefix: str) -> str:
    return f"test-{prefix}-{uuid.uuid4().hex[:12]}"


# ── Awards ───────────────────────────────────────────────────────────────────

def test_create_award_links_existing_budget_record(client, engine):
    proposal_id = _id("proposal")
    creator = _id("user")

    async def _body():
        async with AsyncSessionLocal() as db:
            budget = BudgetRecord(id=new_uuid(), proposal_id=proposal_id, total_cost=50000.0)
            db.add(budget)
            await db.flush()
            await db.commit()
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(
                db, proposal_id, {"funding_agency": "NSF", "award_number": "NSF-1234", "link_budget": True},
                created_by=creator, org_id=None,
            )
            await db.commit()
            return award, budget.id

    award, budget_id = _run(_body())
    assert award.funding_agency == "NSF"
    assert award.award_number == "NSF-1234"
    assert award.budget_record_id == budget_id
    assert award.status == "active"


def test_create_award_duplicate_raises_400(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_award(db, proposal_id, {"funding_agency": "NSF", "link_budget": False}, created_by=_id("user"), org_id=None)
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.create_award(db, proposal_id, {"funding_agency": "NSF", "link_budget": False}, created_by=_id("user"), org_id=None)
            return exc_info.value.status_code

    assert _run(_body()) == 400


def test_update_award_and_list_for_user(client, engine):
    proposal_id = _id("proposal")
    owner = _id("user")
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "DOE", "link_budget": False}, created_by=owner, org_id=org_id)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            updated = await engine.update_award(db, award_id, {"status": "terminated", "total_award_value": 250000.0})
            await db.commit()
        async with AsyncSessionLocal() as db:
            # This award isn't proposal-owned by `owner` in the Proposal
            # table (no real Proposal row exists), so list_awards_for_user
            # must find it via org_id membership, not proposal ownership.
            found = await engine.list_awards_for_user(db, _id("other-user"), [org_id])
            return updated, found

    updated, found = _run(_body())
    assert updated.status == "terminated"
    assert updated.total_award_value == 250000.0
    assert any(a.id == updated.id for a in found)


# ── Budget administration / burn-rate ───────────────────────────────────────

def test_budget_status_computes_burn_rate_and_variance(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            budget = BudgetRecord(id=new_uuid(), proposal_id=proposal_id, total_cost=100000.0)
            db.add(budget)
            await db.flush()
            award = await engine.create_award(
                db, proposal_id, {
                    "funding_agency": "NSF", "link_budget": True,
                    "period_of_performance_start": datetime.utcnow() - timedelta(days=50),
                    "period_of_performance_end": datetime.utcnow() + timedelta(days=50),
                },
                created_by=_id("user"), org_id=None,
            )
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            await engine.create_expenditure(db, award_id, {"category": "personnel", "amount": 30000.0}, recorded_by=_id("user"))
            await engine.create_expenditure(db, award_id, {"category": "equipment", "amount": 10000.0}, recorded_by=_id("user"))
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_budget_status(db, award_id)

    status = _run(_body())
    assert status.baseline_total_cost == 100000.0
    assert status.total_expended == 40000.0
    assert status.burn_rate_pct == 40.0
    assert status.elapsed_pct == pytest.approx(50.0, abs=1.0)
    assert status.variance_pct == pytest.approx(-10.0, abs=1.0)
    assert status.by_category == {"personnel": 30000.0, "equipment": 10000.0}


def test_budget_status_with_no_baseline_returns_none_burn_rate(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "NSF", "link_budget": False}, created_by=_id("user"), org_id=None)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            return await engine.get_budget_status(db, award_id)

    status = _run(_body())
    assert status.baseline_total_cost == 0.0
    assert status.burn_rate_pct is None
    assert status.elapsed_pct is None


# ── Compliance checklist ─────────────────────────────────────────────────────

def test_compliance_item_lifecycle(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "NSF", "link_budget": False}, created_by=_id("user"), org_id=None)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            item = await engine.create_compliance_item(db, award_id, {"obligation": "Submit annual report", "category": "reporting"})
            await db.commit()
            item_id = item.id
        async with AsyncSessionLocal() as db:
            completed = await engine.update_compliance_item(db, item_id, {"status": "complete"}, completed_by=_id("user"))
            await db.commit()
        async with AsyncSessionLocal() as db:
            items = await engine.list_compliance_items(db, award_id)
            await engine.delete_compliance_item(db, item_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            remaining = await engine.list_compliance_items(db, award_id)
            return completed, items, remaining

    completed, items, remaining = _run(_body())
    assert completed.status == "complete"
    assert completed.completed_at is not None
    assert len(items) == 1
    assert remaining == []


# ── Amendments (integration with Phase 3's ApprovalRequest) ────────────────

def test_amendment_approval_applies_effective_changes_to_award(client, engine):
    proposal_id = _id("proposal")
    org_id = _id("org")
    approver = _id("approver")
    collab = CollaborationEngine()

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "NSF", "total_award_value": 100000.0, "link_budget": False}, created_by=_id("user"), org_id=org_id)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            amendment = await engine.create_amendment(
                db, award_id, {
                    "amendment_type": "budget", "description": "Increase award value",
                    "effective_changes": {"total_award_value": 175000.0}, "approver_id": approver,
                },
                requested_by=_id("requester"), org_id=org_id,
            )
            await db.commit()
            amendment_id, approval_request_id = amendment.id, amendment.approval_request_id
        async with AsyncSessionLocal() as db:
            await collab.decide_approval_request(db, approval_request_id, approver, True, decision_notes="Approved")
            await db.commit()
        async with AsyncSessionLocal() as db:
            refreshed_award = await engine.get_award_or_404(db, award_id)
            amendments = await engine.list_amendments(db, award_id)
            return refreshed_award, amendments

    refreshed_award, amendments = _run(_body())
    assert refreshed_award.total_award_value == 175000.0
    assert amendments[0].status == "approved"
    assert amendments[0].decided_at is not None


def test_amendment_rejection_does_not_apply_changes(client, engine):
    proposal_id = _id("proposal")
    org_id = _id("org")
    approver = _id("approver")
    collab = CollaborationEngine()

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "NSF", "total_award_value": 100000.0, "link_budget": False}, created_by=_id("user"), org_id=org_id)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            amendment = await engine.create_amendment(
                db, award_id, {
                    "amendment_type": "budget", "description": "Increase award value",
                    "effective_changes": {"total_award_value": 999999.0}, "approver_id": approver,
                },
                requested_by=_id("requester"), org_id=org_id,
            )
            await db.commit()
            approval_request_id = amendment.approval_request_id
        async with AsyncSessionLocal() as db:
            await collab.decide_approval_request(db, approval_request_id, approver, False)
            await db.commit()
        async with AsyncSessionLocal() as db:
            refreshed_award = await engine.get_award_or_404(db, award_id)
            amendments = await engine.list_amendments(db, award_id)
            return refreshed_award, amendments

    refreshed_award, amendments = _run(_body())
    assert refreshed_award.total_award_value == 100000.0
    assert amendments[0].status == "rejected"


# ── Issues ───────────────────────────────────────────────────────────────────

def test_issue_lifecycle(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "NSF", "link_budget": False}, created_by=_id("user"), org_id=None)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            issue = await engine.create_issue(db, award_id, {"title": "Vendor delay", "severity": "high"}, raised_by=_id("user"))
            await db.commit()
            issue_id = issue.id
        async with AsyncSessionLocal() as db:
            resolved = await engine.update_issue(db, issue_id, {"status": "resolved"})
            await db.commit()
            return resolved

    resolved = _run(_body())
    assert resolved.status == "resolved"
    assert resolved.resolved_at is not None


# ── Project execution status (reads Phase 2's Scope of Work Engine) ────────

def test_execution_status_rolls_up_scope_of_work(client, engine):
    proposal_id = _id("proposal")
    sow_engine = ScopeOfWorkEngine()

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "NSF", "link_budget": False}, created_by=_id("user"), org_id=None)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            wp = await sow_engine.create_work_package(db, proposal_id, {"name": "WP1"})
            await sow_engine.create_task(db, proposal_id, wp.id, {"name": "Task 1", "status": "in_progress"})
            await sow_engine.create_task(db, proposal_id, wp.id, {"name": "Task 2", "status": "complete"})
            await sow_engine.create_milestone(db, proposal_id, {"name": "M1"})
            await sow_engine.create_deliverable(db, proposal_id, {"name": "D1"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.create_issue(db, award_id, {"title": "Open issue"}, raised_by=_id("user"))
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_execution_status(db, award_id)

    status = _run(_body())
    assert status.total_work_packages == 1
    assert status.total_tasks == 2
    assert status.tasks_by_status == {"in_progress": 1, "complete": 1}
    assert status.total_milestones == 1
    assert status.total_deliverables == 1
    assert status.open_issues == 1


def test_execution_status_with_no_scope_of_work_returns_zeros(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "NSF", "link_budget": False}, created_by=_id("user"), org_id=None)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            return await engine.get_execution_status(db, award_id)

    status = _run(_body())
    assert status.total_work_packages == 0
    assert status.total_tasks == 0
    assert status.open_issues == 0


# ── Performance / KPI actuals ────────────────────────────────────────────────

def test_performance_record_lifecycle(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "NSF", "link_budget": False}, created_by=_id("user"), org_id=None)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            await engine.create_performance_record(db, award_id, {"kpi_name": "Jobs created", "target": 10.0, "actual_value": 6.0, "unit": "jobs"}, recorded_by=_id("user"))
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.list_performance_records(db, award_id)

    records = _run(_body())
    assert len(records) == 1
    assert records[0].kpi_name == "Jobs created"
    assert records[0].actual_value == 6.0


# ── Reports (deterministic aggregation) ─────────────────────────────────────

def test_generate_report_aggregates_budget_execution_and_compliance(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            budget = BudgetRecord(id=new_uuid(), proposal_id=proposal_id, total_cost=20000.0)
            db.add(budget)
            await db.flush()
            award = await engine.create_award(db, proposal_id, {"funding_agency": "NSF", "link_budget": True}, created_by=_id("user"), org_id=None)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            await engine.create_compliance_item(db, award_id, {"obligation": "Report A"})
            item2 = await engine.create_compliance_item(db, award_id, {"obligation": "Report B"})
            await db.commit()
            item2_id = item2.id
        async with AsyncSessionLocal() as db:
            await engine.update_compliance_item(db, item2_id, {"status": "complete"}, completed_by=_id("user"))
            await db.commit()
        async with AsyncSessionLocal() as db:
            award = await engine.get_award_or_404(db, award_id)
            from routers.awards import _to_award_out
            report = await engine.generate_report(db, award_id, "financial", _to_award_out(award))
            return report

    report = _run(_body())
    assert report.report_type == "financial"
    assert report.budget_status.baseline_total_cost == 20000.0
    assert report.compliance_summary == {"pending": 1, "complete": 1}


# ── Closeout ─────────────────────────────────────────────────────────────────

def test_close_award_creates_memory_record_and_closes_award(client, engine):
    proposal_id = _id("proposal")
    closer = _id("user")

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "NIH", "link_budget": False}, created_by=_id("user"), org_id=None)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            closeout = await engine.close_award(db, award_id, {
                "deliverables_reconciled": True, "final_report_submitted": True,
                "lessons_learned": ["Start procurement earlier next time"], "outcome_score": 92.0,
            }, closed_by=closer)
            await db.commit()
            closeout_id = closeout.id
        async with AsyncSessionLocal() as db:
            refreshed_award = await engine.get_award_or_404(db, award_id)
            refreshed_closeout = await engine.get_closeout(db, award_id)
            return refreshed_award, refreshed_closeout, closeout_id

    award, closeout, closeout_id = _run(_body())
    assert award.status == "closed"
    assert closeout.deliverables_reconciled is True
    assert closeout.memory_record_id is not None
    assert closeout.lessons_learned == ["Start procurement earlier next time"]
    assert closeout.id == closeout_id


# ── Renewal ──────────────────────────────────────────────────────────────────

def test_create_renewal_opportunity_links_originating_award(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "DOE", "link_budget": False}, created_by=_id("user"), org_id=None)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                # No real Proposal row exists for this fake proposal_id, so
                # renewal creation (which needs the proposal's title/phase)
                # should 404 rather than silently fabricate a title.
                await engine.create_renewal_opportunity(db, award_id, {}, uploaded_by=_id("user"))
            return exc_info.value.status_code

    assert _run(_body()) == 404
