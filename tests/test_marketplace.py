"""
Phase 4 — Marketplace Monetization. Covers the purchase flow layered onto
the existing listing-groundwork Marketplace (PRD §20):
engines/marketplace_engine.py's purchase_listing()/list_purchases_for_org()
and the HTTP surface in routers/marketplace.py (permission gating, 400/402/
409 validation order, successful debit + entitlement record).

Same conventions as test_service_catalog.py: engine-level tests wrap their
body in asyncio.run() against a real DB session; router-level tests use the
`client`/`registered_user` fixtures from conftest.py. A single registered
user can own multiple organizations (each `_create_org` call makes a new
one), which is convenient here: "vendor org" and "buyer org" are just two
orgs owned by the same test user unless a test specifically needs a
different actor (e.g. permission-gating tests use a second registered user
as a non-member).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from database import AsyncSessionLocal
from engines.credit_engine import CreditEngine
from engines.marketplace_engine import MarketplaceEngine


def _run(coro):
    return asyncio.run(coro)


def _register_and_login(client, label: str) -> dict:
    email = f"{label}-{uuid.uuid4().hex[:10]}@example.com"
    password = "TestPassword123!"
    resp = client.post("/api/v1/auth/register", json={
        "email": email, "password": password,
        "full_name": f"{label.title()} User", "organization": "Test Org",
    })
    assert resp.status_code == 201, resp.text
    login = client.post("/api/v1/auth/login", data={"username": email, "password": password})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return {"email": email, "user_id": resp.json()["id"], "headers": {"Authorization": f"Bearer {token}"}}


def _create_org(client, owner_headers: dict) -> str:
    resp = client.post("/api/v1/organizations/", json={"name": f"Org {uuid.uuid4().hex[:8]}"}, headers=owner_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _create_listing(client, headers: dict, vendor_org_id: str, price_cents=10000, listing_type="template_pack") -> str:
    resp = client.post("/api/v1/marketplace", json={
        "listing_type": listing_type, "name": f"Listing {uuid.uuid4().hex[:6]}",
        "price_cents": price_cents, "vendor_org_id": vendor_org_id,
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _publish(client, headers: dict, listing_id: str) -> None:
    resp = client.patch(f"/api/v1/marketplace/{listing_id}", json={"status": "published"}, headers=headers)
    assert resp.status_code == 200, resp.text


def _set_balance(org_id: str, amount: float) -> None:
    async def _body():
        async with AsyncSessionLocal() as db:
            engine = CreditEngine()
            ledger = await engine.get_or_create_ledger(db, org_id)
            ledger.balance = amount
            await db.commit()
    _run(_body())


# ── Engine-level ─────────────────────────────────────────────────────────────

def test_purchase_listing_debits_buyer_balance_and_records_purchase(client, registered_user):
    vendor_org_id = _create_org(client, registered_user["headers"])
    buyer_org_id = _create_org(client, registered_user["headers"])
    listing_id = _create_listing(client, registered_user["headers"], vendor_org_id, price_cents=2500)
    _publish(client, registered_user["headers"], listing_id)

    async def _body():
        engine = MarketplaceEngine()
        credit_engine = CreditEngine()
        async with AsyncSessionLocal() as db:
            before = await credit_engine.get_balance(db, buyer_org_id)
        async with AsyncSessionLocal() as db:
            purchase = await engine.purchase_listing(db, listing_id, buyer_org_id, registered_user["user_id"])
            await db.commit()
        async with AsyncSessionLocal() as db:
            after = await credit_engine.get_balance(db, buyer_org_id)
        return before, after, purchase

    before, after, purchase = _run(_body())
    assert before - after == pytest.approx(25.0)
    assert purchase.price_cents_paid == 2500
    assert purchase.buyer_org_id == buyer_org_id
    assert purchase.listing_id == listing_id


def test_purchase_listing_is_blocked_when_already_owned(client, registered_user):
    vendor_org_id = _create_org(client, registered_user["headers"])
    buyer_org_id = _create_org(client, registered_user["headers"])
    listing_id = _create_listing(client, registered_user["headers"], vendor_org_id)
    _publish(client, registered_user["headers"], listing_id)

    async def _body():
        engine = MarketplaceEngine()
        async with AsyncSessionLocal() as db:
            await engine.purchase_listing(db, listing_id, buyer_org_id, registered_user["user_id"])
            await db.commit()
        async with AsyncSessionLocal() as db:
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                await engine.purchase_listing(db, listing_id, buyer_org_id, registered_user["user_id"])
            return exc_info.value.status_code
    status_code = _run(_body())
    assert status_code == 409


# ── Router: validation order (400s), 402, 409 ───────────────────────────────

def test_router_rejects_unpublished_listing(client, registered_user):
    vendor_org_id = _create_org(client, registered_user["headers"])
    buyer_org_id = _create_org(client, registered_user["headers"])
    listing_id = _create_listing(client, registered_user["headers"], vendor_org_id)  # still draft

    resp = client.post(
        f"/api/v1/marketplace/{listing_id}/purchase",
        json={"buyer_org_id": buyer_org_id}, headers=registered_user["headers"],
    )
    assert resp.status_code == 400
    assert "published" in resp.json()["detail"].lower()


def test_router_rejects_listing_with_no_price(client, registered_user):
    vendor_org_id = _create_org(client, registered_user["headers"])
    buyer_org_id = _create_org(client, registered_user["headers"])
    listing_id = _create_listing(client, registered_user["headers"], vendor_org_id, price_cents=None)
    _publish(client, registered_user["headers"], listing_id)

    resp = client.post(
        f"/api/v1/marketplace/{listing_id}/purchase",
        json={"buyer_org_id": buyer_org_id}, headers=registered_user["headers"],
    )
    assert resp.status_code == 400
    assert "price" in resp.json()["detail"].lower()


def test_router_rejects_self_purchase(client, registered_user):
    vendor_org_id = _create_org(client, registered_user["headers"])
    listing_id = _create_listing(client, registered_user["headers"], vendor_org_id)
    _publish(client, registered_user["headers"], listing_id)

    resp = client.post(
        f"/api/v1/marketplace/{listing_id}/purchase",
        json={"buyer_org_id": vendor_org_id}, headers=registered_user["headers"],
    )
    assert resp.status_code == 400
    assert "own" in resp.json()["detail"].lower()


def test_router_402_when_buyer_balance_insufficient(client, registered_user):
    vendor_org_id = _create_org(client, registered_user["headers"])
    buyer_org_id = _create_org(client, registered_user["headers"])
    listing_id = _create_listing(client, registered_user["headers"], vendor_org_id, price_cents=99999999)
    _publish(client, registered_user["headers"], listing_id)
    _set_balance(buyer_org_id, 0.0)

    resp = client.post(
        f"/api/v1/marketplace/{listing_id}/purchase",
        json={"buyer_org_id": buyer_org_id}, headers=registered_user["headers"],
    )
    assert resp.status_code == 402


def test_router_409_on_repeat_purchase(client, registered_user):
    vendor_org_id = _create_org(client, registered_user["headers"])
    buyer_org_id = _create_org(client, registered_user["headers"])
    listing_id = _create_listing(client, registered_user["headers"], vendor_org_id, price_cents=1000)
    _publish(client, registered_user["headers"], listing_id)

    first = client.post(
        f"/api/v1/marketplace/{listing_id}/purchase",
        json={"buyer_org_id": buyer_org_id}, headers=registered_user["headers"],
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/api/v1/marketplace/{listing_id}/purchase",
        json={"buyer_org_id": buyer_org_id}, headers=registered_user["headers"],
    )
    assert second.status_code == 409


def test_router_successful_purchase_returns_transaction_and_debits_balance(client, registered_user):
    vendor_org_id = _create_org(client, registered_user["headers"])
    buyer_org_id = _create_org(client, registered_user["headers"])
    listing_id = _create_listing(client, registered_user["headers"], vendor_org_id, price_cents=5000)
    _publish(client, registered_user["headers"], listing_id)

    resp = client.post(
        f"/api/v1/marketplace/{listing_id}/purchase",
        json={"buyer_org_id": buyer_org_id}, headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["price_cents_paid"] == 5000
    assert body["listing_id"] == listing_id
    assert body["buyer_org_id"] == buyer_org_id

    listed = client.get(f"/api/v1/marketplace/purchases?org_id={buyer_org_id}", headers=registered_user["headers"])
    assert listed.status_code == 200, listed.text
    assert any(p["listing_id"] == listing_id for p in listed.json())


# ── Permission gating ────────────────────────────────────────────────────────

def test_non_member_cannot_purchase(client, registered_user):
    vendor_org_id = _create_org(client, registered_user["headers"])
    buyer_org_id = _create_org(client, registered_user["headers"])
    listing_id = _create_listing(client, registered_user["headers"], vendor_org_id)
    _publish(client, registered_user["headers"], listing_id)

    stranger = _register_and_login(client, "stranger")
    resp = client.post(
        f"/api/v1/marketplace/{listing_id}/purchase",
        json={"buyer_org_id": buyer_org_id}, headers=stranger["headers"],
    )
    assert resp.status_code == 403


def test_non_member_cannot_list_purchases(client, registered_user):
    buyer_org_id = _create_org(client, registered_user["headers"])
    stranger = _register_and_login(client, "stranger")
    resp = client.get(f"/api/v1/marketplace/purchases?org_id={buyer_org_id}", headers=stranger["headers"])
    assert resp.status_code == 403
