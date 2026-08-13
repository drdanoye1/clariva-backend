"""
Partner Center (Engine 28) — engine-level tests.

Covers engines/partner_engine.py directly: applications, deal
registrations (incl. duplicate-domain detection), attribution capture
(incl. first-touch-wins and org-creation integration via
routers/organizations.py::create_organization's referral_code field),
the commission rule's seed-and-version behavior, cohort-based commission
math (the 20%/15%/10%/0% schedule), reversal/adjustment entries, and
payouts.

Same convention as test_service_catalog.py: PartnerEngine methods are
async and need a real DB session, so each test wraps its body in
asyncio.run() rather than pulling in pytest-asyncio for just this file.
The `client` fixture is included (unused directly) in engine-only tests
purely to trigger the app's lifespan startup, which is what creates this
phase's new tables via Base.metadata.create_all().
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from database import AsyncSessionLocal
from engines.partner_engine import (
    AttributionNotFoundError, CommissionEntryNotFoundError, DealRegistrationNotFoundError,
    DuplicateClaimError, PartnerEngine, PartnerNotFoundError,
)
from models.db_models import CommissionEntry, CustomerAttribution, Partner, new_uuid


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def engine():
    return PartnerEngine()


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


async def _apply_and_approve(db_session, engine: PartnerEngine, actor_id: str) -> Partner:
    partner = await engine.apply(
        db_session, name=f"Firm {uuid.uuid4().hex[:6]}", contact_name="Jane Consultant",
        contact_email=f"jane-{uuid.uuid4().hex[:6]}@example.com", partner_type="referral", notes="Interested",
    )
    return await engine.approve_partner(db_session, partner.id, actor_id)


# ── Applications ─────────────────────────────────────────────────────────────

def test_apply_creates_pending_partner_with_no_referral_code(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await engine.apply(
                db, name="Acme Consulting", contact_name="Jane Doe", contact_email="jane@acme.example",
                partner_type="referral", notes="Would like to refer clients.",
            )
            assert partner.status == "pending"
            assert partner.referral_code is None
            assert partner.program_status == "registered"
    _run(_body())


def test_approve_partner_generates_referral_code_and_sets_status(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await engine.apply(db, name="Acme", contact_name="Jane", contact_email="jane2@acme.example")
            approved = await engine.approve_partner(db, partner.id, actor_id=new_uuid())
            assert approved.status == "approved"
            assert approved.referral_code is not None
            assert len(approved.referral_code) <= 8
            assert approved.reviewed_at is not None
    _run(_body())


def test_reject_partner_sets_status_and_reason(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await engine.apply(db, name="Acme", contact_name="Jane", contact_email="jane3@acme.example")
            rejected = await engine.reject_partner(db, partner.id, actor_id=new_uuid(), reason="Not a fit")
            assert rejected.status == "rejected"
            assert rejected.rejection_reason == "Not a fit"
            assert rejected.referral_code is None
    _run(_body())


def test_suspend_partner(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            suspended = await engine.suspend_partner(db, partner.id, actor_id=new_uuid(), reason="Policy violation")
            assert suspended.status == "suspended"
    _run(_body())


def test_get_partner_raises_for_unknown_id(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(PartnerNotFoundError):
                await engine.get_partner(db, "not-a-real-id")
    _run(_body())


def test_set_program_status_rejects_invalid_value(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            with pytest.raises(ValueError):
                await engine.set_program_status(db, partner.id, "diamond")
    _run(_body())


def test_set_program_status_accepts_valid_value(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            updated = await engine.set_program_status(db, partner.id, "gold")
            assert updated.program_status == "gold"
    _run(_body())


# ── Deal registrations ───────────────────────────────────────────────────────

def test_register_deal_requires_approved_partner(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await engine.apply(db, name="Acme", contact_name="Jane", contact_email="jane4@acme.example")
            with pytest.raises(ValueError):
                await engine.register_deal(db, partner_id=partner.id, organization_name="Prospect Co")
    _run(_body())


def test_register_deal_duplicate_domain_across_partners_raises(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner_a = await _apply_and_approve(db, engine, new_uuid())
            partner_b = await _apply_and_approve(db, engine, new_uuid())
            await engine.register_deal(db, partner_id=partner_a.id, organization_name="Prospect Co", domain="prospect.example")
            with pytest.raises(DuplicateClaimError):
                await engine.register_deal(db, partner_id=partner_b.id, organization_name="Prospect Co Inc", domain="PROSPECT.example")
    _run(_body())


def test_register_deal_same_domain_same_partner_allowed(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            await engine.register_deal(db, partner_id=partner.id, organization_name="Prospect Co", domain="prospect2.example")
            # Same partner re-registering the same domain doesn't conflict with itself.
            deal2 = await engine.register_deal(db, partner_id=partner.id, organization_name="Prospect Co (follow-up)", domain="prospect2.example")
            assert deal2.status == "pending_review"
    _run(_body())


def test_approve_deal_sets_protection_window(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            deal = await engine.register_deal(db, partner_id=partner.id, organization_name="Prospect Co")
            approved = await engine.approve_deal(db, deal.id, actor_id=new_uuid())
            assert approved.status == "approved"
            assert approved.protection_expires_at is not None
            # SQLite returns naive datetimes on read-back (even though the
            # engine writes an aware one) — same recurring naive/aware
            # pattern documented on partner_engine.py's `_naive()` helper.
            # Compare like-for-like rather than assuming tzinfo survives
            # the round-trip.
            expires = approved.protection_expires_at
            expires = expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)
            assert expires > datetime.now(timezone.utc)
    _run(_body())


def test_approve_deal_can_set_enterprise_commissionable_value(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            deal = await engine.register_deal(db, partner_id=partner.id, organization_name="Big Co", proposed_plan="enterprise")
            approved = await engine.approve_deal(db, deal.id, actor_id=new_uuid(), approved_commissionable_value_cents=1_500_000)
            assert approved.approved_commissionable_value_cents == 1_500_000
    _run(_body())


def test_reject_deal_sets_status_and_reason(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            deal = await engine.register_deal(db, partner_id=partner.id, organization_name="Prospect Co")
            rejected = await engine.reject_deal(db, deal.id, actor_id=new_uuid(), reason="Duplicate")
            assert rejected.status == "rejected"
            assert rejected.rejection_reason == "Duplicate"
    _run(_body())


def test_get_deal_raises_for_unknown_id(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(DealRegistrationNotFoundError):
                await engine.get_deal(db, "not-a-real-id")
    _run(_body())


# ── Attribution ──────────────────────────────────────────────────────────────

def test_capture_attribution_unknown_referral_code_returns_none(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            attribution = await engine.capture_attribution(db, organization_id=new_uuid(), referral_code="NOPE1234")
            assert attribution is None
    _run(_body())


def test_capture_attribution_unapproved_partner_returns_none(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            # A pending (never approved) partner has no referral_code at all,
            # so any code passed here can't match — covers the "unapproved"
            # path implicitly.
            await engine.apply(db, name="Acme", contact_name="Jane", contact_email="jane5@acme.example")
            attribution = await engine.capture_attribution(db, organization_id=new_uuid(), referral_code="ANYCODE1")
            assert attribution is None
    _run(_body())


def test_capture_attribution_first_touch_wins(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner_a = await _apply_and_approve(db, engine, new_uuid())
            partner_b = await _apply_and_approve(db, engine, new_uuid())
            org_id = new_uuid()

            first = await engine.capture_attribution(db, organization_id=org_id, referral_code=partner_a.referral_code)
            assert first is not None
            assert first.partner_id == partner_a.id

            second = await engine.capture_attribution(db, organization_id=org_id, referral_code=partner_b.referral_code)
            assert second is None  # already attributed — first touch wins

            current = await engine.get_attribution_for_org(db, org_id)
            assert current.partner_id == partner_a.id
    _run(_body())


def test_create_organization_with_referral_code_captures_attribution(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            # This session's writes must be durable before the HTTP call
            # below opens its own independent session (via get_db(), which
            # commits on success) — engine methods only flush() by design
            # (same as every other engine in this codebase; the caller
            # commits), so unlike same-session tests elsewhere in this
            # file that read their own writes back before the session
            # closes, this test needs an explicit commit or the partner
            # row never survives past this block.
            await db.commit()
            return partner
    partner = _run(_body())

    owner = _register_and_login(client, "orgowner")
    org_id = _create_org(client, owner["headers"], referral_code=partner.referral_code)

    async def _check():
        async with AsyncSessionLocal() as db:
            return await engine.get_attribution_for_org(db, org_id)
    attribution = _run(_check())
    assert attribution is not None
    assert attribution.partner_id == partner.id
    assert attribution.attribution_type == "partner_sourced"


def test_create_organization_with_invalid_referral_code_still_succeeds(client):
    owner = _register_and_login(client, "orgowner2")
    # Org creation must never fail just because the referral code was junk.
    org_id = _create_org(client, owner["headers"], referral_code="TOTALLY-BOGUS")
    assert org_id


# ── Commission rule ──────────────────────────────────────────────────────────

def test_get_active_commission_rule_seeds_default_schedule(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            rule = await engine.get_active_commission_rule(db)
            assert rule.months_1_12_rate == 0.20
            assert rule.months_13_24_rate == 0.15
            assert rule.months_25_36_rate == 0.10
            assert rule.month_37_plus_rate == 0.0
            assert rule.is_active is True
    _run(_body())


def test_update_commission_rule_deactivates_old_and_creates_new(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            original = await engine.get_active_commission_rule(db)
            updated = await engine.update_commission_rule(
                db, months_1_12_rate=0.25, months_13_24_rate=0.18,
                months_25_36_rate=0.12, month_37_plus_rate=0.02, actor_id=new_uuid(),
            )
            assert updated.id != original.id
            assert updated.months_1_12_rate == 0.25
            await db.refresh(original)
            assert original.is_active is False

            current = await engine.get_active_commission_rule(db)
            assert current.id == updated.id
    _run(_body())


# ── Commission entries / cohort math ─────────────────────────────────────────

async def _attributed_org(db, engine: PartnerEngine, *, days_ago: int) -> tuple:
    partner = await _apply_and_approve(db, engine, new_uuid())
    org_id = new_uuid()
    attribution = CustomerAttribution(organization_id=org_id, partner_id=partner.id, attribution_type="partner_sourced")
    db.add(attribution)
    await db.flush()
    attribution.attributed_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    await db.flush()
    await db.refresh(attribution)
    return partner, org_id, attribution


def test_record_commission_returns_none_without_attribution(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            entry = await engine.record_commission(
                db, organization_id=new_uuid(), plan_id="professional",
                qualifying_revenue_cents=9900, payment_reference="order-1",
            )
            assert entry is None
    _run(_body())


def test_record_commission_month_1_12_bucket_applies_20_percent(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner, org_id, _ = await _attributed_org(db, engine, days_ago=5)
            entry = await engine.record_commission(
                db, organization_id=org_id, plan_id="professional",
                qualifying_revenue_cents=9900, payment_reference="order-a",
            )
            assert entry is not None
            assert entry.months_since_attribution == 1
            assert entry.commission_rate == 0.20
            assert entry.commission_amount_cents == round(9900 * 0.20)
            assert entry.status == "pending"
            assert entry.partner_id == partner.id
    _run(_body())


def test_record_commission_month_13_24_bucket_applies_15_percent(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            _, org_id, _ = await _attributed_org(db, engine, days_ago=400)
            entry = await engine.record_commission(
                db, organization_id=org_id, plan_id="professional_annual",
                qualifying_revenue_cents=39000, payment_reference="order-b",
            )
            assert entry is not None
            assert 13 <= entry.months_since_attribution <= 24
            assert entry.commission_rate == 0.15
    _run(_body())


def test_record_commission_month_25_36_bucket_applies_10_percent(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            _, org_id, _ = await _attributed_org(db, engine, days_ago=800)
            entry = await engine.record_commission(
                db, organization_id=org_id, plan_id="organization",
                qualifying_revenue_cents=24900, payment_reference="order-c",
            )
            assert entry is not None
            assert 25 <= entry.months_since_attribution <= 36
            assert entry.commission_rate == 0.10
    _run(_body())


def test_record_commission_month_37_plus_creates_no_entry(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            _, org_id, _ = await _attributed_org(db, engine, days_ago=1200)
            entry = await engine.record_commission(
                db, organization_id=org_id, plan_id="organization",
                qualifying_revenue_cents=24900, payment_reference="order-d",
            )
            assert entry is None  # 0% standard rate — no ledger noise
    _run(_body())


def test_record_manual_commission_raises_without_attribution(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(AttributionNotFoundError):
                await engine.record_manual_commission(db, organization_id=new_uuid(), qualifying_revenue_cents=1_500_000)
    _run(_body())


def test_record_manual_commission_raises_when_cohort_rate_is_zero(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            _, org_id, _ = await _attributed_org(db, engine, days_ago=1200)
            with pytest.raises(ValueError):
                await engine.record_manual_commission(db, organization_id=org_id, qualifying_revenue_cents=1_500_000)
    _run(_body())


def test_record_manual_commission_creates_entry_for_enterprise_deal(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            _, org_id, _ = await _attributed_org(db, engine, days_ago=5)
            entry = await engine.record_manual_commission(
                db, organization_id=org_id, qualifying_revenue_cents=1_500_000,
                payment_reference="invoice-001", plan_id="enterprise",
            )
            assert entry.commission_amount_cents == round(1_500_000 * 0.20)
            assert entry.plan_id == "enterprise"
    _run(_body())


def test_update_commission_entry_status_pending_to_approved(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            _, org_id, _ = await _attributed_org(db, engine, days_ago=5)
            entry = await engine.record_commission(db, organization_id=org_id, plan_id="professional", qualifying_revenue_cents=9900)
            updated = await engine.update_commission_entry_status(db, entry.id, "approved")
            assert updated.status == "approved"
    _run(_body())


def test_update_commission_entry_status_rejects_invalid_status(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            _, org_id, _ = await _attributed_org(db, engine, days_ago=5)
            entry = await engine.record_commission(db, organization_id=org_id, plan_id="professional", qualifying_revenue_cents=9900)
            with pytest.raises(ValueError):
                await engine.update_commission_entry_status(db, entry.id, "bogus_status")
    _run(_body())


def test_reversing_entry_creates_negated_adjustment(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            _, org_id, _ = await _attributed_org(db, engine, days_ago=5)
            entry = await engine.record_commission(db, organization_id=org_id, plan_id="professional", qualifying_revenue_cents=9900)
            original_amount = entry.commission_amount_cents

            reversed_entry = await engine.update_commission_entry_status(db, entry.id, "reversed")
            assert reversed_entry.status == "reversed"

            result = await db.execute(
                select(CommissionEntry).where(CommissionEntry.adjusts_entry_id == entry.id)
            )
            adjustment = result.scalar_one()
            assert adjustment.is_adjustment is True
            assert adjustment.commission_amount_cents == -original_amount
            assert adjustment.status == "reversed"
    _run(_body())


def test_cannot_mutate_an_adjustment_entry(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            _, org_id, _ = await _attributed_org(db, engine, days_ago=5)
            entry = await engine.record_commission(db, organization_id=org_id, plan_id="professional", qualifying_revenue_cents=9900)
            await engine.update_commission_entry_status(db, entry.id, "reversed")
            result = await db.execute(select(CommissionEntry).where(CommissionEntry.adjusts_entry_id == entry.id))
            adjustment = result.scalar_one()
            with pytest.raises(ValueError):
                await engine.update_commission_entry_status(db, adjustment.id, "paid")
    _run(_body())


def test_get_commission_entry_raises_for_unknown_id(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(CommissionEntryNotFoundError):
                await engine.get_commission_entry(db, "not-a-real-id")
    _run(_body())


# ── Payouts ──────────────────────────────────────────────────────────────────

def test_create_payout_marks_entries_paid_and_sums_amount(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner, org_id, _ = await _attributed_org(db, engine, days_ago=5)
            e1 = await engine.record_commission(db, organization_id=org_id, plan_id="professional", qualifying_revenue_cents=9900, payment_reference="o1")
            e2 = await engine.record_commission(db, organization_id=org_id, plan_id="professional", qualifying_revenue_cents=9900, payment_reference="o2")
            await engine.update_commission_entry_status(db, e1.id, "approved")
            await engine.update_commission_entry_status(db, e1.id, "available")
            await engine.update_commission_entry_status(db, e2.id, "approved")
            await engine.update_commission_entry_status(db, e2.id, "available")

            payout = await engine.create_payout(db, partner_id=partner.id, commission_entry_ids=[e1.id, e2.id], actor_id=new_uuid())
            assert payout.amount_cents == e1.commission_amount_cents + e2.commission_amount_cents
            assert payout.status == "paid"

            await db.refresh(e1)
            await db.refresh(e2)
            assert e1.status == "paid"
            assert e2.status == "paid"
    _run(_body())


def test_create_payout_rejects_entry_not_available(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner, org_id, _ = await _attributed_org(db, engine, days_ago=5)
            entry = await engine.record_commission(db, organization_id=org_id, plan_id="professional", qualifying_revenue_cents=9900)
            with pytest.raises(ValueError):
                await engine.create_payout(db, partner_id=partner.id, commission_entry_ids=[entry.id], actor_id=new_uuid())
    _run(_body())


def test_create_payout_rejects_entry_from_different_partner(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            _, org_id, _ = await _attributed_org(db, engine, days_ago=5)
            other_partner = await _apply_and_approve(db, engine, new_uuid())
            entry = await engine.record_commission(db, organization_id=org_id, plan_id="professional", qualifying_revenue_cents=9900)
            await engine.update_commission_entry_status(db, entry.id, "approved")
            await engine.update_commission_entry_status(db, entry.id, "available")
            with pytest.raises(ValueError):
                await engine.create_payout(db, partner_id=other_partner.id, commission_entry_ids=[entry.id], actor_id=new_uuid())
    _run(_body())


def test_create_payout_rejects_empty_entry_list(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner = await _apply_and_approve(db, engine, new_uuid())
            with pytest.raises(ValueError):
                await engine.create_payout(db, partner_id=partner.id, commission_entry_ids=[], actor_id=new_uuid())
    _run(_body())


# ── Channel overview ─────────────────────────────────────────────────────────

def test_get_channel_overview_reflects_partner_and_commission_counts(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            partner, org_id, _ = await _attributed_org(db, engine, days_ago=5)
            await engine.record_commission(db, organization_id=org_id, plan_id="professional", qualifying_revenue_cents=9900)
            overview = await engine.get_channel_overview(db)
            assert overview["active_partners"] >= 1
            assert overview["attributed_customers"] >= 1
            assert overview["outstanding_commission_liability_cents"] >= round(9900 * 0.20)
    _run(_body())
