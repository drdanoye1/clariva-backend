"""
Partner Center (Engine 28) — Partner Portal tests.

Covers the read-only, partner-facing authenticated dashboard added on top
of the Phase 1 MVP admin console: engine-level invite/accept/overview/
customer-list logic (engines/partner_engine.py's new Partner Portal
methods) and the HTTP-layer invite + portal endpoints in
routers/partners.py (admin "send portal invite" action, the public
invite-claim flow, and the authenticated /partners/portal/* endpoints
gated by require_partner_portal_access).

Same conventions as test_partner_engine.py / test_partners_api.py:
engine-level tests wrap each body in asyncio.run() against a real
AsyncSessionLocal() session; HTTP-layer tests use the `client` fixture.
send_email() always degrades gracefully to a warning + False return on
any failure — whether that's no RESEND_API_KEY configured (the sandbox/CI
case) or a real key rejecting a fake @example.com recipient (a real local
.env) — so portal-invite-creation tests assert on that (email_sent=False)
rather than mocking the network call, and the assertion holds either way.
Tests that need `list_attributed_customers()`'s Organization-name join to
resolve (it inner-joins Organization, unlike the ledger/attribution
queries) use the `_create_org` HTTP helper for a real Organization row,
same convention as test_partner_engine.py — a bare `new_uuid()` org_id
works fine for attribution/ledger-only assertions but silently drops out
of that one join.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from database import AsyncSessionLocal
from engines.partner_engine import (
    PartnerAccountExistsError, PartnerEngine, PartnerNotFoundError, PortalInviteNotFoundError,
)
from models.db_models import Partner, PartnerPortalInvite, User, new_uuid


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def engine():
    return PartnerEngine()


async def _apply_and_approve(db_session, engine: PartnerEngine, actor_id: str) -> Partner:
    partner = await engine.apply(
        db_session, name=f"Firm {uuid.uuid4().hex[:6]}", contact_name="Jane Consultant",
        contact_email=f"jane-{uuid.uuid4().hex[:6]}@example.com", partner_type="referral", notes="Interested",
    )
    return await engine.approve_partner(db_session, partner.id, actor_id)


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
    """Same helper as test_partner_engine.py's — creates a real Organization
    row via the HTTP endpoint (which also captures attribution when
    referral_code is passed, per routers/organizations.py::create_organization)."""
    body = {"name": f"Org {uuid.uuid4().hex[:8]}"}
    if referral_code:
        body["referral_code"] = referral_code
    resp = client.post("/api/v1/organizations/", json=body, headers=owner_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ══════════════════════════════════════════════════════════════════════════
# Engine-level tests
# ══════════════════════════════════════════════════════════════════════════

def test_create_portal_invite_requires_approved_partner(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await engine.apply(db, name="Acme", contact_name="Jane", contact_email="stillpending@example.com")
            with pytest.raises(ValueError):
                await engine.create_portal_invite(db, partner_id=partner.id, invited_by_user_id=new_uuid())
    _run(_body())


def test_create_portal_invite_unknown_partner_raises(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(PartnerNotFoundError):
                await engine.create_portal_invite(db, partner_id="not-a-real-id", invited_by_user_id=new_uuid())
    _run(_body())


def test_create_portal_invite_issues_token_for_new_email(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            await db.commit()
            result = await engine.create_portal_invite(db, partner_id=partner.id, invited_by_user_id=new_uuid())
            assert result["linked_existing_user"] is False
            assert result["invite"] is not None
            assert result["invite"].email == partner.contact_email
            assert result["invite"].status == "pending"
    _run(_body())


def test_create_portal_invite_links_existing_user_directly(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            # An account with this contact_email already exists (e.g. the
            # partner's contact already has an org-member Clariva login).
            existing = User(
                email=partner.contact_email, hashed_password="not-a-real-hash",
                full_name="Jane Consultant", organization="Some Org",
            )
            db.add(existing)
            await db.flush()
            await db.commit()

            result = await engine.create_portal_invite(db, partner_id=partner.id, invited_by_user_id=new_uuid())
            assert result["linked_existing_user"] is True
            assert result["invite"] is None
            assert result["partner"].user_id == existing.id
    _run(_body())


def test_create_portal_invite_rejects_already_linked_partner(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            partner.user_id = new_uuid()
            await db.flush()
            await db.commit()
            with pytest.raises(PartnerAccountExistsError):
                await engine.create_portal_invite(db, partner_id=partner.id, invited_by_user_id=new_uuid())
    _run(_body())


def test_create_portal_invite_revokes_prior_pending_invite(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            await db.commit()
            first = await engine.create_portal_invite(db, partner_id=partner.id, invited_by_user_id=new_uuid())
            await db.commit()
            first_invite_id = first["invite"].id

            second = await engine.create_portal_invite(db, partner_id=partner.id, invited_by_user_id=new_uuid())
            await db.commit()
            assert second["invite"].id != first_invite_id

            refreshed = await db.get(PartnerPortalInvite, first_invite_id)
            assert refreshed.status == "revoked"
    _run(_body())


def test_accept_portal_invite_creates_user_and_links_partner(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            await db.commit()
            result = await engine.create_portal_invite(db, partner_id=partner.id, invited_by_user_id=new_uuid())
            await db.commit()
            token = result["invite"].token

            user = await engine.accept_portal_invite(
                db, token=token, full_name="Jane Consultant", hashed_password="hashed-value",
            )
            await db.commit()
            assert user.email == partner.contact_email
            assert user.organization == f"{partner.name} (Partner)"

            linked = await engine.get_partner_by_user_id(db, user.id)
            assert linked is not None
            assert linked.id == partner.id
    _run(_body())


def test_accept_portal_invite_unknown_token_raises(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(PortalInviteNotFoundError):
                await engine.accept_portal_invite(db, token="not-a-real-token", full_name="X", hashed_password="h")
    _run(_body())


def test_accept_portal_invite_already_accepted_raises_value_error(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            await db.commit()
            result = await engine.create_portal_invite(db, partner_id=partner.id, invited_by_user_id=new_uuid())
            await db.commit()
            token = result["invite"].token
            await engine.accept_portal_invite(db, token=token, full_name="Jane", hashed_password="h1")
            await db.commit()
            with pytest.raises(ValueError):
                await engine.accept_portal_invite(db, token=token, full_name="Jane", hashed_password="h2")
    _run(_body())


def test_get_partner_overview_zero_state(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            await db.commit()
            zero_state = await engine.get_partner_overview(db, partner.id)
            assert zero_state["active_customers"] == 0
            assert zero_state["lifetime_earnings_cents"] == 0
            assert zero_state["available_for_payout_cents"] == 0
            assert zero_state["commission_this_month_cents"] == 0
            assert zero_state["attributed_subscription_revenue_cents"] == 0
    _run(_body())


def test_get_partner_overview_and_customer_list_scoped_to_partner(client, engine):
    # list_attributed_customers() inner-joins Organization to resolve a
    # display name (see its docstring — it must never join into content
    # tables, but organization_id -> Organization.id is a real NOT NULL FK
    # in production, so the join is correct there). A synthetic org_id with
    # no matching Organization row — fine for capture_attribution/ledger
    # tests that never join Organization, per test_partner_engine.py's
    # test_capture_attribution_first_touch_wins — silently drops out of
    # this particular query. Use a real org via _create_org (same helper
    # test_partner_engine.py uses) so the join has something to match.
    async def _create_partners():
        async with AsyncSessionLocal() as db:
            partner_a = await _apply_and_approve(db, engine, new_uuid())
            partner_b = await _apply_and_approve(db, engine, new_uuid())
            await db.commit()
            return partner_a, partner_b
    partner_a, partner_b = _run(_create_partners())

    owner = _register_and_login(client, "overviewowner")
    org_a = _create_org(client, owner["headers"], referral_code=partner_a.referral_code)

    async def _bill_and_check():
        async with AsyncSessionLocal() as db:
            entry = await engine.record_manual_commission(db, organization_id=org_a, qualifying_revenue_cents=990000)
            await engine.update_commission_entry_status(db, entry.id, "available")
            await db.commit()

            overview_a = await engine.get_partner_overview(db, partner_a.id)
            assert overview_a["active_customers"] == 1
            assert overview_a["available_for_payout_cents"] == entry.commission_amount_cents
            assert overview_a["lifetime_earnings_cents"] == entry.commission_amount_cents

            overview_b = await engine.get_partner_overview(db, partner_b.id)
            assert overview_b["active_customers"] == 0
            assert overview_b["lifetime_earnings_cents"] == 0

            customers_a = await engine.list_attributed_customers(db, partner_a.id)
            assert len(customers_a) == 1
            assert customers_a[0]["organization_id"] == org_a
            assert set(customers_a[0].keys()) == {"organization_id", "organization_name", "attribution_type", "attributed_at"}

            customers_b = await engine.list_attributed_customers(db, partner_b.id)
            assert customers_b == []
    _run(_bill_and_check())


# ══════════════════════════════════════════════════════════════════════════
# HTTP-layer tests
# ══════════════════════════════════════════════════════════════════════════

def test_admin_send_portal_invite_requires_superadmin(client, registered_user):
    partner = _apply(client)
    resp = client.post(
        f"/api/v1/partners/admin/applications/{partner['id']}/portal-invite",
        headers=registered_user["headers"],
    )
    assert resp.status_code == 403


def test_admin_send_portal_invite_requires_approved_partner(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    partner = _apply(client)  # still pending
    resp = client.post(
        f"/api/v1/partners/admin/applications/{partner['id']}/portal-invite",
        headers=registered_user["headers"],
    )
    assert resp.status_code == 422


def test_admin_send_portal_invite_creates_invite_and_reports_no_email_sent(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    headers = registered_user["headers"]
    partner = _apply(client)
    client.post(f"/api/v1/partners/admin/applications/{partner['id']}/approve", headers=headers)

    resp = client.post(f"/api/v1/partners/admin/applications/{partner['id']}/portal-invite", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["linked_existing_user"] is False
    # send_email() always degrades to False here rather than raising: either
    # there's no RESEND_API_KEY configured (sandbox/CI), or there is one but
    # Resend rejects a fake @example.com recipient (a real local .env) — both
    # are caught by the same try/except in email_service.py, so this
    # assertion holds regardless of which environment runs it.
    assert body["email_sent"] is False


def test_admin_send_portal_invite_unknown_partner_404s(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.post(
        "/api/v1/partners/admin/applications/not-a-real-id/portal-invite",
        headers=registered_user["headers"],
    )
    assert resp.status_code == 404


def _approve_partner_and_get_invite_token(client, headers, contact_email: str = None) -> tuple[dict, str | None]:
    """Returns the *approved* partner (so `referral_code` is populated —
    apply() alone never sets it) and the invite token, or `None` for the
    token when create_portal_invite() linked an existing User directly
    (see the docstring on test_portal_invite_accept_links_directly_when_
    user_already_exists — that path never creates a PartnerPortalInvite row)."""
    partner = _apply(client, **({"contact_email": contact_email} if contact_email else {}))
    approve_resp = client.post(f"/api/v1/partners/admin/applications/{partner['id']}/approve", headers=headers)
    assert approve_resp.status_code == 200, approve_resp.text
    approved = approve_resp.json()

    invite_resp = client.post(f"/api/v1/partners/admin/applications/{partner['id']}/portal-invite", headers=headers)
    assert invite_resp.status_code == 200, invite_resp.text
    if invite_resp.json()["linked_existing_user"]:
        return approved, None

    async def _body():
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(PartnerPortalInvite).where(PartnerPortalInvite.partner_id == partner["id"])
            )
            invite = result.scalars().first()
            return invite.token
    token = _run(_body())
    return approved, token


def test_portal_invite_preview_returns_partner_name_and_validity(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    partner, token = _approve_partner_and_get_invite_token(client, registered_user["headers"])

    resp = client.get(f"/api/v1/partners/portal/invite/{token}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["partner_name"] == partner["name"]
    assert body["valid"] is True
    assert body["reason"] is None


def test_portal_invite_preview_unknown_token_404s(client):
    resp = client.get("/api/v1/partners/portal/invite/not-a-real-token")
    assert resp.status_code == 404


def test_portal_invite_accept_rejects_short_password(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    _partner, token = _approve_partner_and_get_invite_token(client, registered_user["headers"])
    resp = client.post(
        f"/api/v1/partners/portal/invite/{token}/accept",
        json={"full_name": "Jane Consultant", "password": "short"},
    )
    assert resp.status_code == 400


def test_portal_invite_accept_issues_tokens_and_grants_portal_access(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    partner, token = _approve_partner_and_get_invite_token(client, registered_user["headers"])

    accept_resp = client.post(
        f"/api/v1/partners/portal/invite/{token}/accept",
        json={"full_name": "Jane Consultant", "password": "PortalPassword123!"},
    )
    assert accept_resp.status_code == 200, accept_resp.text
    portal_headers = {"Authorization": f"Bearer {accept_resp.json()['access_token']}"}

    me_resp = client.get("/api/v1/partners/portal/me", headers=portal_headers)
    assert me_resp.status_code == 200, me_resp.text
    assert me_resp.json()["id"] == partner["id"]

    overview_resp = client.get("/api/v1/partners/portal/overview", headers=portal_headers)
    assert overview_resp.status_code == 200, overview_resp.text
    for key in (
        "active_customers", "attributed_subscription_revenue_cents", "commission_this_month_cents",
        "available_for_payout_cents", "lifetime_earnings_cents",
    ):
        assert key in overview_resp.json()

    customers_resp = client.get("/api/v1/partners/portal/customers", headers=portal_headers)
    assert customers_resp.status_code == 200
    assert customers_resp.json() == []

    entries_resp = client.get("/api/v1/partners/portal/commission-entries", headers=portal_headers)
    assert entries_resp.status_code == 200
    assert entries_resp.json() == []

    payouts_resp = client.get("/api/v1/partners/portal/payouts", headers=portal_headers)
    assert payouts_resp.status_code == 200
    assert payouts_resp.json() == []


def test_portal_invite_accept_links_directly_when_user_already_exists(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    existing = _register_and_login(client, "existingportaluser")
    partner, _token_unused = _approve_partner_and_get_invite_token(client, registered_user["headers"], contact_email=existing["email"])

    # create_portal_invite should have linked the existing user directly —
    # no PartnerPortalInvite row to accept.
    me_resp = client.get("/api/v1/partners/portal/me", headers=existing["headers"])
    assert me_resp.status_code == 200, me_resp.text
    assert me_resp.json()["id"] == partner["id"]


def test_portal_endpoints_require_linked_partner_account(client, registered_user):
    resp = client.get("/api/v1/partners/portal/me", headers=registered_user["headers"])
    assert resp.status_code == 403


def test_portal_endpoints_require_auth(client):
    resp = client.get("/api/v1/partners/portal/me")
    assert resp.status_code == 401


def test_portal_commission_entries_scoped_to_calling_partner_only(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    headers = registered_user["headers"]

    partner_a, token_a = _approve_partner_and_get_invite_token(client, headers)
    accept_a = client.post(
        f"/api/v1/partners/portal/invite/{token_a}/accept",
        json={"full_name": "Partner A Contact", "password": "PortalPassword123!"},
    )
    assert accept_a.status_code == 200, accept_a.text
    headers_a = {"Authorization": f"Bearer {accept_a.json()['access_token']}"}

    partner_b, token_b = _approve_partner_and_get_invite_token(client, headers)
    accept_b = client.post(
        f"/api/v1/partners/portal/invite/{token_b}/accept",
        json={"full_name": "Partner B Contact", "password": "PortalPassword123!"},
    )
    assert accept_b.status_code == 200, accept_b.text
    headers_b = {"Authorization": f"Bearer {accept_b.json()['access_token']}"}

    # Give partner A one qualifying customer + a manual commission entry.
    # Real Organization row (not a fabricated uuid) — /portal/customers
    # inner-joins Organization to resolve a display name, so a synthetic
    # org_id with no matching row would silently vanish from that query
    # even though the attribution/ledger rows themselves exist. See
    # test_get_partner_overview_and_customer_list_scoped_to_partner's
    # matching comment above.
    customer_owner = _register_and_login(client, "scopedcustomerowner")
    org_a = _create_org(client, customer_owner["headers"], referral_code=partner_a["referral_code"])

    async def _bill():
        async with AsyncSessionLocal() as db:
            eng = PartnerEngine()
            entry = await eng.record_manual_commission(db, organization_id=org_a, qualifying_revenue_cents=500000)
            await db.commit()
            return entry.id
    entry_id = _run(_bill())

    entries_a = client.get("/api/v1/partners/portal/commission-entries", headers=headers_a)
    assert entries_a.status_code == 200
    assert any(e["id"] == entry_id for e in entries_a.json())

    entries_b = client.get("/api/v1/partners/portal/commission-entries", headers=headers_b)
    assert entries_b.status_code == 200
    assert entries_b.json() == []

    customers_a = client.get("/api/v1/partners/portal/customers", headers=headers_a)
    assert len(customers_a.json()) == 1
    assert customers_a.json()[0]["organization_id"] == org_a

    customers_b = client.get("/api/v1/partners/portal/customers", headers=headers_b)
    assert customers_b.json() == []
