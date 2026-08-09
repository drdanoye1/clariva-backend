"""
On-Demand AI Services Marketplace router — Phase 2 of the Enterprise
Public-Facing Pricing & Internal Engineering Economics spec (v1.0, Aug
2026), §9.2.

Mounted at /api/v1/service-catalog. The catalog-listing endpoint is
global (no org scoping — it's the same centrally-priced menu for every
org); every other endpoint is scoped under an org_id, same convention as
credits.py sharing organizations.py's prefix.

Flow every caller (frontend or a future partner integration) is expected
to follow before triggering a paid AI generation:
  1. GET  /{org_id}/quote/{service_key}   — price + funding source preview
  2. Caller shows the user that quote and gets explicit confirmation
  3. POST /{org_id}/consume               — charges (complimentary or paid)
     and returns the transaction record actually written
Only step 3 spends anything; step 1 is side-effect free (aside from lazily
granting first-time complimentary entitlements, which costs the org
nothing). This satisfies the spec's acceptance criteria: no paid
generation without authorization, every paid transaction records its
funding source and price, and complimentary vs paid usage is always
distinguishable in the transaction log.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import User
from models.schemas import (
    AIServiceTransactionOut, OrgServiceSummaryOut, ServiceCatalogItemOut,
    ServiceConsumeRequest, ServiceEntitlementOut, ServiceQuoteOut,
)
from routers.auth import get_current_user
from routers.organizations import _assert_member, _assert_permission
from engines.credit_engine import InsufficientCreditsError
from engines.service_catalog_engine import ServiceCatalogEngine, ServiceNotFoundError
from audit import log_action

router = APIRouter()
engine = ServiceCatalogEngine()


@router.get("/", response_model=List[ServiceCatalogItemOut])
async def list_service_catalog(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The full centrally-priced service menu — same list for every org.
    Any authenticated user may view prices (this is public-facing pricing
    information, not org-scoped data)."""
    items = await engine.list_catalog(db)
    return items


@router.get("/{org_id}/quote/{service_key}", response_model=ServiceQuoteOut)
async def quote_service(
    org_id: str,
    service_key: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Any member can preview a price — this is what powers the
    price-before-generation confirmation dialog. Side-effect free except
    for lazily granting first-time complimentary entitlements."""
    await _assert_member(org_id, current_user.id, db)
    try:
        quote = await engine.quote(db, org_id, service_key)
    except ServiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return ServiceQuoteOut(**quote)


@router.post("/{org_id}/consume", response_model=AIServiceTransactionOut)
async def consume_service(
    org_id: str,
    body: ServiceConsumeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Charges for one unit of `service_key` — complimentary allowance
    first, else the org's paid AI Services balance. Gated to
    purchase_ai_services (owner/editor) — the same authorization gate the
    spec's acceptance criteria require before any paid generation.
    Callers should have already shown the user the matching
    GET .../quote/{service_key} result and gotten explicit confirmation;
    this endpoint does not re-confirm, it charges."""
    await _assert_permission(org_id, current_user.id, "purchase_ai_services", db)
    try:
        transaction = await engine.consume(
            db, org_id, current_user.id, body.service_key, reference=body.reference,
        )
    except ServiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InsufficientCreditsError as exc:
        raise HTTPException(status_code=402, detail=str(exc))

    await log_action(
        db, actor_id=current_user.id, action="ai_service.consumed",
        org_id=org_id, object_type="ai_service_transaction", object_id=transaction.id,
        detail={
            "service_key": transaction.service_key,
            "funding_source": transaction.funding_source,
            "price_cents": transaction.price_cents,
        },
    )
    return transaction


@router.get("/{org_id}/entitlements", response_model=List[ServiceEntitlementOut])
async def list_entitlements(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Any member can see the org's live complimentary allowances (what's
    left, and when it expires) — same visibility model as the AI credit
    balance in credits.py."""
    await _assert_member(org_id, current_user.id, db)
    entitlements = await engine.list_entitlements(db, org_id)
    return entitlements


@router.get("/{org_id}/summary", response_model=OrgServiceSummaryOut)
async def get_org_service_summary(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Owner-only rollup (manage_service_funding) — balance, entitlements,
    and recent transaction history in one call, for an org usage/billing
    view. Individual members already see their own remaining entitlements
    via the lighter /entitlements endpoint; the full transaction log
    (who spent what, from which funding source) is funding-management
    territory, same discipline as manage_credits gating credits.py's
    transaction/allocation endpoints."""
    await _assert_permission(org_id, current_user.id, "manage_service_funding", db)
    summary = await engine.get_org_summary(db, org_id)
    return OrgServiceSummaryOut(
        ai_services_balance_cents=summary["ai_services_balance_cents"],
        entitlements=summary["entitlements"],
        transactions=summary["transactions"],
    )
