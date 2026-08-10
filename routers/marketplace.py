"""
Marketplace router (Clariva Enterprise™ PRD §20) — listing groundwork only,
per the PRD's own "future revenue channel" framing (see
engines/marketplace_engine.py's docstring): browsable listings, no payment
processing or install flow. Publishing/editing/archiving a listing requires
the owner-only `manage_marketplace_listings` permission on the listing's
vendor organization (see rbac.py); browsing published listings just
requires being an authenticated user, since it's meant to be seen
across organizations.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import User
from models.schemas import (
    MarketplaceListingCreate, MarketplaceListingOut, MarketplaceListingUpdate,
    MarketplacePurchaseOut, MarketplacePurchaseRequest,
)
from routers.auth import get_current_user
from routers.organizations import _assert_member, _assert_permission
from engines.marketplace_engine import MarketplaceEngine
from audit import log_action

router = APIRouter()
engine = MarketplaceEngine()


@router.get("", response_model=List[MarketplaceListingOut])
async def browse_marketplace(
    listing_type: Optional[str] = None, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    return await engine.browse(db, listing_type=listing_type)


@router.get("/mine", response_model=List[MarketplaceListingOut])
async def list_my_listings(org_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await _assert_permission(org_id, current_user.id, "manage_marketplace_listings", db)
    return await engine.list_listings_for_org(db, org_id)


@router.post("", response_model=MarketplaceListingOut)
async def create_listing(
    payload: MarketplaceListingCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    if payload.vendor_org_id:
        await _assert_permission(payload.vendor_org_id, current_user.id, "manage_marketplace_listings", db)
    listing = await engine.create_listing(db, payload.model_dump(), created_by=current_user.id)
    await db.commit()
    return listing


@router.patch("/{listing_id}", response_model=MarketplaceListingOut)
async def update_listing(
    listing_id: str, payload: MarketplaceListingUpdate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    listing = await engine.get_listing_or_404(db, listing_id)
    if not listing.vendor_org_id:
        raise HTTPException(status_code=403, detail="Platform-provided listings cannot be edited here.")
    await _assert_permission(listing.vendor_org_id, current_user.id, "manage_marketplace_listings", db)
    updated = await engine.update_listing(db, listing_id, payload.model_dump(exclude_unset=True))
    await db.commit()
    return updated


@router.delete("/{listing_id}")
async def delete_listing(listing_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    listing = await engine.get_listing_or_404(db, listing_id)
    if not listing.vendor_org_id:
        raise HTTPException(status_code=403, detail="Platform-provided listings cannot be deleted here.")
    await _assert_permission(listing.vendor_org_id, current_user.id, "manage_marketplace_listings", db)
    await engine.delete_listing(db, listing_id)
    await db.commit()
    return {"deleted": True}


# ── Purchases (Phase 4 — Marketplace Monetization) ──────────────────────────
# Buying a listing spends the buyer org's shared AI Services balance — same
# monetary consequence as confirming a service-catalog quote in
# routers/service_catalog.py, so it's gated by the identical
# `purchase_ai_services` permission (owner + editor) rather than a new one.

@router.get("/purchases", response_model=List[MarketplacePurchaseOut])
async def list_purchases(
    org_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Every listing this org already owns — any member may view this (same
    visibility model as viewing entitlements/credit balance elsewhere),
    even though only owner/editor can actually spend to buy one."""
    await _assert_member(org_id, current_user.id, db)
    return await engine.list_purchases_for_org(db, org_id)


@router.post("/{listing_id}/purchase", response_model=MarketplacePurchaseOut)
async def purchase_listing(
    listing_id: str, payload: MarketplacePurchaseRequest,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Charges payload.buyer_org_id's AI Services balance for a published
    listing and records the purchase. The engine itself validates the
    listing is published, priced, not the buyer's own, and not already
    owned before ever touching the ledger."""
    await _assert_permission(payload.buyer_org_id, current_user.id, "purchase_ai_services", db)
    purchase = await engine.purchase_listing(db, listing_id, payload.buyer_org_id, current_user.id)
    await db.commit()

    await log_action(
        db, actor_id=current_user.id, action="marketplace.listing_purchased",
        org_id=payload.buyer_org_id, object_type="marketplace_purchase", object_id=purchase.id,
        detail={"listing_id": listing_id, "price_cents_paid": purchase.price_cents_paid},
    )
    return purchase
