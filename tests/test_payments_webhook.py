"""
Post-launch addendum — real-money payment wiring tests.

Covers routers/payments.py's webhook (signature verification, idempotency,
plan-subscription activation, AI Services Fund top-up), the two checkout
endpoints' permission/validation gates, and scripts/downgrade_expired_plans.py.

No real Square HTTP calls are made anywhere in this file: create-checkout
and create-fund-checkout naturally 503 in the test environment (no Square
access token configured — same "not configured" pattern as
test_document_export_api.py::test_export_returns_503_when_storage_not_configured),
and the webhook's own outbound call (fetching an Order's metadata from
Square's Orders API) is monkeypatched at the
routers.payments._fetch_order_metadata seam so these tests exercise the
webhook's own logic (signature check, idempotency, plan/ledger effects)
without needing network access or real Square credentials.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import routers.payments as payments_router
from database import AsyncSessionLocal
from engines.credit_engine import CreditEngine
from models.db_models import Organization
from scripts import downgrade_expired_plans


def _run(coro):
    import asyncio
    return asyncio.run(coro)


WEBHOOK_URL = "https://example.herokuapp.com/api/v1/payments/webhook"
SIGNING_KEY = "test-signing-key"


@pytest.fixture(autouse=True)
def _configure_webhook_secrets(monkeypatch):
    monkeypatch.setattr(payments_router.settings, "SQUARE_WEBHOOK_SIGNATURE_KEY", SIGNING_KEY)
    monkeypatch.setattr(payments_router.settings, "SQUARE_WEBHOOK_NOTIFICATION_URL", WEBHOOK_URL)


def _sign(raw_body: bytes) -> str:
    string_to_sign = WEBHOOK_URL + raw_body.decode("utf-8")
    digest = hmac.new(SIGNING_KEY.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _post_webhook(client, event: dict, *, bad_signature: bool = False):
    raw_body = json.dumps(event).encode("utf-8")
    signature = "not-a-real-signature" if bad_signature else _sign(raw_body)
    return client.post(
        "/api/v1/payments/webhook",
        content=raw_body,
        headers={"content-type": "application/json", "x-square-hmacsha256-signature": signature},
    )


def _payment_updated_event(*, order_id: str, status: str = "COMPLETED") -> dict:
    return {
        "merchant_id": "test-merchant",
        "type": "payment.updated",
        "event_id": uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data": {
            "type": "payment",
            "id": f"payment-{uuid.uuid4().hex[:8]}",
            "object": {"payment": {"id": f"payment-{uuid.uuid4().hex[:8]}", "status": status, "order_id": order_id}},
        },
    }


def _create_org(client, owner_headers: dict) -> str:
    resp = client.post("/api/v1/organizations/", json={"name": f"Org {uuid.uuid4().hex[:8]}"}, headers=owner_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _get_org(org_id: str) -> Organization:
    async def _body():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Organization).where(Organization.id == org_id))
            return result.scalar_one()
    return _run(_body())


def _get_balance(org_id: str) -> float:
    async def _body():
        async with AsyncSessionLocal() as db:
            ledger = await CreditEngine().get_or_create_ledger(db, org_id)
            return ledger.balance
    return _run(_body())


def _set_org_plan_and_expiry(org_id: str, plan: str, expires_at) -> None:
    async def _body():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Organization).where(Organization.id == org_id))
            org = result.scalar_one()
            org.plan = plan
            org.plan_expires_at = expires_at
            await db.commit()
    _run(_body())


# ── Signature verification ──────────────────────────────────────────────────

def test_webhook_rejects_bad_signature(client):
    resp = _post_webhook(client, _payment_updated_event(order_id="order-1"), bad_signature=True)
    assert resp.status_code == 401


def test_webhook_rejects_when_notification_url_not_configured(client, monkeypatch):
    monkeypatch.setattr(payments_router.settings, "SQUARE_WEBHOOK_NOTIFICATION_URL", "")
    resp = _post_webhook(client, _payment_updated_event(order_id="order-1"))
    assert resp.status_code == 401


# ── Idempotency ──────────────────────────────────────────────────────────────

def test_webhook_ignores_unhandled_event_types(client):
    event = _payment_updated_event(order_id="order-1")
    event["type"] = "refund.updated"
    resp = _post_webhook(client, event)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_webhook_redelivery_of_same_event_id_is_a_noop(client, registered_user, monkeypatch):
    org_id = _create_org(client, registered_user["headers"])

    async def fake_fetch(base_url, access_token, order_id):
        return {"kind": "fund_topup", "org_id": org_id, "amount_cents": "1500", "initiated_by_user_id": registered_user["user_id"]}
    monkeypatch.setattr(payments_router, "_fetch_order_metadata", fake_fetch)
    monkeypatch.setattr(payments_router, "_square_config", lambda: ("https://fake", "fake-token"))

    event = _payment_updated_event(order_id="order-dup")
    raw_body = json.dumps(event).encode("utf-8")
    signature = _sign(raw_body)

    r1 = client.post("/api/v1/payments/webhook", content=raw_body, headers={"content-type": "application/json", "x-square-hmacsha256-signature": signature})
    assert r1.status_code == 200
    assert r1.json()["status"] == "applied"
    balance_after_first = _get_balance(org_id)

    # Redeliver the exact same event (same event_id) — must no-op.
    r2 = client.post("/api/v1/payments/webhook", content=raw_body, headers={"content-type": "application/json", "x-square-hmacsha256-signature": signature})
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate"
    assert _get_balance(org_id) == balance_after_first


# ── Plan subscription activation ────────────────────────────────────────────

def test_webhook_activates_plan_subscription_and_sets_expiry(client, registered_user, monkeypatch):
    org_id = _create_org(client, registered_user["headers"])

    async def fake_fetch(base_url, access_token, order_id):
        return {"kind": "plan_subscription", "org_id": org_id, "plan_id": "team", "initiated_by_user_id": registered_user["user_id"]}
    monkeypatch.setattr(payments_router, "_fetch_order_metadata", fake_fetch)
    monkeypatch.setattr(payments_router, "_square_config", lambda: ("https://fake", "fake-token"))

    before = datetime.now(timezone.utc)
    resp = _post_webhook(client, _payment_updated_event(order_id="order-plan"))
    assert resp.status_code == 200
    assert resp.json() == {"status": "applied", "kind": "plan_subscription", "org_id": org_id, "plan": "team"}

    org = _get_org(org_id)
    assert org.plan == "team"
    assert org.plan_expires_at is not None
    expires = org.plan_expires_at if org.plan_expires_at.tzinfo else org.plan_expires_at.replace(tzinfo=timezone.utc)
    # ~30 days out for a non-annual plan_id.
    assert timedelta(days=29) < (expires - before) < timedelta(days=31)


def test_webhook_annual_plan_sets_tier_without_suffix_and_365_day_expiry(client, registered_user, monkeypatch):
    org_id = _create_org(client, registered_user["headers"])

    async def fake_fetch(base_url, access_token, order_id):
        return {"kind": "plan_subscription", "org_id": org_id, "plan_id": "organization_annual", "initiated_by_user_id": registered_user["user_id"]}
    monkeypatch.setattr(payments_router, "_fetch_order_metadata", fake_fetch)
    monkeypatch.setattr(payments_router, "_square_config", lambda: ("https://fake", "fake-token"))

    before = datetime.now(timezone.utc)
    resp = _post_webhook(client, _payment_updated_event(order_id="order-annual"))
    assert resp.status_code == 200

    org = _get_org(org_id)
    assert org.plan == "organization"  # "_annual" suffix stripped for the stored tier
    expires = org.plan_expires_at if org.plan_expires_at.tzinfo else org.plan_expires_at.replace(tzinfo=timezone.utc)
    assert timedelta(days=364) < (expires - before) < timedelta(days=366)


def test_webhook_unknown_plan_id_is_ignored_not_crashed(client, registered_user, monkeypatch):
    org_id = _create_org(client, registered_user["headers"])

    async def fake_fetch(base_url, access_token, order_id):
        return {"kind": "plan_subscription", "org_id": org_id, "plan_id": "not-a-real-plan"}
    monkeypatch.setattr(payments_router, "_fetch_order_metadata", fake_fetch)
    monkeypatch.setattr(payments_router, "_square_config", lambda: ("https://fake", "fake-token"))

    resp = _post_webhook(client, _payment_updated_event(order_id="order-bad-plan"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert _get_org(org_id).plan == "free"  # untouched


# ── Fund top-up ──────────────────────────────────────────────────────────────

def test_webhook_credits_fund_topup_to_ai_services_balance(client, registered_user, monkeypatch):
    org_id = _create_org(client, registered_user["headers"])
    starting_balance = _get_balance(org_id)

    async def fake_fetch(base_url, access_token, order_id):
        return {"kind": "fund_topup", "org_id": org_id, "amount_cents": "2500", "initiated_by_user_id": registered_user["user_id"]}
    monkeypatch.setattr(payments_router, "_fetch_order_metadata", fake_fetch)
    monkeypatch.setattr(payments_router, "_square_config", lambda: ("https://fake", "fake-token"))

    resp = _post_webhook(client, _payment_updated_event(order_id="order-fund"))
    assert resp.status_code == 200
    assert resp.json() == {"status": "applied", "kind": "fund_topup", "org_id": org_id, "amount_cents": 2500}
    assert _get_balance(org_id) == pytest.approx(starting_balance + 25.0)  # $25.00


def test_webhook_ignores_non_completed_payments(client, registered_user, monkeypatch):
    org_id = _create_org(client, registered_user["headers"])
    starting_balance = _get_balance(org_id)

    async def fake_fetch(base_url, access_token, order_id):
        return {"kind": "fund_topup", "org_id": org_id, "amount_cents": "5000"}
    monkeypatch.setattr(payments_router, "_fetch_order_metadata", fake_fetch)
    monkeypatch.setattr(payments_router, "_square_config", lambda: ("https://fake", "fake-token"))

    resp = _post_webhook(client, _payment_updated_event(order_id="order-pending", status="PENDING"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert _get_balance(org_id) == starting_balance  # never touched


def test_webhook_unknown_org_id_is_ignored_not_crashed(client, monkeypatch):
    async def fake_fetch(base_url, access_token, order_id):
        return {"kind": "fund_topup", "org_id": "not-a-real-org-id", "amount_cents": "1000"}
    monkeypatch.setattr(payments_router, "_fetch_order_metadata", fake_fetch)
    monkeypatch.setattr(payments_router, "_square_config", lambda: ("https://fake", "fake-token"))
    resp = _post_webhook(client, _payment_updated_event(order_id="order-unknown-org"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


# ── Checkout endpoints (permission + not-configured gates) ─────────────────

def test_create_checkout_requires_manage_credits_permission(client, registered_user):
    # A second, unrelated user has no membership at all on this org.
    org_id = _create_org(client, registered_user["headers"])
    other_email = f"other-{uuid.uuid4().hex[:8]}@example.com"
    client.post("/api/v1/auth/register", json={"email": other_email, "password": "TestPassword123!", "full_name": "Other", "organization": "Other Org"})
    login = client.post("/api/v1/auth/login", data={"username": other_email, "password": "TestPassword123!"})
    other_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.post("/api/v1/payments/create-checkout", json={"plan_id": "professional", "org_id": org_id}, headers=other_headers)
    assert resp.status_code == 403


def test_create_checkout_503s_without_square_credentials(client, registered_user, monkeypatch):
    # Explicit, not ambient: local .env now carries real production Square
    # credentials (wired during this session's Square dashboard setup), so
    # this can no longer rely on the test environment simply lacking a
    # token. Force the "not configured" state directly.
    monkeypatch.setattr(payments_router.settings, "SQUARE_PRODUCTION_ACCESS_TOKEN", "")
    monkeypatch.setattr(payments_router.settings, "SQUARE_SANDBOX_ACCESS_TOKEN", "")
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post("/api/v1/payments/create-checkout", json={"plan_id": "professional", "org_id": org_id}, headers=registered_user["headers"])
    assert resp.status_code == 503


def test_create_checkout_rejects_unknown_plan(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post("/api/v1/payments/create-checkout", json={"plan_id": "not-a-plan", "org_id": org_id}, headers=registered_user["headers"])
    assert resp.status_code == 400


def test_create_fund_checkout_rejects_below_minimum(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post("/api/v1/payments/create-fund-checkout", json={"org_id": org_id, "amount_cents": 500}, headers=registered_user["headers"])
    assert resp.status_code == 422


def test_create_fund_checkout_503s_without_square_credentials(client, registered_user, monkeypatch):
    monkeypatch.setattr(payments_router.settings, "SQUARE_PRODUCTION_ACCESS_TOKEN", "")
    monkeypatch.setattr(payments_router.settings, "SQUARE_SANDBOX_ACCESS_TOKEN", "")
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post("/api/v1/payments/create-fund-checkout", json={"org_id": org_id, "amount_cents": 2000}, headers=registered_user["headers"])
    assert resp.status_code == 503


# ── scripts/downgrade_expired_plans.py ──────────────────────────────────────

def test_downgrade_expired_plans_downgrades_only_past_expiry(client, registered_user):
    org_expired = _create_org(client, registered_user["headers"])
    org_future = _create_org(client, registered_user["headers"])
    org_free = _create_org(client, registered_user["headers"])

    now = datetime.now(timezone.utc)
    _set_org_plan_and_expiry(org_expired, "professional", now - timedelta(days=1))
    _set_org_plan_and_expiry(org_future, "team", now + timedelta(days=10))
    # org_free is left on its default "free" plan with no expiry.

    _run(downgrade_expired_plans.main())

    expired = _get_org(org_expired)
    assert expired.plan == "free"
    assert expired.plan_expires_at is None

    future = _get_org(org_future)
    assert future.plan == "team"  # untouched — not yet expired

    free = _get_org(org_free)
    assert free.plan == "free"


def test_downgrade_expired_plans_is_idempotent(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan_and_expiry(org_id, "professional", datetime.now(timezone.utc) - timedelta(days=1))

    _run(downgrade_expired_plans.main())
    assert _get_org(org_id).plan == "free"

    # Second run finds nothing left to downgrade — must not error.
    _run(downgrade_expired_plans.main())
    assert _get_org(org_id).plan == "free"
