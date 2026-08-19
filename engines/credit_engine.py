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
- Low-balance warning (post-launch addendum, prompted by customer
  discovery): `AICreditLedger.reference_balance` is the "100%" mark —
  seeded at ledger creation and reset on every top-up — so `balance /
  reference_balance` always means "% of the last top-up remaining." debit()
  fires a `credits.balance_low` webhook event (Slack/Teams/generic, via the
  existing connector framework) the moment a debit crosses
  LOW_BALANCE_WARNING_PCT, not on every debit while already below it.
- Team/department spending caps (same addendum): `CreditAllocation` gained
  optional `team_id`/`department_id` columns alongside the existing
  `user_id`. `check_allocation()` now enforces personal, team, department,
  and org-wide-default caps independently — a debit is blocked if it would
  exceed ANY applicable level, mirroring how a real budget hierarchy works
  (a department cap constrains every team inside it; a team cap constrains
  every member inside it). Credits still live in one shared org-level pool
  — these are spending ceilings on slices of that pool, not separate
  sub-ledgers. See docs/ARCHITECTURE.md for the fuller design rationale and
  why that's the right first step over literally partitioning the ledger.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import (
    AICreditLedger, CreditAllocation, CreditTransaction, Team, TeamMembership, new_uuid,
)

_log = logging.getLogger(__name__)

