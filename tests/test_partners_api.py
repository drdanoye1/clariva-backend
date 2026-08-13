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


# ── Admin: per-partner attributed customers (Admin Console org picker) ──────

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


def _create_org(client, owner_headers: dict, referral_code: str = None) -> str:
    body = {"name": f"Org {uuid.uuid4().hex[:8]}"}
    if referral_code:
        body["referral_code"] = referral_code
    resp = client.post("/api/v1/organizations/", json=body, headers=owner_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_admin_list_partner_customers_requires_superadmin(client, registered_user):
    partner = _apply(client)
    resp = client.get(
        f"/api/v1/partners/admin/applications/{partner['id']}/customers",
        headers=registered_user["headers"],
    )
    assert resp.status_code == 403


def test_admin_list_partner_customers_unknown_partner_404s(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.get(
        "/api/v1/partners/admin/applications/not-a-real-id/customers",
        headers=registered_user["headers"],
    )
    assert resp.status_code == 404


def test_admin_list_partner_customers_returns_attributed_orgs(client, registered_user):
    """The endpoint behind the Admin Console's "Customers" toggle — see
    admin-partners.tsx's handleToggleCustomers / handleUseForManualEntry.
    Confirms both the empty state (a fresh approval with no signups yet)
    and the populated state once a real Organization is created with the
    partner's referral code."""
    _make_superadmin(registered_user["user_id"])
    headers = registered_user["headers"]

    partner = _apply(client)
    approve_resp = client.post(f"/api/v1/partners/admin/applications/{partner['id']}/approve", headers=headers)
    assert approve_resp.status_code == 200, approve_resp.text
    approved = approve_resp.json()

    empty_resp = client.get(f"/api/v1/partners/admin/applications/{partner['id']}/customers", headers=headers)
    assert empty_resp.status_code == 200
    assert empty_resp.json() == []

    owner = _register_and_login(client, "customerowner")
    org_id = _create_org(client, owner["headers"], referral_code=approved["referral_code"])

    resp = client.get(f"/api/v1/partners/admin/applications/{partner['id']}/customers", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["organization_id"] == org_id
    assert body[0]["attribution_type"] == "partner_sourced"


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


# ── Editing applications/deals + activity log (tracked-change requirement) ──

def test_admin_update_application_requires_superadmin(client, registered_user):
    partner = _apply(client)
    resp = client.patch(
        f"/api/v1/partners/admin/applications/{partner['id']}",
        json={"name": "New Name"}, headers=registered_user["headers"],
    )
    assert resp.status_code == 403


def test_admin_update_application_unknown_partner_404s(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.patch(
        "/api/v1/partners/admin/applications/not-a-real-id",
        json={"name": "New Name"}, headers=registered_user["headers"],
    )
    assert resp.status_code == 404


def test_admin_update_application_edits_fields_and_logs_diff(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    headers = registered_user["headers"]
    partner = _apply(client, name="Addie Parnters")

    patch_resp = client.patch(
        f"/api/v1/partners/admin/applications/{partner['id']}",
        json={"name": "Addie Partners", "contact_email": "fixed@example.com"},
        headers=headers,
    )
    assert patch_resp.status_code == 200, patch_resp.text
    updated = patch_resp.json()
    assert updated["name"] == "Addie Partners"
    assert updated["contact_email"] == "fixed@example.com"
    # Untouched fields survive the PATCH unchanged.
    assert updated["contact_name"] == partner["contact_name"]

    activity_resp = client.get(f"/api/v1/partners/admin/applications/{partner['id']}/activity", headers=headers)
    assert activity_resp.status_code == 200, activity_resp.text
    entries = activity_resp.json()
    edited = next(e for e in entries if e["action"] == "partner.application.edited")
    assert edited["actor_email"] == registered_user["email"]
    assert edited["detail"]["changes"]["name"] == {"old": "Addie Parnters", "new": "Addie Partners"}
    assert edited["detail"]["changes"]["contact_email"]["new"] == "fixed@example.com"


def test_admin_update_application_noop_writes_no_audit_row(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    headers = registered_user["headers"]
    partner = _apply(client)

    # Re-submitting the exact same value is a no-op — nothing actually
    # changed, so no "edited" row should appear.
    resp = client.patch(
        f"/api/v1/partners/admin/applications/{partner['id']}",
        json={"name": partner["name"]}, headers=headers,
    )
    assert resp.status_code == 200

    activity_resp = client.get(f"/api/v1/partners/admin/applications/{partner['id']}/activity", headers=headers)
    assert not any(e["action"] == "partner.application.edited" for e in activity_resp.json())


def test_admin_application_activity_requires_superadmin(client, registered_user):
    partner = _apply(client)
    resp = client.get(f"/api/v1/partners/admin/applications/{partner['id']}/activity", headers=registered_user["headers"])
    assert resp.status_code == 403


def test_admin_application_activity_includes_status_transitions(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    headers = registered_user["headers"]
    partner = _apply(client)
    client.post(f"/api/v1/partners/admin/applications/{partner['id']}/approve", headers=headers)
    client.post(f"/api/v1/partners/admin/applications/{partner['id']}/suspend", json={"reason": "test"}, headers=headers)

    resp = client.get(f"/api/v1/partners/admin/applications/{partner['id']}/activity", headers=headers)
    assert resp.status_code == 200
    actions = [e["action"] for e in resp.json()]
    assert "partner.application.approved" in actions
    assert "partner.suspended" in actions
    # Newest first.
    assert actions.index("partner.suspended") < actions.index("partner.application.approved")


def _register_deal(client, headers, partner_id: str) -> dict:
    resp = client.post(
        "/api/v1/partners/admin/deal-registrations",
        json={"partner_id": partner_id, "organization_name": "Prospect Co", "domain": f"{uuid.uuid4().hex[:8]}.example"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_admin_update_deal_registration_requires_superadmin(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    headers = registered_user["headers"]
    partner = _apply(client)
    client.post(f"/api/v1/partners/admin/applications/{partner['id']}/approve", headers=headers)
    deal = _register_deal(client, headers, partner["id"])

    # registered_user IS superadmin here (needed to set up the deal) — use
    # a fresh non-superadmin caller to actually exercise the 403 path.
    other = _register_and_login(client, "notadmin")
    resp = client.patch(
        f"/api/v1/partners/admin/deal-registrations/{deal['id']}",
        json={"organization_name": "New Name"}, headers=other["headers"],
    )
    assert resp.status_code == 403


def test_admin_update_deal_registration_unknown_deal_404s(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.patch(
        "/api/v1/partners/admin/deal-registrations/not-a-real-id",
        json={"organization_name": "New Name"}, headers=registered_user["headers"],
    )
    assert resp.status_code == 404


def test_admin_update_deal_registration_edits_fields_and_logs_diff(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    headers = registered_user["headers"]
    partner = _apply(client)
    client.post(f"/api/v1/partners/admin/applications/{partner['id']}/approve", headers=headers)
    deal = _register_deal(client, headers, partner["id"])
    client.post(
        f"/api/v1/partners/admin/deal-registrations/{deal['id']}/approve",
        json={"approved_commissionable_value_cents": 500000}, headers=headers,
    )

    # Correctable even after approval — the negotiated Enterprise figure changed.
    patch_resp = client.patch(
        f"/api/v1/partners/admin/deal-registrations/{deal['id']}",
        json={"organization_name": "Corrected Co", "approved_commissionable_value_cents": 750000},
        headers=headers,
    )
    assert patch_resp.status_code == 200, patch_resp.text
    updated = patch_resp.json()
    assert updated["organization_name"] == "Corrected Co"
    assert updated["approved_commissionable_value_cents"] == 750000

    activity_resp = client.get(f"/api/v1/partners/admin/deal-registrations/{deal['id']}/activity", headers=headers)
    assert activity_resp.status_code == 200
    entries = activity_resp.json()
    edited = next(e for e in entries if e["action"] == "partner.deal_registration.edited")
    assert edited["detail"]["changes"]["organization_name"] == {"old": "Prospect Co", "new": "Corrected Co"}
    assert edited["detail"]["changes"]["approved_commissionable_value_cents"] == {"old": 500000, "new": 750000}
    actions = [e["action"] for e in entries]
    assert "partner.deal_registration.created" in actions
    assert "partner.deal_registration.approved" in actions


def test_admin_deal_registration_activity_requires_superadmin(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    headers = registered_user["headers"]
    partner = _apply(client)
    client.post(f"/api/v1/partners/admin/applications/{partner['id']}/approve", headers=headers)
    deal = _register_deal(client, headers, partner["id"])

    other = _register_and_login(client, "notadmin2")
    resp = client.get(f"/api/v1/partners/admin/deal-registrations/{deal['id']}/activity", headers=other["headers"])
    assert resp.status_code == 403


def test_admin_plan_options_requires_superadmin(client, registered_user):
    resp = client.get("/api/v1/partners/admin/plan-options", headers=registered_user["headers"])
    assert resp.status_code == 403


def test_admin_plan_options_returns_real_plan_keys(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.get("/api/v1/partners/admin/plan-options", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    options = resp.json()
    ids = {o["id"] for o in options}
    # Matches payments.py::PLANS exactly — the same values the Square
    # webhook stamps onto an automatic commission entry's plan_id.
    assert "enterprise" in ids
    assert "team" in ids
    assert "organization" in ids
    assert "large" in ids
    enterprise = next(o for o in options if o["id"] == "enterprise")
    assert enterprise["label"] == "Clariva Enterprise"
