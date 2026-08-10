"""
Engine 18 — Marketplace
Originally listing groundwork only for a future revenue channel (Clariva
Enterprise™ PRD §20: "Marketplace: a future revenue channel for
third-party template packs... connector add-ons, and premium AI
capabilities"). That scoping mirrored how SSO was "schema only" in Phase
1 — the data shape existed and was real, but the commerce layer on top of
it was deliberately deferred.

Phase 4 (Marketplace Monetization) lands the first slice of that commerce
layer: purchase_listing() below lets a buyer org pay for a published
listing out of the same shared AI Services balance
(engines/credit_engine.py's AICreditLedger, Engine 11 — the identical
dollar-denominated pool Engine 21's ServiceCatalogEngine already spends
from for on-demand AI services). Explicit product decisions made for this
first version (see docs/ARCHITECTURE.md for the fuller rationale):
  - Revenue model: the platform collects the payment; no credit flows to
    the vendor org. A real revenue-share/payout ledger is future work,
    once there are actual third-party vendors to pay out.
  - Scope: this only creates the transaction + a permanent
    MarketplacePurchase entitlement record and marks the listing
    "Purchased" for that org. Listings have no attached deliverable
    content yet (see MarketplaceListing's docstring) — what a purchase
    actually unlocks per listing_type (a downloadable file for a
    Template Pack, an enabled connector, an activated AI capability) is
    separate, listing-type-by-listing-type future work.

Design notes (same discipline as every other engine in this codebase):
every method takes an already-open AsyncSession and only flushes, never
commits — the caller's request-scoped session commits once, after the
router handler returns.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engines.credit_engine import CreditEngine, debit_or_402
from models.db_models import MarketplaceListing, MarketplacePurchase, new_uuid

LISTING_TYPES = ("template_pack", "connector", "ai_capability")
LISTING_STATUSES = ("draft", "published", "archived")

credit_engine = CreditEngine()


class MarketplaceEngine:
    async def get_listing_or_404(self, db: AsyncSession, listing_id: str) -> MarketplaceListing:
        result = await db.execute(select(MarketplaceListing).where(MarketplaceListing.id == listing_id))
        listing = result.scalar_one_or_none()
        if not listing:
            raise HTTPException(status_code=404, detail="Listing not found")
        return listing

    async def create_listing(self, db: AsyncSession, data: Dict[str, Any], created_by: str) -> MarketplaceListing:
        if data["listing_type"] not in LISTING_TYPES:
            raise HTTPException(status_code=400, detail=f"Unknown listing type: {data['listing_type']}")
        listing = MarketplaceListing(
            id=new_uuid(), vendor_org_id=data.get("vendor_org_id"), listing_type=data["listing_type"],
            name=data["name"], description=data.get("description"),
            price_cents=data.get("price_cents"), currency=data.get("currency") or "usd",
            status="draft", created_by=created_by,
        )
        db.add(listing)
        await db.flush()
        await db.refresh(listing)
        return listing

    async def update_listing(self, db: AsyncSession, listing_id: str, data: Dict[str, Any]) -> MarketplaceListing:
        listing = await self.get_listing_or_404(db, listing_id)
        if "status" in data and data["status"] is not None and data["status"] not in LISTING_STATUSES:
            raise HTTPException(status_code=400, detail=f"Unknown status: {data['status']}")
        for field in ("name", "description", "price_cents", "currency", "status"):
            if field in data and data[field] is not None:
                setattr(listing, field, data[field])
        await db.flush()
        await db.refresh(listing)
        return listing

    async def delete_listing(self, db: AsyncSession, listing_id: str) -> None:
        listing = await self.get_listing_or_404(db, listing_id)
        await db.delete(listing)
        await db.flush()

    async def list_listings_for_org(self, db: AsyncSession, vendor_org_id: str) -> List[MarketplaceListing]:
        """Every listing an org owns, any status — for that org's own management view."""
        result = await db.execute(
            select(MarketplaceListing).where(MarketplaceListing.vendor_org_id == vendor_org_id).order_by(MarketplaceListing.created_at.desc())
        )
        return list(result.scalars().all())

    async def browse(self, db: AsyncSession, listing_type: Optional[str] = None) -> List[MarketplaceListing]:
        """Published listings only — the public browse view any authenticated user can see, across all orgs."""
        query = select(MarketplaceListing).where(MarketplaceListing.status == "published")
        if listing_type:
            query = query.where(MarketplaceListing.listing_type == listing_type)
        result = await db.execute(query.order_by(MarketplaceListing.created_at.desc()))
        return list(result.scalars().all())

    # -- Purchases (Phase 4 — Marketplace Monetization) ------------------------

    async def get_purchase(
        self, db: AsyncSession, listing_id: str, buyer_org_id: str,
    ) -> Optional[MarketplacePurchase]:
        result = await db.execute(
            select(MarketplacePurchase).where(
                MarketplacePurchase.listing_id == listing_id,
                MarketplacePurchase.buyer_org_id == buyer_org_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_purchases_for_org(self, db: AsyncSession, buyer_org_id: str) -> List[MarketplacePurchase]:
        """Every listing this org has purchased — powers the "Purchased"
        badge on Browse Listings and a buyer-side purchase history."""
        result = await db.execute(
            select(MarketplacePurchase)
            .where(MarketplacePurchase.buyer_org_id == buyer_org_id)
            .order_by(MarketplacePurchase.created_at.desc())
        )
        return list(result.scalars().all())

    async def purchase_listing(
        self, db: AsyncSession, listing_id: str, buyer_org_id: str, user_id: str,
    ) -> MarketplacePurchase:
        """Charges buyer_org_id's shared AI Services balance for a published
        listing and records a permanent MarketplacePurchase entitlement.
        Validation order matters here: cheap, no-DB-write checks first
        (published? priced? not your own listing?), then the
        already-purchased check (still read-only), and only then the
        actual debit — so a rejected purchase never touches the ledger."""
        listing = await self.get_listing_or_404(db, listing_id)

        if listing.status != "published":
            raise HTTPException(status_code=400, detail="This listing is not published.")
        if listing.price_cents is None:
            raise HTTPException(
                status_code=400,
                detail="This listing has no self-serve price — contact the vendor directly.",
            )
        if listing.vendor_org_id == buyer_org_id:
            raise HTTPException(status_code=400, detail="You cannot purchase your own organization's listing.")

        existing = await self.get_purchase(db, listing_id, buyer_org_id)
        if existing:
            raise HTTPException(status_code=409, detail="Your organization already owns this listing.")

        # Reuses the exact same shared AI Services balance (AICreditLedger)
        # that on-demand AI services spend from — debit_or_402 raises the
        # right HTTPException itself for both insufficient balance and any
        # team/department spending cap the buyer is subject to.
        await debit_or_402(
            credit_engine, db, buyer_org_id, user_id, listing.price_cents / 100.0,
            reason=f"marketplace_purchase:{listing_id}",
        )

        purchase = MarketplacePurchase(
            id=new_uuid(), listing_id=listing_id, buyer_org_id=buyer_org_id,
            purchased_by=user_id, price_cents_paid=listing.price_cents,
        )
        db.add(purchase)
        await db.flush()
        await db.refresh(purchase)
        return purchase
