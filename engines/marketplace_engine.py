"""
Engine 18 — Marketplace
Listing groundwork for a future revenue channel (Clariva Enterprise™ PRD
§20: "Marketplace: a future revenue channel for third-party template
packs... connector add-ons, and premium AI capabilities" — explicitly
forward-looking framing in the PRD itself). This engine covers browsable
listings only: no payment processing, no install/activation flow. That
scoping mirrors how SSO was "schema only" in Phase 1 — the data shape
exists and is real, but the commerce layer on top of it is deliberately
future work.

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

from models.db_models import MarketplaceListing, new_uuid

LISTING_TYPES = ("template_pack", "connector", "ai_capability")
LISTING_STATUSES = ("draft", "published", "archived")


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
