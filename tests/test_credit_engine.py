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
    AllocationCapExceededError,
    CreditEngine,
    InsufficientCreditsError,
)


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
            await engine.set_allocation(db, org_id, user_id, cap=5.0, period="total")
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
            await engine.set_allocation(db, org_id, "capped-user", cap=1.0, period="total")
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
            await engine.set_allocation(db, org_id, None, cap=2.0, period="total")
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(AllocationCapExceededError):
                await engine.debit(db, org_id, "any-member", 3.0, reason="over_default_cap")

    _run(_body())
