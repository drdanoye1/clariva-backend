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

from sqlalchemy import select

from database import AsyncSessionLocal
from engines.award_engine import AwardEngine
from engines.collaboration_engine import CollaborationEngine
from engines.scope_of_work_engine import ScopeOfWorkEngine
from models.db_models import (
    AwardCondition, AwardExpenditure, BudgetRecord, Deliverable, Milestone,
    ProjectBaseline, ProjectKnowledge, ScopeOfWork, WorkPackage, new_uuid,
)


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


# ── Phase 7 — Award Received data model foundation (Version 3.0 upgrade) ─────
# Engine methods for activation/baseline-versioning land in Phase 8 (per
# docs/Clariva_Enterprise_v3_Roadmap.docx); Phase 7 is model-only, so these
# tests exercise the new column/tables directly, the same way
# test_create_award_links_existing_budget_record above builds a raw
# BudgetRecord row rather than going through an engine method that doesn't
# exist yet.

def test_new_award_defaults_to_received_award_status(client, engine):
    """AwardEngine.create_award() itself is unchanged by Phase 7, but the
    new award_status column's Python-side default must be "received" for
    every award the ORM inserts from here on — see Award.award_status's
    docstring for why this differs from the migration's SQL-level default."""
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(
                db, proposal_id, {"funding_agency": "NIH", "link_budget": False},
                created_by=_id("user"), org_id=None,
            )
            await db.commit()
            return award

    award = _run(_body())
    assert award.award_status == "received"
    assert award.status == "active"  # the pre-existing lifecycle field is untouched


def test_project_baseline_links_to_award_and_defaults_current(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(
                db, proposal_id, {"funding_agency": "NSF", "link_budget": False},
                created_by=_id("user"), org_id=None,
            )
            await db.commit()
            award_id = award.id

        async with AsyncSessionLocal() as db:
            baseline = ProjectBaseline(
                id=new_uuid(), award_id=award_id, version=1,
                total_award_value=250000.0,
                budget_snapshot={"personnel": [{"name": "PI", "annual_salary": 100000}]},
                scope_snapshot={"work_packages": [{"name": "WP1"}]},
                created_by=_id("user"),
            )
            db.add(baseline)
            await db.flush()
            await db.commit()

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(ProjectBaseline).where(ProjectBaseline.award_id == award_id))
            return result.scalar_one()

    baseline = _run(_body())
    assert baseline.version == 1
    assert baseline.is_current is True
    assert baseline.total_award_value == 250000.0
    assert baseline.budget_snapshot["personnel"][0]["name"] == "PI"
    assert baseline.scope_snapshot["work_packages"][0]["name"] == "WP1"


def test_award_condition_lifecycle_defaults_to_open(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(
                db, proposal_id, {"funding_agency": "DOE", "link_budget": False},
                created_by=_id("user"), org_id=None,
            )
            await db.commit()
            award_id = award.id

        async with AsyncSessionLocal() as db:
            condition = AwardCondition(
                id=new_uuid(), award_id=award_id,
                description="Submit revised budget justification before first drawdown.",
                category="financial", created_by=_id("user"),
            )
            db.add(condition)
            await db.flush()
            condition_id = condition.id
            await db.commit()

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AwardCondition).where(AwardCondition.id == condition_id))
            fetched = result.scalar_one()
            fetched.status = "resolved"
            fetched.resolved_by = _id("user")
            await db.flush()
            await db.commit()

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AwardCondition).where(AwardCondition.id == condition_id))
            return result.scalar_one()

    condition = _run(_body())
    assert condition.category == "financial"
    assert condition.status == "resolved"
    assert condition.resolved_by is not None


# ── Phase 8 — Award Received: activation & baselining engine methods ────────

def test_activate_award_locks_baseline_and_flips_award_status(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            budget = BudgetRecord(id=new_uuid(), proposal_id=proposal_id, total_cost=75000.0, personnel=[{"name": "PI"}])
            db.add(budget)
            await db.flush()
            await db.commit()
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(
                db, proposal_id, {"funding_agency": "NSF", "total_award_value": 75000.0, "link_budget": True},
                created_by=_id("user"), org_id=None,
            )
            await db.commit()
            award_id = award.id
        assert award.award_status == "received"

        async with AsyncSessionLocal() as db:
            baseline = await engine.activate_award(db, award_id, {"notes": "Initial activation"}, created_by=_id("user"))
            await db.commit()
            return award_id, baseline

    award_id, baseline = _run(_body())
    assert baseline.version == 1
    assert baseline.is_current is True
    assert baseline.total_award_value == 75000.0
    assert baseline.budget_snapshot["total_cost"] == 75000.0
    assert baseline.budget_snapshot["personnel"] == [{"name": "PI"}]
    assert baseline.notes == "Initial activation"

    async def _reload():
        async with AsyncSessionLocal() as db:
            return await engine.get_award_or_404(db, award_id)

    reloaded = _run(_reload())
    assert reloaded.award_status == "active"


def test_activate_award_twice_raises_400(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "DOE", "link_budget": False}, created_by=_id("user"), org_id=None)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            await engine.activate_award(db, award_id, {}, created_by=_id("user"))
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.activate_award(db, award_id, {}, created_by=_id("user"))
            return exc_info.value.status_code

    assert _run(_body()) == 400


def test_create_baseline_version_without_activation_raises_400(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "NASA", "link_budget": False}, created_by=_id("user"), org_id=None)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.create_baseline_version(db, award_id, {}, created_by=_id("user"))
            return exc_info.value.status_code

    assert _run(_body()) == 400


