"""
AI Credits router — shared organization-level AI credit pool
(Clariva Enterprise™ PRD §13). Mounted under the same /api/v1/organizations
prefix as organizations.py, the same way budget_export.py shares
budget.py's prefix.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import User
from models.schemas import (
    CreditAllocationOut, CreditAllocationRequest, CreditBalanceOut,
    CreditTopupRequest, CreditTransactionOut,
)
from routers.auth import get_current_user
from routers.organizations import _assert_member, _assert_permission
from engines.credit_engine import CreditEngine, debit_or_402
from audit import log_action

router = APIRouter()
credit_engine = CreditEngine()


@router.get("/{org_id}/credits", response_model=CreditBalanceOut)
async def get_credit_balance(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Any member can see the org's shared balance."""
    await _assert_member(org_id, current_user.id, db)
    ledger = await credit_engine.get_or_create_ledger(db, org_id)
    return CreditBalanceOut(org_id=org_id, balance=ledger.balance, updated_at=ledger.updated_at)


@router.get("/{org_id}/credits/transactions", response_model=List[CreditTransactionOut])
async def get_credit_transactions(
    org_id: str,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_member(org_id, current_user.id, db)
    transactions = await credit_engine.get_transactions(db, org_id, limit=limit)
    return [
        CreditTransactionOut(
            id=t.id, user_id=t.user_id, amount=t.amount,
            reason=t.reason, balance_after=t.balance_after, created_at=t.created_at,
        )
        for t in transactions
    ]


@router.post("/{org_id}/credits/topup", response_model=CreditBalanceOut)
async def topup_credits(
    org_id: str,
    body: CreditTopupRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manual top-up. Gated to manage_credits (owner) — real billing-linked
    top-ups are a Phase 6 integration (payments.py) concern; this is the
    admin-facing lever until that's wired up."""
    await _assert_permission(org_id, current_user.id, "manage_credits", db)
    ledger = await credit_engine.credit(db, org_id, body.amount, body.reason, user_id=current_user.id)

    await log_action(db, actor_id=current_user.id, action="credits.topup",
                      org_id=org_id, object_type="ai_credit_ledger", object_id=ledger.id,
                      detail={"amount": body.amount, "reason": body.reason})

    return CreditBalanceOut(org_id=org_id, balance=ledger.balance, updated_at=ledger.updated_at)


@router.get("/{org_id}/credits/allocations", response_model=List[CreditAllocationOut])
async def list_allocations(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_permission(org_id, current_user.id, "manage_credits", db)
    from sqlalchemy import select
    from models.db_models import CreditAllocation
    result = await db.execute(select(CreditAllocation).where(CreditAllocation.org_id == org_id))
    return [
        CreditAllocationOut(id=a.id, user_id=a.user_id, cap=a.cap, period=a.period)
        for a in result.scalars().all()
    ]


@router.post("/{org_id}/credits/allocations", response_model=CreditAllocationOut)
async def set_allocation(
    org_id: str,
    body: CreditAllocationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set (or clear, with cap=None) a per-member spending cap, or the
    org-wide default cap when user_id is omitted."""
    await _assert_permission(org_id, current_user.id, "manage_credits", db)
    allocation = await credit_engine.set_allocation(db, org_id, body.user_id, body.cap, body.period)

    await log_action(db, actor_id=current_user.id, action="credits.allocation_set",
                      org_id=org_id, object_type="credit_allocation", object_id=allocation.id,
                      detail={"user_id": body.user_id, "cap": body.cap, "period": body.period})

    return CreditAllocationOut(
        id=allocation.id, user_id=allocation.user_id, cap=allocation.cap, period=allocation.period,
    )
