"""Engine 18 — Marketplace. See engines/marketplace_engine.py's docstring
for the deliberate "listing groundwork only" scope of this phase."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from database import AsyncSessionLocal
from engines.marketplace_engine import MarketplaceEngine


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def engine():
    return MarketplaceEngine()


def _id(prefix: str) -> str:
    return f"test-{prefix}-{uuid.uuid4().hex[:12]}"


def test_create_listing_rejects_unknown_type(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.create_listing(db, {"listing_type": "not_real", "name": "X"}, created_by=_id("user"))
            return exc_info.value.status_code

    assert _run(_body()) == 400


def test_browse_only_returns_published_listings(client, engine):
    vendor_org = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            draft = await engine.create_listing(db, {"listing_type": "template_pack", "name": "Draft Pack", "vendor_org_id": vendor_org}, created_by=_id("user"))
            published = await engine.create_listing(db, {"listing_type": "template_pack", "name": "Published Pack", "vendor_org_id": vendor_org}, created_by=_id("user"))
            await db.commit()
            draft_id, published_id = draft.id, published.id
        async with AsyncSessionLocal() as db:
            await engine.update_listing(db, published_id, {"status": "published"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            browsed = await engine.browse(db)
            all_for_org = await engine.list_listings_for_org(db, vendor_org)
            return browsed, all_for_org, draft_id, published_id

    browsed, all_for_org, draft_id, published_id = _run(_body())
    browsed_ids = {l.id for l in browsed}
    assert published_id in browsed_ids
    assert draft_id not in browsed_ids
    assert {l.id for l in all_for_org} == {draft_id, published_id}


def test_update_listing_rejects_unknown_status(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            listing = await engine.create_listing(db, {"listing_type": "connector", "name": "X"}, created_by=_id("user"))
            await db.commit()
            listing_id = listing.id
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.update_listing(db, listing_id, {"status": "not_a_status"})
            return exc_info.value.status_code

    assert _run(_body()) == 400


def test_delete_listing(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            listing = await engine.create_listing(db, {"listing_type": "ai_capability", "name": "X"}, created_by=_id("user"))
            await db.commit()
            listing_id = listing.id
        async with AsyncSessionLocal() as db:
            await engine.delete_listing(db, listing_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.get_listing_or_404(db, listing_id)
            return exc_info.value.status_code

    assert _run(_body()) == 404
