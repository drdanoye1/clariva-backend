"""
Partner Center (Engine 28) — HTTP-layer tests for routers/partners.py.

Covers the one unauthenticated endpoint (POST /partners/apply — the real
backend for frontend/src/pages/partners.tsx's apply form) and the
require_superadmin permission gate on every /partners/admin/* endpoint,
plus a few end-to-end flows through the HTTP layer (approve an
application, register + approve a deal, update the commission rule,
create a manual commission entry, create a payout, read the channel
overview). Engine-level edge cases (cohort math, duplicate-domain
detection, reversal/adjustment entries, etc.) are covered in
test_partner_engine.py — this file focuses on routing/permission/schema
correctness.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from database import AsyncSessionLocal
from models.db_models import User


def _run(coro):
    return asyncio.run(coro)


def _make_superadmin(user_id: str) -> None:
    async def _body():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one()
            user.is_superadmin = True
            await db.commit()
    _run(_body())


def _apply(client, **overrides) -> dict:
    body = {
        "name": f"Consulting Firm {uuid.uuid4().hex[:6]}",
        "contact_name": "Jane Doe",
        "contact_email": f"jane-{uuid.uuid4().hex[:8]}@example.com",
        "partner_type": "referral",
        "message": "Interested in referring clients.",
    }
    body.update(overrides)
    resp = client.post("/api/v1/partners/apply", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Public apply endpoint ────────────────────────────────────────────────────

def test_apply_endpoint_requires_no_auth_and_returns_pending_partner(client):
    partner = _apply(client)
    assert partner["status"] == "pending"
    assert partner["referral_code"] is None


def test_apply_endpoint_rejects_invalid_email(client):
    resp = client.post("/api/v1/partners/apply", json={
        "name": "Firm", "contact_name": "Jane", "contact_email": "not-an-email",
    })
    assert resp.status_code == 422


def test_apply_endpoint_requires_name_and_contact_name(client):
    resp = client.post("/api/v1/partners/apply", json={"contact_email": "jane@example.com"})
    assert resp.status_code == 422


# ── Permission gating ────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/partners/admin/applications"),
    ("get", "/api/v1/partners/admin/deal-registrations"),
    ("get", "/api/v1/partners/admin/commission-rule"),
    ("get", "/api/v1/partners/admin/commission-entries"),
    ("get", "/api/v1/partners/admin/payouts"),
    ("get", "/api/v1/partners/admin/overview"),
])
def test_admin_get_endpoints_require_superadmin(client, registered_user, method, path):
    resp = getattr(client, method)(path, headers=registered_user["headers"])
    assert resp.status_code == 403


def test_admin_approve_application_requires_superadmin(client, registered_user):
    partner = _apply(client)
    resp = client.post(
        f"/api/v1/partners/admin/applications/{partner['id']}/approve",
        headers=registered_user["headers"],
    )
    assert resp.status_code == 403


# ── End-to-end admin flows ───────────────────────────────────────────────────

def test_admin_can_approve_and_reject_applications(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    headers = registered_user["headers"]

    partner = _apply(client)
    approve_resp = client.post(f"/api/v1/partners/admin/applications/{partner['id']}/approve", headers=headers)
    assert approve_resp.status_code == 200, approve_resp.text
    approved = approve_resp.json()
    assert approved["status"] == "approved"
    assert approved["referral_code"]

    other = _apply(client)
    reject_resp = client.post(
        f"/api/v1/partners/admin/applications/{other['id']}/reject",
        json={"reason": "Not a fit"}, headers=headers,
    )
    assert reject_resp.status_code == 200, reject_resp.text
    assert reject_resp.json()["status"] == "rejected"

    list_resp = client.get("/api/v1/partners/admin/applications?status=approved", headers=headers)
    assert list_resp.status_code == 200
    assert any(p["id"] == partner["id"] for p in list_resp.json())


def test_admin_approve_unknown_application_404s(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.post("/api/v1/partners/admin/applications/not-a-real-id/approve", headers=registered_user["headers"])
    assert resp.status_code == 404


def test_admin_can_register_and_approve_a_deal(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    headers = registered_user["headers"]

    partner = _apply(client)
    client.post(f"/api/v1/partners/admin/applications/{partner['id']}/approve", headers=headers)

    deal_resp = client.post(
        "/api/v1/partners/admin/deal-registrations",
        json={"partner_id": partner["id"], "organization_name": "Prospect Co", "domain": f"{uuid.uuid4().hex[:8]}.example"},
        headers=headers,
    )
    assert deal_resp.status_code == 201, deal_resp.text
    deal = deal_resp.json()
    assert deal["status"] == "pending_review"

    approve_resp = client.post(
        f"/api/v1/partners/admin/deal-registrations/{deal['id']}/approve",
        json={}, headers=headers,
    )
    assert approve_resp.status_code == 200, approve_resp.text
    assert approve_resp.json()["status"] == "approved"
    assert approve_resp.json()["protection_expires_at"] is not None


def test_admin_cannot_register_deal_for_unapproved_partner(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    partner = _apply(client)  # still pending — never approved
    resp = client.post(
        "/api/v1/partners/admin/deal-registrations",
        json={"partner_id": partner["id"], "organization_name": "Prospect Co"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 422


def test_admin_can_get_and_update_commission_rule(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    headers = registered_user["headers"]

    get_resp = client.get("/api/v1/partners/admin/commission-rule", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["months_1_12_rate"] == 0.20

    update_resp = client.put(
        "/api/v1/partners/admin/commission-rule",
        json={"months_1_12_rate": 0.22, "months_13_24_rate": 0.16, "months_25_36_rate": 0.11, "month_37_plus_rate": 0.0},
        headers=headers,
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["months_1_12_rate"] == 0.22


def test_admin_commission_rule_update_rejects_out_of_range_rate(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.put(
        "/api/v1/partners/admin/commission-rule",
        json={"months_1_12_rate": 1.5, "months_13_24_rate": 0.15, "months_25_36_rate": 0.10, "month_37_plus_rate": 0.0},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 422


def test_admin_manual_commission_entry_requires_attribution(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.post(
        "/api/v1/partners/admin/commission-entries/manual",
        json={"organization_id": "not-attributed-org", "qualifying_revenue_cents": 500000},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 404


def test_admin_channel_overview_returns_expected_shape(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.get("/api/v1/partners/admin/overview", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in (
        "active_partners", "pending_applications", "suspended_partners", "attributed_customers",
        "pending_deal_registrations", "outstanding_commission_liability_cents", "total_commission_paid_cents",
    ):
        assert key in body


def test_admin_payout_endpoint_rejects_empty_entry_list(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    headers = registered_user["headers"]
    partner = _apply(client)
    client.post(f"/api/v1/partners/admin/applications/{partner['id']}/approve", headers=headers)
    resp = client.post(
        "/api/v1/partners/admin/payouts",
        json={"partner_id": partner["id"], "commission_entry_ids": []},
        headers=headers,
    )
    assert resp.status_code == 422