DEFAULT_STARTING_BALANCE = 100.0   # free allotment for a newly created org
GENERATION_COST = 1.0              # credits per AI proposal-section generation call
# Phase 6 (CLARIVA-DOCGEN-SPEC-001) — future-tense validator. A read+flag pass
# over already-generated content, not a full regeneration, so it's priced well
# below GENERATION_COST — same "lighter call, lighter price" reasoning as the
# Logic Model Chart feature's stage-regen pricing, just via the flat
# debit_or_402 pattern (like GENERATION_COST itself) rather than a full
# service-catalog entry, since there's no subscriber/payg differentiation
# needed for a utility QA check.
VALIDATION_COST = 0.25
LOW_BALANCE_WARNING_PCT = 0.20     # fire credits.balance_low when remaining/reference drops to this


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
        ledger = AICreditLedger(
            id=new_uuid(), org_id=org_id,
            balance=DEFAULT_STARTING_BALANCE, reference_balance=DEFAULT_STARTING_BALANCE,
        )
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

    async def _get_scoped_allocation(
        self, db: AsyncSession, org_id: str, *,
        user_id: Optional[str] = None, team_id: Optional[str] = None,
        department_id: Optional[str] = None,
    ) -> Optional[CreditAllocation]:
        """Look up the single allocation row matching this exact scope. Called
        with all three of user_id/team_id/department_id left None, this finds
        the org-wide default cap (all three columns are nullable and default
        to NULL for that row)."""
        result = await db.execute(
            select(CreditAllocation).where(
                CreditAllocation.org_id == org_id,
                CreditAllocation.user_id == user_id,
                CreditAllocation.team_id == team_id,
                CreditAllocation.department_id == department_id,
            )
        )
        return result.scalar_one_or_none()

    async def _spent_in_period(
        self, db: AsyncSession, org_id: str, period: str, user_ids: List[str]
    ) -> float:
        """Sum of debits attributable to any of `user_ids` — a single user for
        a personal/default cap, or every member of a team/department for a
        group cap."""
        if not user_ids:
            return 0.0
        query = select(CreditTransaction).where(
            CreditTransaction.org_id == org_id,
            CreditTransaction.user_id.in_(user_ids),
            CreditTransaction.amount < 0,
        )
        if period == "monthly":
            since = datetime.utcnow() - timedelta(days=30)
            query = query.where(CreditTransaction.created_at >= since)
        result = await db.execute(query)
        return sum(-t.amount for t in result.scalars().all())

    async def _team_member_ids(self, db: AsyncSession, team_id: str) -> List[str]:
        result = await db.execute(select(TeamMembership.user_id).where(TeamMembership.team_id == team_id))
        return [row[0] for row in result.all()]

    async def _department_member_ids(self, db: AsyncSession, department_id: str) -> List[str]:
        team_result = await db.execute(select(Team.id).where(Team.department_id == department_id))
        team_ids = [row[0] for row in team_result.all()]
        if not team_ids:
            return []
        result = await db.execute(select(TeamMembership.user_id).where(TeamMembership.team_id.in_(team_ids)))
        return list({row[0] for row in result.all()})

    async def _resolve_user_scopes(self, db: AsyncSession, user_id: str) -> tuple[List[str], List[str]]:
        """Every team the user belongs to, and every department those teams
        roll up into (a user with no team memberships has neither)."""
        result = await db.execute(select(TeamMembership.team_id).where(TeamMembership.user_id == user_id))
        team_ids = [row[0] for row in result.all()]
        department_ids: List[str] = []
        if team_ids:
            dept_result = await db.execute(
                select(Team.department_id).where(Team.id.in_(team_ids), Team.department_id.isnot(None))
            )
            department_ids = list({row[0] for row in dept_result.all()})
        return team_ids, department_ids

    @staticmethod
    def _raise_if_exceeded(allocation: CreditAllocation, already_spent: float, amount: float) -> None:
        if allocation.cap is not None and already_spent + amount > allocation.cap:
            raise AllocationCapExceededError(allocation.cap, allocation.period, already_spent + amount)

    async def check_allocation(
        self, db: AsyncSession, org_id: str, user_id: str, amount: float
    ) -> None:
        """Raises AllocationCapExceededError if this debit would exceed ANY
        applicable cap. Checked independently, each a hard ceiling on its own
        slice of the shared pool:
          1. The user's personal cap if one is set, else the org-wide default
             cap (mutually exclusive — a personal cap overrides the default,
             exactly as before this feature).
          2. Every team the user belongs to that has its own cap (spend
             summed across all of that team's members).
          3. Every department those teams roll up into that has its own cap
             (spend summed across all of that department's members, i.e.
             every member of every team in it).
        No-op if nothing is configured at any level — the org's overall
        balance is still the outer limit either way."""
        personal = await self._get_scoped_allocation(db, org_id, user_id=user_id)
        if personal and personal.cap is not None:
            spent = await self._spent_in_period(db, org_id, personal.period, [user_id])
            self._raise_if_exceeded(personal, spent, amount)
        else:
            default = await self._get_scoped_allocation(db, org_id)
            if default and default.cap is not None:
                spent = await self._spent_in_period(db, org_id, default.period, [user_id])
                self._raise_if_exceeded(default, spent, amount)

        team_ids, department_ids = await self._resolve_user_scopes(db, user_id)

        for team_id in team_ids:
            allocation = await self._get_scoped_allocation(db, org_id, team_id=team_id)
            if allocation and allocation.cap is not None:
                member_ids = await self._team_member_ids(db, team_id)
                spent = await self._spent_in_period(db, org_id, allocation.period, member_ids)
                self._raise_if_exceeded(allocation, spent, amount)

        for department_id in department_ids:
            allocation = await self._get_scoped_allocation(db, org_id, department_id=department_id)
            if allocation and allocation.cap is not None:
                member_ids = await self._department_member_ids(db, department_id)
                spent = await self._spent_in_period(db, org_id, allocation.period, member_ids)
                self._raise_if_exceeded(allocation, spent, amount)

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

        old_balance = ledger.balance
        new_balance = old_balance - amount

        # Low-balance warning: detect the debit that crosses the threshold
        # (rather than firing on every debit while already below it) so a
        # connector with an active Slack/Teams webhook gets exactly one
        # alert per crossing, not one per generation call.
        crossed_low_balance = False
        new_pct = None
        if ledger.reference_balance and ledger.reference_balance > 0:
            old_pct = old_balance / ledger.reference_balance
            new_pct = new_balance / ledger.reference_balance
            crossed_low_balance = old_pct >= LOW_BALANCE_WARNING_PCT > new_pct

        ledger.balance = new_balance
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

        if crossed_low_balance:
            # Lazy import to avoid a module-load cycle with connector_engine.
            # Best-effort and non-blocking, same as audit.py's dispatch calls
            # — a webhook failure must never break the debit that triggered it.
            try:
                from engines.connector_engine import ConnectorEngine
                await ConnectorEngine().dispatch_event(db, org_id, "credits.balance_low", {
                    "balance": ledger.balance,
                    "reference_balance": ledger.reference_balance,
                    "pct_remaining": round((new_pct or 0) * 100, 1),
                })
            except Exception:
                _log.exception("Failed to dispatch credits.balance_low event for org %s", org_id)

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
        # credit() is only ever a top-up/allotment (see debit() for spend) —
        # every top-up resets the "100%" mark, so the low-balance warning is
        # always relative to what the org most recently topped up to.
        ledger.reference_balance = ledger.balance
        db.add(CreditTransaction(
            id=new_uuid(), org_id=org_id, user_id=user_id,
            amount=amount, reason=reason, balance_after=ledger.balance,
        ))
        await db.flush()
        # See the matching comment in debit() — refresh the server-generated
        # `updated_at` now so it's safe to read synchronously afterward.
        await db.refresh(ledger)
        return ledger

    async def list_allocations(self, db: AsyncSession, org_id: str) -> List[CreditAllocation]:
        result = await db.execute(
            select(CreditAllocation).where(CreditAllocation.org_id == org_id)
            .order_by(CreditAllocation.created_at.desc())
        )
        return list(result.scalars().all())

    async def set_allocation(
        self, db: AsyncSession, org_id: str, cap: Optional[float], period: str = "monthly",
        user_id: Optional[str] = None, team_id: Optional[str] = None,
        department_id: Optional[str] = None,
    ) -> CreditAllocation:
        """Upsert the cap for exactly one scope. Callers must pass at most one
        of user_id/team_id/department_id — all three left None targets the
        org-wide default cap. Scopes are independent rows (see the
        CreditAllocation docstring), so setting a team cap never touches any
        personal or department cap."""
        if sum(1 for v in (user_id, team_id, department_id) if v) > 1:
            raise ValueError("set_allocation targets exactly one scope: user_id, team_id, or department_id")
        allocation = await self._get_scoped_allocation(
            db, org_id, user_id=user_id, team_id=team_id, department_id=department_id
        )
        if allocation:
            allocation.cap = cap
            allocation.period = period
        else:
            allocation = CreditAllocation(
                id=new_uuid(), org_id=org_id, user_id=user_id, team_id=team_id,
                department_id=department_id, cap=cap, period=period,
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
