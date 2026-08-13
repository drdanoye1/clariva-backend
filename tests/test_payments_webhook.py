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
from engines.partner_engine import PartnerEngine
from models.db_models import CommissionEntry, Organization
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


# ── Commercial Architecture Phase 1 — Large tier + display names ───────────

def test_plans_dict_has_large_tier_at_correct_amounts():
    large = payments_router.PLANS["large"]
    assert large["amount"] == 499900  # $4,999.00
    large_annual = payments_router.PLANS["large_annual"]
    assert large_annual["amount"] == 4999000  # $49,990.00


def test_plans_dict_enterprise_priced_above_large():
    # Large ($4,999/mo) must never be more expensive than Enterprise's
    # marketing anchor — the whole point of raising Enterprise's figure in
    # this phase (see the comment on PLANS["enterprise"]["amount"]).
    assert payments_router.PLANS["enterprise"]["amount"] > payments_router.PLANS["large"]["amount"]


def test_tier_display_names_covers_every_internal_plan_key():
    # Every tier a real org can be on must have a display label — a
    # missing entry would silently fall back to the raw internal string
    # wherever tierDisplayName()/TIER_DISPLAY_NAMES.get() is used.
    for tier in ("free", "professional", "team", "organization", "large", "enterprise"):
        assert tier in payments_router.TIER_DISPLAY_NAMES
    assert payments_router.TIER_DISPLAY_NAMES["team"] == "Small"
    assert payments_router.TIER_DISPLAY_NAMES["organization"] == "Medium"
    assert payments_router.TIER_DISPLAY_NAMES["large"] == "Large"


def test_bulk_allowed_plans_includes_large():
    from routers.foa import BULK_ALLOWED_PLANS
    assert "large" in BULK_ALLOWED_PLANS
    assert set(BULK_ALLOWED_PLANS) == {"team", "organization", "large", "enterprise"}


def test_complimentary_allowance_seed_includes_large_rows():
    from engines.service_catalog_engine import COMPLIMENTARY_ALLOWANCE_SEED
    large_rows = [row for row in COMPLIMENTARY_ALLOWANCE_SEED if row[0] == "large"]
    # Same four service_keys the other three tiers get complimentary rows for.
    assert {row[1] for row in large_rows} == {
        "grant_opportunity_analysis", "doc_cover_letter",
        "proposal_development_standard", "award_setup_activation",
    }


# ── Additional Seats — create-seats-checkout + webhook ─────────────────────

