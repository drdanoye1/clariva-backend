"""
Engine 20 — Portfolio Dashboard (Version 3.0 architecture upgrade, Phase 12).

Like the other engine test files, methods are async and need a real DB
session, so each test wraps its body in asyncio.run(). Fake proposal_id/
user_id/org_id strings are used throughout without creating real
Proposal/User/Organization rows for the FK-less fields — SQLite's FK
enforcement is off by default in this app's setup (see
test_scope_of_work_engine.py's identical note). Every ID that could
collide across test functions is generated fresh per test via `_id()`.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from database import AsyncSessionLocal
from engines.award_engine import AwardEngine
from engines.portfolio_engine import PortfolioEngine
from models.db_models import (
    AICreditLedger, AwardExpenditure, BudgetRecord, Milestone,
    OrgProposal, Proposal, ProjectKnowledge, ScopeOfWork, new_uuid,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def engine():
    return PortfolioEngine()


@pytest.fixture()
def award_engine():
    return AwardEngine()


def _id(prefix: str) -> str:
    return f"test-{prefix}-{uuid.uuid4().hex[:12]}"


def test_pre_award_count_excludes_proposals_with_awards(client, engine, award_engine):
    user_id = _id("user")
    proposal_with_award = _id("proposal")
    proposal_without_award = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            db.add(Proposal(id=proposal_with_award, owner_id=user_id, title="A", agency="NSF", phase="phase_i"))
            db.add(Proposal(id=proposal_without_award, owner_id=user_id, title="B", agency="NSF", phase="phase_i"))
            await db.flush()
            await db.commit()
        async with AsyncSessionLocal() as db:
            await award_engine.create_award(db, proposal_with_award, {"funding_agency": "NSF", "link_budget": False}, created_by=user_id, org_id=None)
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_summary(db, user_id, org_ids=[])

    summary = _run(_body())
    assert summary.pre_award_count == 1
    assert summary.award_received_count == 1  # new awards default to "received"
    assert summary.post_award_count == 0


def test_org_shared_proposal_counted_once(client, engine):
    user_id = _id("user")
    org_id = _id("org")
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            db.add(Proposal(id=proposal_id, owner_id=_id("other-owner"), title="Shared", agency="NIH", phase="phase_i"))
            db.add(OrgProposal(id=new_uuid(), org_id=org_id, proposal_id=proposal_id, shared_by=user_id))
            await db.flush()
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_summary(db, user_id, org_ids=[org_id])

    summary = _run(_body())
    assert summary.pre_award_count == 1


def test_award_received_condition_alert(client, engine, award_engine):
    user_id = _id("user")
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            db.add(Proposal(id=proposal_id, owner_id=user_id, title="C", agency="DOE", phase="phase_i"))
            await db.flush()
            award = await award_engine.create_award(db, proposal_id, {"funding_agency": "DOE", "link_budget": False}, created_by=user_id, org_id=None)
            await db.commit()
            award_id = award.id
        async with AsyncSessionLocal() as db:
            await award_engine.create_condition(db, award_id, {"description": "Open one"}, created_by=user_id)
            resolved = await award_engine.create_condition(db, award_id, {"description": "Resolved one"}, created_by=user_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            await award_engine.update_condition(db, resolved.id, {"status": "resolved"}, resolved_by=user_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_summary(db, user_id, org_ids=[])

    summary = _run(_body())
    assert summary.open_sponsor_conditions == 1
    assert any("sponsor condition" in a.message for a in summary.alerts)


def test_active_award_over_budget_and_behind_schedule_alerts(client, engine, award_engine):
    user_id = _id("user")
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            db.add(Proposal(id=proposal_id, owner_id=user_id, title="D", agency="NASA", phase="phase_i"))
            budget = BudgetRecord(id=new_uuid(), proposal_id=proposal_id, total_cost=10000.0)
            db.add(budget)
            pk = ProjectKnowledge(id=new_uuid(), proposal_id=proposal_id)
            db.add(pk)
            await db.flush()
            sow = ScopeOfWork(id=new_uuid(), project_knowledge_id=pk.id)
            db.add(sow)
            await db.flush()
            db.add(Milestone(id=new_uuid(), scope_of_work_id=sow.id, name="M1", due_month=0, status="pending"))
            await db.flush()
            await db.commit()

        async with AsyncSessionLocal() as db:
            award = await award_engine.create_award(
                db, proposal_id, {
                    "funding_agency": "NASA", "link_budget": True,
                    "period_of_performance_start": datetime.utcnow() - timedelta(days=100),
                    "period_of_performance_end": datetime.utcnow() + timedelta(days=100),
                },
                created_by=user_id, org_id=None,
            )
            await db.commit()
            award_id = award.id

        async with AsyncSessionLocal() as db:
            await award_engine.activate_award(db, award_id, {}, created_by=user_id)
            await db.commit()

        async with AsyncSessionLocal() as db:
            # 90% burn vs. ~50% elapsed time — well over the 10-point overspend threshold.
            db.add(AwardExpenditure(id=new_uuid(), award_id=award_id, category="personnel", amount=9000.0))
            await db.flush()
            await db.commit()

        async with AsyncSessionLocal() as db:
            return await engine.get_summary(db, user_id, org_ids=[])

    summary = _run(_body())
    assert summary.post_award_count == 1
    assert summary.awards_over_budget == 1
    assert summary.milestones_behind_schedule == 1
    assert any("overspending" in a.message for a in summary.alerts)
    assert any("behind schedule" in a.message for a in summary.alerts)


def test_low_credit_balance_generates_info_alert(client, engine):
    user_id = _id("user")
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            db.add(AICreditLedger(id=new_uuid(), org_id=org_id, balance=10.0, reference_balance=100.0))
            await db.flush()
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_summary(db, user_id, org_ids=[org_id])

    summary = _run(_body())
    assert any(a.severity == "info" and "credit" in a.message for a in summary.alerts)


def test_no_data_returns_zeroed_summary(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            return await engine.get_summary(db, _id("user"), org_ids=[])

    summary = _run(_body())
    assert summary.pre_award_count == 0
    assert summary.award_received_count == 0
    assert summary.post_award_count == 0
    assert summary.alerts == []
