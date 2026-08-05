"""
Engine 11 — AI Credit Ledger
Meters and enforces the shared, organization-level AI credit pool
(Clariva Enterprise™ PRD §13 "Shared AI Credits").

This is the first concrete piece of the AI Orchestration Layer the PRD
describes in §8: rather than every engine tracking its own usage, callers
that want metering (currently: proposal generation) call
`CreditEngine.debit()` before invoking the underlying AI call, and every
existing engine's internal logic is untouched. See docs/ARCHITECTURE.md for
how future engines plug into this same pattern.

Design notes:
- Metering is opt-in per call site via an explicit org_id — a proposal
  generated with no org_id is not charged anything, so today's default
  personal-use flow is completely unaffected (see routers/proposals.py).
- A ledger and its allocations are created lazily on first use
  (get_or_create_ledger), so organizations created before this feature
  shipped work identically to ones created after it.
- Every balance change is a CreditTransaction row, so `balance` on
  AICreditLedger is always reconstructable from the transaction log if it
  ever needs auditing/repair — it's a cache, not the sole source of truth.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import AICreditLedger, CreditAllocation, CreditTransaction, new_uuid

DEFAULT_STARTING_BALANCE = 100.0   # free allotment for a newly created org
GENERATION_COST = 1.0              # credits per AI proposal-section generation call


class InsufficientCreditsError(Exception):
    """Raised internally; routers should catch and translate to HTTP 402."""

    def __init__(self, available: float, requested: float):
        self.available = available
        self.requested = requested
        super().__init__(f"Insufficient AI credits: {available} available, {requested} requested.")


class AllocationCapExceededError(Exception):
    """Raised when a per-member spending cap would be exceeded."""

    def __init__(self, cap: float, period: str, would_spend: float):
        self.cap = cap
        self.period = period
        self.would_spend = would_spend
        super().__init__(f"This would exceed your {period} allocation cap of {cap} credits.")


class CreditEngine:
    async def get_or_create_ledger(self, db: AsyncSession, org_id: str) -> AICreditLedger:
        result = await db.execute(select(AICreditLedger).where(AICreditLedger.org_id == org_id))
        ledger = result.scalar_one_or_none()
        if ledger:
            return ledger
        ledger = AICreditLedger(id=new_uuid(), org_id=org_id, balance=DEFAULT_STARTING_BALANCE)
        db.add(ledger)
        await db.flush()
        return ledger

    async def get_balance(self, db: AsyncSession, org_id: str) -> float:
        ledger = await self.get_or_create_ledger(db, org_id)
        return ledger.balance

    async def get_transactions(
        self, db: AsyncSession, org_id: str, limit: int = 50
    ) -> List[CreditTransaction]:
        result = await db.execute(
            select(CreditTransaction)
            .where(CreditTransaction.org_id == org_id)
            .order_by(CreditTransaction.created_at.desc())
            .limit(min(limit, 500))
        )
        return list(result.scalars().all())

    async def _get_allocation_cap(
        self, db: AsyncSession, org_id: str, user_id: str
    ) -> Optional[CreditAllocation]:
        """A per-user allocation if one exists, else the org-wide default (user_id IS NULL), else None."""
        result = await db.execute(
            select(CreditAllocation).where(
                CreditAllocation.org_id == org_id, CreditAllocation.user_id == user_id
            )
        )
        allocation = result.scalar_one_or_none()
        if allocation:
            return allocation
        result = await db.execute(
            select(CreditAllocation).where(
                CreditAllocation.org_id == org_id, CreditAllocation.user_id.is_(None)
            )
        )
        return result.scalar_one_or_none()

    async def _spent_in_period(
        self, db: AsyncSession, org_id: str, user_id: str, period: str
    ) -> float:
        query = select(CreditTransaction).where(
            CreditTransaction.org_id == org_id,
            CreditTransaction.user_id == user_id,
            CreditTransaction.amount < 0,
        )
        if period == "monthly":
            since = datetime.utcnow() - timedelta(days=30)
            query = query.where(CreditTransaction.created_at >= since)
        result = await db.execute(query)
        return sum(-t.amount for t in result.scalars().all())

    async def check_allocation(
        self, db: AsyncSession, org_id: str, user_id: str, amount: float
    ) -> None:
        """Raises AllocationCapExceededError if this debit would exceed the
        caller's per-user (or org-wide default) allocation cap. No-op if no
        allocation/cap is configured — the org's overall balance is still
        the outer limit either way."""
        allocation = await self._get_allocation_cap(db, org_id, user_id)
        if not allocation or allocation.cap is None:
            return
        already_spent = await self._spent_in_period(db, org_id, user_id, allocation.period)
        if already_spent + amount > allocation.cap:
            raise AllocationCapExceededError(allocation.cap, allocation.period, already_spent + amount)

    async def debit(
        self, db: AsyncSession, org_id: str, user_id: Optional[str], amount: float, reason: str
    ) -> AICreditLedger:
        """Deduct `amount` credits from the org's shared pool. Raises
        InsufficientCreditsError / AllocationCapExceededError rather than
        partially applying a debit the caller can't afford."""
        if amount <= 0:
            raise ValueError("debit amount must be positive")

        if user_id:
            await self.check_allocation(db, org_id, user_id, amount)

        ledger = await self.get_or_create_ledger(db, org_id)
        if ledger.balance < amount:
            raise InsufficientCreditsError(ledger.balance, amount)

        ledger.balance -= amount
        db.add(CreditTransaction(
            id=new_uuid(), org_id=org_id, user_id=user_id,
            amount=-amount, reason=reason, balance_after=ledger.balance,
        ))
        await db.flush()
        # `updated_at` has a server-side `onupdate=func.now()`, so this UPDATE
        # expires it — refresh now so callers can safely read ledger.balance /
        # ledger.updated_at synchronously afterward instead of hitting
        # AsyncSession's MissingGreenlet on an implicit lazy-load.
        await db.refresh(ledger)
        return ledger

    async def credit(
        self, db: AsyncSession, org_id: str, amount: float, reason: str,
        user_id: Optional[str] = None,
    ) -> AICreditLedger:
        """Add credits to the org's pool (top-up / plan allotment)."""
        if amount <= 0:
            raise ValueError("credit amount must be positive")

        ledger = await self.get_or_create_ledger(db, org_id)
        ledger.balance += amount
        db.add(CreditTransaction(
            id=new_uuid(), org_id=org_id, user_id=user_id,
            amount=amount, reason=reason, balance_after=ledger.balance,
        ))
        await db.flush()
        # See the matching comment in debit() — refresh the server-generated
        # `updated_at` now so it's safe to read synchronously afterward.
        await db.refresh(ledger)
        return ledger

    async def set_allocation(
        self, db: AsyncSession, org_id: str, user_id: Optional[str],
        cap: Optional[float], period: str = "monthly",
    ) -> CreditAllocation:
        existing = await db.execute(
            select(CreditAllocation).where(
                CreditAllocation.org_id == org_id, CreditAllocation.user_id == user_id
            )
        )
        allocation = existing.scalar_one_or_none()
        if allocation:
            allocation.cap = cap
            allocation.period = period
        else:
            allocation = CreditAllocation(
                id=new_uuid(), org_id=org_id, user_id=user_id, cap=cap, period=period,
            )
            db.add(allocation)
        await db.flush()
        return allocation


async def debit_or_402(
    engine: "CreditEngine", db: AsyncSession, org_id: str, user_id: str, amount: float, reason: str,
) -> None:
    """Convenience wrapper for routers: does the debit, or raises the
    appropriate HTTPException instead of a bare Python exception."""
    try:
        await engine.debit(db, org_id, user_id, amount, reason)
    except InsufficientCreditsError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    except AllocationCapExceededError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