def test_create_seats_checkout_requires_manage_credits_permission(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan_and_expiry(org_id, "team", None)
    other_email = f"other-{uuid.uuid4().hex[:8]}@example.com"
    client.post("/api/v1/auth/register", json={"email": other_email, "password": "TestPassword123!", "full_name": "Other", "organization": "Other Org"})
    login = client.post("/api/v1/auth/login", data={"username": other_email, "password": "TestPassword123!"})
    other_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.post("/api/v1/payments/create-seats-checkout", json={"org_id": org_id, "seat_count": 3}, headers=other_headers)
    assert resp.status_code == 403


def test_create_seats_checkout_rejects_ineligible_plan(client, registered_user):
    # Default "free" plan has no seat price — SEAT_PRICES_CENTS only
    # covers team/organization/large.
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post("/api/v1/payments/create-seats-checkout", json={"org_id": org_id, "seat_count": 2}, headers=registered_user["headers"])
    assert resp.status_code == 400

    _set_org_plan_and_expiry(org_id, "professional", None)
    resp = client.post("/api/v1/payments/create-seats-checkout", json={"org_id": org_id, "seat_count": 2}, headers=registered_user["headers"])
    assert resp.status_code == 400  # Professional is single-user, no seat add-on

    _set_org_plan_and_expiry(org_id, "enterprise", None)
    resp = client.post("/api/v1/payments/create-seats-checkout", json={"org_id": org_id, "seat_count": 2}, headers=registered_user["headers"])
    assert resp.status_code == 400  # Enterprise seats are a custom quote, not self-serve


def test_create_seats_checkout_rejects_zero_or_over_max_seats(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan_and_expiry(org_id, "team", None)
    resp = client.post("/api/v1/payments/create-seats-checkout", json={"org_id": org_id, "seat_count": 0}, headers=registered_user["headers"])
    assert resp.status_code == 422
    resp = client.post("/api/v1/payments/create-seats-checkout", json={"org_id": org_id, "seat_count": 9999}, headers=registered_user["headers"])
    assert resp.status_code == 422


def test_create_seats_checkout_503s_without_square_credentials(client, registered_user, monkeypatch):
    monkeypatch.setattr(payments_router.settings, "SQUARE_PRODUCTION_ACCESS_TOKEN", "")
    monkeypatch.setattr(payments_router.settings, "SQUARE_SANDBOX_ACCESS_TOKEN", "")
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan_and_expiry(org_id, "organization", None)
    resp = client.post("/api/v1/payments/create-seats-checkout", json={"org_id": org_id, "seat_count": 5}, headers=registered_user["headers"])
    assert resp.status_code == 503


def test_create_seats_checkout_prices_against_current_plan_tier(client, registered_user, monkeypatch):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan_and_expiry(org_id, "organization", None)  # Medium — $20/seat/mo

    captured = {}

    async def fake_create_order_payment_link(**kwargs):
        captured.update(kwargs)
        return {"url": "https://squareup.example/checkout/fake"}

    async def fake_get_location_id(base_url, token):
        return "loc-1"

    monkeypatch.setattr(payments_router, "_create_order_payment_link", fake_create_order_payment_link)
    monkeypatch.setattr(payments_router, "_square_config", lambda: ("https://fake", "fake-token"))
    monkeypatch.setattr(payments_router, "_get_location_id", fake_get_location_id)

    resp = client.post("/api/v1/payments/create-seats-checkout", json={"org_id": org_id, "seat_count": 4}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["seat_count"] == 4
    assert body["amount_cents"] == 4 * 2000  # 4 seats * $20.00
    assert captured["amount_cents"] == 4 * 2000
    assert captured["metadata"]["kind"] == "seat_purchase"
    assert captured["metadata"]["seat_count"] == 4
    assert captured["metadata"]["plan_at_purchase"] == "organization"


def test_webhook_applies_seat_purchase_and_increments_purchased_seats(client, registered_user, monkeypatch):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan_and_expiry(org_id, "team", None)
    assert _get_org(org_id).purchased_seats == 0

    async def fake_fetch(base_url, access_token, order_id):
        return {"kind": "seat_purchase", "org_id": org_id, "seat_count": "3", "plan_at_purchase": "team", "initiated_by_user_id": registered_user["user_id"]}
    monkeypatch.setattr(payments_router, "_fetch_order_metadata", fake_fetch)
    monkeypatch.setattr(payments_router, "_square_config", lambda: ("https://fake", "fake-token"))

    resp = _post_webhook(client, _payment_updated_event(order_id="order-seats"))
    assert resp.status_code == 200
    assert resp.json() == {"status": "applied", "kind": "seat_purchase", "org_id": org_id, "seat_count": 3, "total_purchased_seats": 3}
    assert _get_org(org_id).purchased_seats == 3

    # A second purchase adds on top of the first rather than replacing it.
    async def fake_fetch_2(base_url, access_token, order_id):
        return {"kind": "seat_purchase", "org_id": org_id, "seat_count": "2", "plan_at_purchase": "team", "initiated_by_user_id": registered_user["user_id"]}
    monkeypatch.setattr(payments_router, "_fetch_order_metadata", fake_fetch_2)
    resp2 = _post_webhook(client, _payment_updated_event(order_id="order-seats-2"))
    assert resp2.status_code == 200
    assert _get_org(org_id).purchased_seats == 5


def test_webhook_ignores_seat_purchase_with_invalid_seat_count(client, registered_user, monkeypatch):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan_and_expiry(org_id, "team", None)

    async def fake_fetch(base_url, access_token, order_id):
        return {"kind": "seat_purchase", "org_id": org_id, "seat_count": "0"}
    monkeypatch.setattr(payments_router, "_fetch_order_metadata", fake_fetch)
    monkeypatch.setattr(payments_router, "_square_config", lambda: ("https://fake", "fake-token"))

    resp = _post_webhook(client, _payment_updated_event(order_id="order-bad-seats"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert _get_org(org_id).purchased_seats == 0


def test_downgrade_expired_plans_resets_purchased_seats(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan_and_expiry(org_id, "team", datetime.now(timezone.utc) - timedelta(days=1))

    async def _bump_seats():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Organization).where(Organization.id == org_id))
            org = result.scalar_one()
            org.purchased_seats = 7
            await db.commit()
    _run(_bump_seats())
    assert _get_org(org_id).purchased_seats == 7

    _run(downgrade_expired_plans.main())

    org = _get_org(org_id)
    assert org.plan == "free"
    assert org.purchased_seats == 0


# ── Partner Center (Engine 28) — webhook → commission integration ──────────
#
# Confirms the hook added to routers/payments.py's `plan_subscription`
# branch (see PartnerEngine.record_commission's docstring) actually fires
# end-to-end through the real HTTP webhook: an org attributed to an
# approved partner gets a CommissionEntry on a plan_subscription webhook;
# an unattributed org (the common case) gets none, and the webhook's own
# response/plan-activation behavior is unaffected either way.

def _commission_entries_for_org(org_id: str) -> list[CommissionEntry]:
    async def _body():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(CommissionEntry).where(CommissionEntry.organization_id == org_id))
            return list(result.scalars().all())
    return _run(_body())


def _active_month_1_12_commission_rate() -> float:
    # The commission rule is a single shared, mutable, versioned row (see
    # PartnerEngine.get_active_commission_rule) — other test modules
    # (test_partners_api.py's commission-rule update test, in particular)
    # legitimately change it within the same shared test-session database
    # (see conftest.py: one SQLite file for the whole pytest run, not a
    # fresh DB per test). Asserting against a hardcoded 0.20 here would
    # make this test's pass/fail depend on file collection order across
    # the whole suite — same class of bug as the AIServiceTransaction
    # count fix elsewhere in this file's history. Read whatever rate is
    # actually active instead.
    async def _body():
        async with AsyncSessionLocal() as db:
            rule = await PartnerEngine().get_active_commission_rule(db)
            return rule.months_1_12_rate
    return _run(_body())


def _approved_partner_referral_code(label: str) -> str:
    async def _body():
        async with AsyncSessionLocal() as db:
            engine = PartnerEngine()
            partner = await engine.apply(
                db, name=f"Integration Partner {label}", contact_name="Pat Partner",
                contact_email=f"partner-{uuid.uuid4().hex[:8]}@example.com",
            )
            await db.commit()
            partner = await engine.approve_partner(db, partner.id, actor_id="test-actor")
            await db.commit()
            return partner.referral_code
    return _run(_body())


def test_webhook_plan_subscription_creates_commission_entry_for_attributed_org(client, registered_user, monkeypatch):
    referral_code = _approved_partner_referral_code("A")
    org_resp = client.post(
        "/api/v1/organizations/",
        json={"name": f"Attributed Org {uuid.uuid4().hex[:8]}", "referral_code": referral_code},
        headers=registered_user["headers"],
    )
    assert org_resp.status_code == 201, org_resp.text
    org_id = org_resp.json()["id"]

    async def fake_fetch(base_url, access_token, order_id):
        return {"kind": "plan_subscription", "org_id": org_id, "plan_id": "team", "initiated_by_user_id": registered_user["user_id"]}
    monkeypatch.setattr(payments_router, "_fetch_order_metadata", fake_fetch)
    monkeypatch.setattr(payments_router, "_square_config", lambda: ("https://fake", "fake-token"))

    resp = _post_webhook(client, _payment_updated_event(order_id="order-partner-commission"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"
    assert _get_org(org_id).plan == "team"  # plan activation is unaffected by the commission hook

    entries = _commission_entries_for_org(org_id)
    assert len(entries) == 1
    entry = entries[0]
    expected_rate = _active_month_1_12_commission_rate()  # month 1 of the 1-12 cohort bucket
    assert entry.qualifying_revenue_cents == payments_router.PLANS["team"]["amount"]
    assert entry.commission_rate == expected_rate
    assert entry.commission_amount_cents == round(payments_router.PLANS["team"]["amount"] * expected_rate)
    assert entry.status == "pending"


def test_webhook_plan_subscription_creates_no_commission_entry_for_unattributed_org(client, registered_user, monkeypatch):
    org_id = _create_org(client, registered_user["headers"])  # no referral_code — the common case

    async def fake_fetch(base_url, access_token, order_id):
        return {"kind": "plan_subscription", "org_id": org_id, "plan_id": "team", "initiated_by_user_id": registered_user["user_id"]}
    monkeypatch.setattr(payments_router, "_fetch_order_metadata", fake_fetch)
    monkeypatch.setattr(payments_router, "_square_config", lambda: ("https://fake", "fake-token"))

    resp = _post_webhook(client, _payment_updated_event(order_id="order-no-attribution"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"
    assert _get_org(org_id).plan == "team"

    assert _commission_entries_for_org(org_id) == []


def test_webhook_plan_subscription_survives_partner_engine_failure(client, registered_user, monkeypatch):
    # The webhook's plan_subscription branch wraps the commission call in a
    # bare try/except specifically so a partner-engine bug can never block
    # a paying customer's plan activation (see routers/payments.py's inline
    # comment at the hook site). Simulate that failure directly.
    referral_code = _approved_partner_referral_code("B")
    org_resp = client.post(
        "/api/v1/organizations/",
        json={"name": f"Attributed Org {uuid.uuid4().hex[:8]}", "referral_code": referral_code},
        headers=registered_user["headers"],
    )
    org_id = org_resp.json()["id"]

    async def fake_fetch(base_url, access_token, order_id):
        return {"kind": "plan_subscription", "org_id": org_id, "plan_id": "team", "initiated_by_user_id": registered_user["user_id"]}
    monkeypatch.setattr(payments_router, "_fetch_order_metadata", fake_fetch)
    monkeypatch.setattr(payments_router, "_square_config", lambda: ("https://fake", "fake-token"))

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated partner engine failure")
    monkeypatch.setattr(payments_router.partner_engine, "record_commission", _boom)

    resp = _post_webhook(client, _payment_updated_event(order_id="order-partner-engine-boom"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"
    assert _get_org(org_id).plan == "team"  # plan activation still applied despite the exception
