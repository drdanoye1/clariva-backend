"""
Engine 12 — Scope of Work Engine.

Covers the deterministic, DB-only half of the engine (CRUD, staleness
flagging, budget sync). The three AI-generation methods
(generate_methodology_narrative, generate_evaluation_plan,
generate_work_breakdown) are not exercised here — they need a real OpenAI
call, same policy as proposal_generator.py's generation methods (see
conftest.py's module docstring and docs/ARCHITECTURE.md's testing
conventions section).

Like test_credit_engine.py, these methods are async and need a real DB
session, so each test wraps its body in asyncio.run() rather than pulling in
pytest-asyncio for just this one file. The `client` fixture is included
(unused directly) in every test purely to trigger the app's lifespan
startup, which is what creates the new Phase 2 tables in the test database.
Fake proposal_id strings are used throughout without creating a real
Proposal row first — SQLite's FK enforcement is off by default in this
app's setup, and test_credit_engine.py already establishes this as the
accepted pattern for isolated engine-level tests.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from database import AsyncSessionLocal
from engines.scope_of_work_engine import ScopeOfWorkEngine
from models.db_models import BudgetRecord


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def engine():
    return ScopeOfWorkEngine()


def _proposal_id() -> str:
    return f"test-proposal-{uuid.uuid4().hex[:12]}"


def test_get_or_create_project_knowledge_is_idempotent(client, engine):
    proposal_id = _proposal_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            pk1 = await engine.get_or_create_project_knowledge(db, proposal_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            pk2 = await engine.get_or_create_project_knowledge(db, proposal_id)
            await db.commit()
            return pk1.id, pk2.id

    id1, id2 = _run(_body())
    assert id1 == id2


def test_update_project_knowledge_sets_fields_and_marks_stale(client, engine):
    proposal_id = _proposal_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            pk = await engine.update_project_knowledge(db, proposal_id, {
                "objectives": "Reduce latency by 50%",
                "risks": [{"risk": "Vendor delay", "mitigation": "Dual-source"}],
                "kpis": [{"name": "Latency", "target": "50ms", "unit": "ms"}],
            })
            await db.commit()
            return pk

    pk = _run(_body())
    assert pk.objectives == "Reduce latency by 50%"
    assert pk.risks == [{"risk": "Vendor delay", "mitigation": "Dual-source"}]
    assert pk.kpis == [{"name": "Latency", "target": "50ms", "unit": "ms"}]
    assert pk.stale_flags.get("sections") is True


def test_clear_stale_flag_removes_only_that_flag(client, engine):
    proposal_id = _proposal_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            pk = await engine.get_or_create_project_knowledge(db, proposal_id)
            engine.mark_stale(pk, "sections", "budget")
            await db.commit()
        async with AsyncSessionLocal() as db:
            pk = await engine.clear_stale_flag(db, proposal_id, "sections")
            await db.commit()
            return pk

    pk = _run(_body())
    assert "sections" not in pk.stale_flags
    assert pk.stale_flags.get("budget") is True


def test_get_or_create_scope_of_work_also_creates_project_knowledge(client, engine):
    proposal_id = _proposal_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            sow = await engine.get_or_create_scope_of_work(db, proposal_id)
            pk = await engine.get_or_create_project_knowledge(db, proposal_id)
            await db.commit()
            return sow.project_knowledge_id, pk.id

    sow_pk_id, pk_id = _run(_body())
    assert sow_pk_id == pk_id


def test_update_scope_of_work_sets_methodology_and_period(client, engine):
    proposal_id = _proposal_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            sow = await engine.update_scope_of_work(db, proposal_id, {
                "period_of_performance_months": 24,
                "methodology_narrative": "We will iterate in two-week sprints.",
            })
            await db.commit()
            return sow

    sow = _run(_body())
    assert sow.period_of_performance_months == 24
    assert sow.methodology_narrative == "We will iterate in two-week sprints."


def test_create_work_package_without_cost_does_not_flag_budget(client, engine):
    proposal_id = _proposal_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            wp = await engine.create_work_package(db, proposal_id, {"name": "Design Phase"})
            pk = await engine.get_or_create_project_knowledge(db, proposal_id)
            await db.commit()
            return wp, pk

    wp, pk = _run(_body())
    assert wp.name == "Design Phase"
    assert wp.order_index == 0
    assert "budget" not in pk.stale_flags
    assert pk.stale_flags.get("sections") is True


def test_create_work_package_with_cost_flags_budget(client, engine):
    proposal_id = _proposal_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_work_package(db, proposal_id, {"name": "Build Phase", "estimated_cost": 5000.0})
            pk = await engine.get_or_create_project_knowledge(db, proposal_id)
            await db.commit()
            return pk

    pk = _run(_body())
    assert pk.stale_flags.get("budget") is True


def test_work_packages_are_ordered_by_creation(client, engine):
    proposal_id = _proposal_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_work_package(db, proposal_id, {"name": "First"})
            await engine.create_work_package(db, proposal_id, {"name": "Second"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            sow = await engine.get_or_create_scope_of_work(db, proposal_id)
            wps = await engine.list_work_packages(db, sow.id)
            await db.commit()
            return [wp.name for wp in wps]

    names = _run(_body())
    assert names == ["First", "Second"]


def test_delete_work_package_with_cost_flags_budget(client, engine):
    proposal_id = _proposal_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            wp = await engine.create_work_package(db, proposal_id, {"name": "Temp", "estimated_cost": 1000.0})
            await db.commit()
        async with AsyncSessionLocal() as db:
            pk = await engine.get_or_create_project_knowledge(db, proposal_id)
            engine.clear_stale(pk, "budget")  # simulate a prior sync clearing it
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.delete_work_package(db, proposal_id, wp.id)
            pk = await engine.get_or_create_project_knowledge(db, proposal_id)
            await db.commit()
            return pk

    pk = _run(_body())
    assert pk.stale_flags.get("budget") is True


def test_create_task_under_work_package(client, engine):
    proposal_id = _proposal_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            wp = await engine.create_work_package(db, proposal_id, {"name": "Phase 1"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            task = await engine.create_task(db, proposal_id, wp.id, {"name": "Draft spec"})
            await db.commit()
            return task

    task = _run(_body())
    assert task.name == "Draft spec"
    assert task.status == "not_started"


def test_create_task_under_missing_work_package_raises_404(client, engine):
    proposal_id = _proposal_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                await engine.create_task(db, proposal_id, "does-not-exist", {"name": "Orphan task"})
            return exc_info.value.status_code

    status_code = _run(_body())
    assert status_code == 404


def test_create_milestone_and_deliverable_against_work_package(client, engine):
    proposal_id = _proposal_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            wp = await engine.create_work_package(db, proposal_id, {"name": "Phase 1"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            milestone = await engine.create_milestone(db, proposal_id, {"name": "Kickoff", "work_package_id": wp.id, "due_month": 1})
            deliverable = await engine.create_deliverable(db, proposal_id, {"name": "Kickoff report", "work_package_id": wp.id, "deliverable_type": "report"})
            await db.commit()
            return milestone, deliverable

    milestone, deliverable = _run(_body())
    assert milestone.name == "Kickoff"
    assert milestone.status == "pending"
    assert deliverable.deliverable_type == "report"


def test_sync_budget_creates_tagged_line_item_and_preserves_manual_ones(client, engine):
    proposal_id = _proposal_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            # A manually-added Budget Builder line item, before any sync.
            budget = BudgetRecord(
                id="budget-" + proposal_id, proposal_id=proposal_id,
                other_direct=[{"id": "manual1", "category": "Materials", "description": "Widgets", "cost": 200.0}],
            )
            db.add(budget)
            await engine.create_work_package(db, proposal_id, {"name": "Fabrication", "estimated_cost": 3000.0})
            await db.commit()
        async with AsyncSessionLocal() as db:
            budget, synced = await engine.sync_budget_from_scope_of_work(db, proposal_id)
            await db.commit()
            return budget, synced

    budget, synced = _run(_body())
    assert synced == 1
    categories = {item["category"] for item in budget.other_direct}
    assert "Materials" in categories       # manual item preserved
    assert "Scope of Work" in categories   # SOW-derived item added
    assert budget.total_direct == 3200.0
    assert budget.total_cost == 3200.0     # no indirect rate set in this test


def test_sync_budget_is_idempotent_on_rerun(client, engine):
    proposal_id = _proposal_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_work_package(db, proposal_id, {"name": "Only WP", "estimated_cost": 500.0})
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.sync_budget_from_scope_of_work(db, proposal_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            budget, synced = await engine.sync_budget_from_scope_of_work(db, proposal_id)
            await db.commit()
            return budget, synced

    budget, synced = _run(_body())
    assert synced == 1
    assert len(budget.other_direct) == 1  # re-syncing replaces, not duplicates
    assert budget.total_direct == 500.0


def test_sync_budget_clears_budget_stale_flag(client, engine):
    proposal_id = _proposal_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_work_package(db, proposal_id, {"name": "WP", "estimated_cost": 100.0})
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.sync_budget_from_scope_of_work(db, proposal_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            pk = await engine.get_or_create_project_knowledge(db, proposal_id)
            await db.commit()
            return pk

    pk = _run(_body())
    assert "budget" not in pk.stale_flags