def test_create_baseline_version_increments_and_flips_current(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "NIH", "total_award_value": 100000.0, "link_budget": False}, created_by=_id("user"), org_id=None)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            first = await engine.activate_award(db, award_id, {}, created_by=_id("user"))
            await db.commit()
            first_id = first.id
        async with AsyncSessionLocal() as db:
            second = await engine.create_baseline_version(db, award_id, {"notes": "Post-amendment re-baseline"}, created_by=_id("user"))
            await db.commit()
            return award_id, first_id, second

    award_id, first_id, second = _run(_body())
    assert second.version == 2
    assert second.is_current is True
    assert second.notes == "Post-amendment re-baseline"

    async def _reload_first():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(ProjectBaseline).where(ProjectBaseline.id == first_id))
            return result.scalar_one()

    first_reloaded = _run(_reload_first())
    assert first_reloaded.is_current is False

    async def _current():
        async with AsyncSessionLocal() as db:
            return await engine.get_current_baseline(db, award_id)

    current = _run(_current())
    assert current.id == second.id

    async def _list_all():
        async with AsyncSessionLocal() as db:
            return await engine.list_baselines(db, award_id)

    all_baselines = _run(_list_all())
    assert [b.version for b in all_baselines] == [2, 1]


# ── Phase 10 — Planned vs. Actual variance engine ────────────────────────────

def test_planned_vs_actual_before_activation_raises_400(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "NSF", "link_budget": False}, created_by=_id("user"), org_id=None)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.get_planned_vs_actual(db, award_id)
            return exc_info.value.status_code

    assert _run(_body()) == 400


def test_planned_vs_actual_computes_budget_and_schedule_variance(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            budget = BudgetRecord(id=new_uuid(), proposal_id=proposal_id, total_cost=100000.0)
            db.add(budget)

            pk = ProjectKnowledge(id=new_uuid(), proposal_id=proposal_id)
            db.add(pk)
            await db.flush()
            sow = ScopeOfWork(id=new_uuid(), project_knowledge_id=pk.id)
            db.add(sow)
            await db.flush()

            # due_month=0 milestones/deliverables are already "due" the
            # instant any time has elapsed; due_month=999 never is within
            # this test's period of performance — deterministic without
            # needing to compute an exact elapsed-time boundary.
            db.add_all([
                Milestone(id=new_uuid(), scope_of_work_id=sow.id, name="M1 behind", due_month=0, status="pending"),
                Milestone(id=new_uuid(), scope_of_work_id=sow.id, name="M2 done", due_month=0, status="complete"),
                Milestone(id=new_uuid(), scope_of_work_id=sow.id, name="M3 not due", due_month=999, status="pending"),
                Deliverable(id=new_uuid(), scope_of_work_id=sow.id, name="D1 behind", due_month=0, status="pending"),
            ])
            await db.flush()
            await db.commit()

        async with AsyncSessionLocal() as db:
            award = await engine.create_award(
                db, proposal_id, {
                    "funding_agency": "NSF", "total_award_value": 100000.0, "link_budget": True,
                    "period_of_performance_start": datetime.utcnow() - timedelta(days=200),
                    "period_of_performance_end": datetime.utcnow() + timedelta(days=200),
                },
                created_by=_id("user"), org_id=None,
            )
            await db.commit()
            award_id = award.id

        async with AsyncSessionLocal() as db:
            await engine.activate_award(db, award_id, {}, created_by=_id("user"))
            await db.commit()

        async with AsyncSessionLocal() as db:
            exp = AwardExpenditure(id=new_uuid(), award_id=award_id, category="personnel", amount=25000.0)
            db.add(exp)
            await db.flush()
            await db.commit()

        async with AsyncSessionLocal() as db:
            return await engine.get_planned_vs_actual(db, award_id)

    result = _run(_body())
    assert result.baseline_version == 1
    assert result.baseline_total_cost == 100000.0
    assert result.total_expended == 25000.0
    assert result.burn_rate_pct == 25.0
    assert result.elapsed_pct == pytest.approx(50.0, abs=3.0)
    assert result.budget_variance_pct == pytest.approx(-25.0, abs=3.0)

    assert result.planned_milestones == 3
    assert result.current_milestones == 3
    assert result.milestones_completed == 1
    assert result.milestones_behind_schedule == 1  # M1 only — M2 done, M3 not due yet

    assert result.planned_deliverables == 1
    assert result.current_deliverables == 1
    assert result.deliverables_behind_schedule == 1

    assert result.scope_drift is False


def test_planned_vs_actual_detects_scope_drift_after_baseline(client, engine):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            pk = ProjectKnowledge(id=new_uuid(), proposal_id=proposal_id)
            db.add(pk)
            await db.flush()
            sow = ScopeOfWork(id=new_uuid(), project_knowledge_id=pk.id)
            db.add(sow)
            await db.flush()
            await db.commit()

        async with AsyncSessionLocal() as db:
            award = await engine.create_award(db, proposal_id, {"funding_agency": "DOE", "link_budget": False}, created_by=_id("user"), org_id=None)
            await db.commit()
            award_id = award.id

        async with AsyncSessionLocal() as db:
            await engine.activate_award(db, award_id, {}, created_by=_id("user"))
            await db.commit()

        # Add a work package AFTER the baseline was locked — live scope has
        # now drifted from what was captured in the snapshot.
        async with AsyncSessionLocal() as db:
            sow_result = await db.execute(select(ScopeOfWork).where(ScopeOfWork.project_knowledge_id == pk.id))
            sow_row = sow_result.scalar_one()
            db.add(WorkPackage(id=new_uuid(), scope_of_work_id=sow_row.id, name="New WP added post-baseline"))
            await db.flush()
            await db.commit()

        async with AsyncSessionLocal() as db:
            return await engine.get_planned_vs_actual(db, award_id)

    result = _run(_body())
    assert result.planned_work_packages == 0
    assert result.current_work_packages == 1
    assert result.scope_drift is True
