"""
Engine 11 — AI Credit Ledger.

CreditEngine methods are async and need a real DB session, so each test
wraps its body in asyncio.run() rather than pulling in pytest-asyncio for
just this one file. The `client` fixture is included (unused directly) in
every test purely to trigger the app's lifespan startup, which is what
creates the ai_credit_ledgers/credit_transactions/credit_allocations tables
in the test database — see conftest.py.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from database import AsyncSessionLocal
from engines.credit_engine import (
    DEFAULT_STARTING_BALANCE,
    GENERATION_COST,
    LOW_BALANCE_WARNING_PCT,
    AllocationCapExceededError,
    CreditEngine,
    InsufficientCreditsError,
)
from models.db_models import Department, Team, TeamMembership, new_uuid


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def engine():
    return CreditEngine()


def _org_id() -> str:
    return f"test-org-{uuid.uuid4().hex[:12]}"


def test_generation_cost_is_positive():
    assert GENERATION_COST > 0


def test_new_org_gets_default_starting_balance(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            balance = await engine.get_balance(db, org_id)
            await db.commit()
            return balance

    assert _run(_body()) == DEFAULT_STARTING_BALANCE


def test_debit_reduces_balance_and_records_transaction(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.debit(db, org_id, "user-1", 10.0, reason="test_debit")
            await db.commit()
        async with AsyncSessionLocal() as db:
            balance = await engine.get_balance(db, org_id)
            txns = await engine.get_transactions(db, org_id)
            return balance, txns

    balance, txns = _run(_body())
    assert balance == DEFAULT_STARTING_BALANCE - 10.0
    assert len(txns) == 1
    assert txns[0].amount == -10.0
    assert txns[0].reason == "test_debit"
    assert txns[0].balance_after == balance


def test_debit_raises_when_insufficient_balance(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(InsufficientCreditsError):
                await engine.debit(db, org_id, "user-1", DEFAULT_STARTING_BALANCE + 1, reason="too_much")
            # Balance must be unchanged after a rejected debit.
            balance = await engine.get_balance(db, org_id)
            return balance

    assert _run(_body()) == DEFAULT_STARTING_BALANCE


def test_credit_increases_balance(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.credit(db, org_id, 50.0, reason="topup")
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_balance(db, org_id)

    assert _run(_body()) == DEFAULT_STARTING_BALANCE + 50.0


def test_allocation_cap_enforced_per_user(client, engine):
    org_id = _org_id()
    user_id = "capped-user"

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.set_allocation(db, org_id, cap=5.0, period="total", user_id=user_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.debit(db, org_id, user_id, 5.0, reason="first")
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(AllocationCapExceededError):
                await engine.debit(db, org_id, user_id, 1.0, reason="over_cap")

    _run(_body())


def test_allocation_cap_does_not_affect_other_users(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.set_allocation(db, org_id, cap=1.0, period="total", user_id="capped-user")
            await db.commit()
        async with AsyncSessionLocal() as db:
            # A different user in the same org with no personal or org-wide
            # cap configured should only be limited by the org's balance.
            await engine.debit(db, org_id, "uncapped-user", 20.0, reason="ok")
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_balance(db, org_id)

    assert _run(_body()) == DEFAULT_STARTING_BALANCE - 20.0


def test_org_wide_default_allocation_applies_when_no_personal_cap(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            # user_id=None => org-wide default cap for anyone without their
            # own CreditAllocation row.
            await engine.set_allocation(db, org_id, cap=2.0, period="total")
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(AllocationCapExceededError):
                await engine.debit(db, org_id, "any-member", 3.0, reason="over_default_cap")

    _run(_body())


# ── Low-balance warning (reference_balance / threshold crossing) ──────────────

def test_reference_balance_seeded_at_ledger_creation(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            ledger = await engine.get_or_create_ledger(db, org_id)
            await db.commit()
            return ledger.balance, ledger.reference_balance

    balance, reference_balance = _run(_body())
    assert balance == DEFAULT_STARTING_BALANCE
    assert reference_balance == DEFAULT_STARTING_BALANCE


def test_credit_resets_reference_balance_to_new_balance(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            # Spend some down first so balance != reference_balance going in.
            await engine.debit(db, org_id, "user-1", 40.0, reason="spend")
            await db.commit()
        async with AsyncSessionLocal() as db:
            ledger = await engine.credit(db, org_id, 200.0, reason="topup")
            await db.commit()
            return ledger.balance, ledger.reference_balance

    balance, reference_balance = _run(_body())
    # 100 - 40 + 200 = 260, and the top-up should reset the "100%" mark to it.
    assert balance == 260.0
    assert reference_balance == 260.0


def test_debit_dispatches_low_balance_event_exactly_once_on_crossing(client, engine, monkeypatch):
    org_id = _org_id()
    calls = []

    async def _fake_dispatch(self, db, dispatched_org_id, event_type, payload):
        calls.append((dispatched_org_id, event_type, payload))
        return []

    from engines import connector_engine
    monkeypatch.setattr(connector_engine.ConnectorEngine, "dispatch_event", _fake_dispatch)

    # Balance starts at DEFAULT_STARTING_BALANCE (== reference_balance).
    # Debit down to 1 credit under the warning threshold in a single call —
    # that's the crossing debit; a second, smaller debit follows while
    # already below threshold and must not re-fire the event.
    threshold_balance = DEFAULT_STARTING_BALANCE * LOW_BALANCE_WARNING_PCT
    crossing_debit = DEFAULT_STARTING_BALANCE - (threshold_balance - 1)

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.debit(db, org_id, "user-1", crossing_debit, reason="big_spend")
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.debit(db, org_id, "user-1", 1.0, reason="small_spend")
            await db.commit()

    _run(_body())
    assert len(calls) == 1
    assert calls[0][1] == "credits.balance_low"
    assert calls[0][2]["pct_remaining"] == pytest.approx(threshold_balance - 1)


def test_debit_does_not_dispatch_when_staying_above_threshold(client, engine, monkeypatch):
    org_id = _org_id()
    calls = []

    async def _fake_dispatch(self, db, dispatched_org_id, event_type, payload):
        calls.append((dispatched_org_id, event_type, payload))
        return []

    from engines import connector_engine
    monkeypatch.setattr(connector_engine.ConnectorEngine, "dispatch_event", _fake_dispatch)

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.debit(db, org_id, "user-1", 10.0, reason="small_spend")
            await db.commit()

    _run(_body())
    assert calls == []


# ── Team / department spending caps ───────────────────────────────────────────

def test_team_cap_enforced_across_all_team_members(client, engine):
    org_id = _org_id()
    team_id = new_uuid()
    user_a, user_b = "team-user-a", "team-user-b"

    async def _body():
        async with AsyncSessionLocal() as db:
            db.add(Team(id=team_id, org_id=org_id, name="Grants Team", created_by=user_a))
            db.add(TeamMembership(id=new_uuid(), team_id=team_id, user_id=user_a))
            db.add(TeamMembership(id=new_uuid(), team_id=team_id, user_id=user_b))
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.set_allocation(db, org_id, cap=10.0, period="total", team_id=team_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            # user_a spends 7 of the team's 10-credit cap.
            await engine.debit(db, org_id, user_a, 7.0, reason="a_spend")
            await db.commit()
        async with AsyncSessionLocal() as db:
            # user_b, a different member of the same team, tries to spend 5
            # more — 7 + 5 = 12 > the team's shared cap of 10, so this must
            # be blocked even though user_b personally hasn't spent anything.
            with pytest.raises(AllocationCapExceededError):
                await engine.debit(db, org_id, user_b, 5.0, reason="b_spend")

    _run(_body())


def test_department_cap_enforced_across_all_teams_in_it(client, engine):
    org_id = _org_id()
    department_id = new_uuid()
    team_1, team_2 = new_uuid(), new_uuid()
    user_a, user_b = "dept-user-a", "dept-user-b"

    async def _body():
        async with AsyncSessionLocal() as db:
            db.add(Department(id=department_id, org_id=org_id, name="R&D", created_by=user_a))
            db.add(Team(id=team_1, org_id=org_id, department_id=department_id, name="Team 1", created_by=user_a))
            db.add(Team(id=team_2, org_id=org_id, department_id=department_id, name="Team 2", created_by=user_b))
            db.add(TeamMembership(id=new_uuid(), team_id=team_1, user_id=user_a))
            db.add(TeamMembership(id=new_uuid(), team_id=team_2, user_id=user_b))
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.set_allocation(db, org_id, cap=10.0, period="total", department_id=department_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            # user_a (team_1) spends 6 of the department's shared 10-credit cap.
            await engine.debit(db, org_id, user_a, 6.0, reason="a_spend")
            await db.commit()
        async with AsyncSessionLocal() as db:
            # user_b is on a *different* team, but that team rolls up into
            # the same department — 6 + 5 = 11 > 10, so this must be blocked.
            with pytest.raises(AllocationCapExceededError):
                await engine.debit(db, org_id, user_b, 5.0, reason="b_spend")

    _run(_body())


def test_set_allocation_rejects_multiple_scopes(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(ValueError):
                await engine.set_allocation(
                    db, org_id, cap=5.0, user_id="some-user", team_id="some-team"
                )

    _run(_body())
