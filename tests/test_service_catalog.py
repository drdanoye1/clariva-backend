"""
Phase 2 — On-Demand AI Services Marketplace & Org Funding Controls
(Enterprise Pricing spec §9.2). Covers engines/service_catalog_engine.py
directly (seeding, quoting, entitlement consumption, paid fallback) and
the HTTP surface in routers/service_catalog.py (permission gating,
price-before-generation quote, 402 on insufficient balance).

Same convention as test_credit_engine.py: ServiceCatalogEngine methods are
async and need a real DB session, so each engine-level test wraps its body
in asyncio.run() rather than pulling in pytest-asyncio for just this file.
The `client` fixture is included (unused directly) in every engine-level
test purely to trigger the app's lifespan startup, which is what creates
the new Phase 2 tables (and seeds the catalog) in the test database — see
conftest.py and main.py's _seed_service_catalog().

Org plan is set directly via the ORM (there's no "upgrade plan" endpoint
yet — Organization.plan is set by a future billing integration) so tests
can exercise each plan's complimentary allowance without needing Square
checkout in the loop, same "insert what a future flow will eventually
create" convention test_funding_intelligence_api.py uses for FOARecord.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from database import AsyncSessionLocal
from engines.credit_engine import CreditEngine, InsufficientCreditsError
from engines.service_catalog_engine import (
    COMPLIMENTARY_ALLOWANCE_SEED, SERVICE_CATALOG_SEED, ServiceCatalogEngine,
    ServiceNotFoundError,
)
from models.db_models import (
    ComplimentaryAllowance, FOARecord, Organization, OrgServiceEntitlement,
    ServiceCatalogItem, User, new_uuid,
)


def _run(coro):
    return asyncio.run(coro)


def _org_id() -> str:
    return f"test-org-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def catalog_engine():
    return ServiceCatalogEngine()


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


def _set_org_plan(org_id: str, plan: str) -> None:
    async def _body():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Organization).where(Organization.id == org_id))
            org = result.scalar_one()
            org.plan = plan
            await db.commit()
    _run(_body())


# ── Engine: seeding ──────────────────────────────────────────────────────────

def test_ensure_seeded_inserts_every_catalog_item_and_allowance(client, catalog_engine):
    # The app's own startup lifespan already seeded once (see
    # main.py::_seed_service_catalog) — this asserts that seed landed.
    async def _body():
        async with AsyncSessionLocal() as db:
            items = await db.execute(select(ServiceCatalogItem.service_key))
            keys = {row[0] for row in items.all()}
            assert {s["service_key"] for s in SERVICE_CATALOG_SEED} <= keys

            allowances = await db.execute(select(ComplimentaryAllowance.plan, ComplimentaryAllowance.service_key))
            pairs = {(row[0], row[1]) for row in allowances.all()}
            assert {(p, k) for p, k, _q, _d in COMPLIMENTARY_ALLOWANCE_SEED} <= pairs
    _run(_body())


def test_ensure_seeded_is_idempotent(client, catalog_engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            await catalog_engine.ensure_seeded(db)  # second call must not duplicate or error
            await db.commit()
            result = await db.execute(
                select(ServiceCatalogItem).where(ServiceCatalogItem.service_key == "grant_opportunity_analysis")
            )
            assert len(result.scalars().all()) == 1
    _run(_body())


def test_get_service_raises_for_unknown_key(client, catalog_engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(ServiceNotFoundError):
                await catalog_engine.get_service(db, "not_a_real_service")
    _run(_body())


# ── Engine: quoting + entitlements ───────────────────────────────────────────

def test_quote_uses_complimentary_when_org_has_entitlement(client, catalog_engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            db.add(Organization(id=org_id, name="Test Org", created_by=new_uuid(), plan="professional"))
            await db.commit()
        async with AsyncSessionLocal() as db:
            quote = await catalog_engine.quote(db, org_id, "grant_opportunity_analysis")
            await db.commit()
            return quote

    quote = _run(_body())
    assert quote["funding_source"] == "complimentary"
    assert quote["price_cents"] == 0
    assert quote["list_price_cents"] == 1000


def test_quote_falls_back_to_paid_balance_when_no_entitlement(client, catalog_engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            # Free plan = no complimentary allowance at all.
            db.add(Organization(id=org_id, name="Test Org", created_by=new_uuid(), plan="free"))
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await catalog_engine.quote(db, org_id, "grant_opportunity_analysis")

    quote = _run(_body())
    assert quote["funding_source"] == "ai_services_balance"
    # Free/PAYG price for grant_opportunity_analysis == subscriber price (no premium defined).
    assert quote["price_cents"] == 1000
    # New org gets a lazily-created ledger with the default starting balance.
    assert quote["ai_services_balance_cents"] == 10000  # $100.00 default balance


def test_consume_spends_complimentary_allowance_then_falls_back_to_paid(client, catalog_engine):
    org_id = _org_id()

    async def _setup():
        async with AsyncSessionLocal() as db:
            # Professional plan gets 2 complimentary Grant Opportunity Analyses.
            db.add(Organization(id=org_id, name="Test Org", created_by=new_uuid(), plan="professional"))
            await db.commit()
    _run(_setup())

    async def _consume_once():
        async with AsyncSessionLocal() as db:
            t = await catalog_engine.consume(db, org_id, None, "grant_opportunity_analysis")
            await db.commit()
            return t.funding_source, t.price_cents

    assert _run(_consume_once()) == ("complimentary", 0)
    assert _run(_consume_once()) == ("complimentary", 0)
    # Third call exceeds the 2-unit Professional allowance — falls to paid balance.
    assert _run(_consume_once()) == ("ai_services_balance", 1000)

    async def _check_entitlement():
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(OrgServiceEntitlement).where(
                    OrgServiceEntitlement.org_id == org_id,
                    OrgServiceEntitlement.service_key == "grant_opportunity_analysis",
                )
            )
            entitlement = result.scalar_one()
            return entitlement.used_quantity, entitlement.granted_quantity

    assert _run(_check_entitlement()) == (2, 2)


def test_consume_raises_insufficient_credits_when_balance_exhausted(client, catalog_engine):
    org_id = _org_id()

    async def _setup():
        async with AsyncSessionLocal() as db:
            db.add(Organization(id=org_id, name="Test Org", created_by=new_uuid(), plan="free"))
            await db.commit()
        # Drain the default $100 starting balance below the $10.00 PAYG
        # analysis price, then confirm the next consume() is rejected
        # outright rather than partially applied.
        async with AsyncSessionLocal() as db:
            credit_engine = CreditEngine()
            ledger = await credit_engine.get_or_create_ledger(db, org_id)
            ledger.balance = 5.0
            await db.commit()
    _run(_setup())

    async def _body():
        async with AsyncSessionLocal() as db:
            await catalog_engine.consume(db, org_id, None, "grant_opportunity_analysis")

    with pytest.raises(InsufficientCreditsError):
        _run(_body())


# ── Router: permission gating + 402 ─────────────────────────────────────────

def test_any_member_can_list_catalog_and_get_quote(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "team")

    catalog_resp = client.get("/api/v1/service-catalog/", headers=registered_user["headers"])
    assert catalog_resp.status_code == 200, catalog_resp.text
    assert any(item["service_key"] == "grant_opportunity_analysis" for item in catalog_resp.json())

    quote_resp = client.get(
        f"/api/v1/service-catalog/{org_id}/quote/grant_opportunity_analysis",
        headers=registered_user["headers"],
    )
    assert quote_resp.status_code == 200, quote_resp.text
    assert quote_resp.json()["funding_source"] == "complimentary"


def test_quote_unknown_service_returns_404(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.get(
        f"/api/v1/service-catalog/{org_id}/quote/not_a_real_service",
        headers=registered_user["headers"],
    )
    assert resp.status_code == 404


def test_non_member_cannot_quote(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    stranger = _register_and_login(client, "stranger")
    resp = client.get(
        f"/api/v1/service-catalog/{org_id}/quote/grant_opportunity_analysis",
        headers=stranger["headers"],
    )
    assert resp.status_code == 403


def test_owner_can_consume_and_records_transaction(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "organization")

    resp = client.post(
        f"/api/v1/service-catalog/{org_id}/consume",
        json={"service_key": "grant_opportunity_analysis", "reference": {"foa_id": "abc123"}},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["funding_source"] == "complimentary"
    assert body["price_cents"] == 0
    assert body["reference"] == {"foa_id": "abc123"}


def test_consume_402_when_no_entitlement_and_insufficient_balance(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "free")

    async def _drain():
        async with AsyncSessionLocal() as db:
            credit_engine = CreditEngine()
            ledger = await credit_engine.get_or_create_ledger(db, org_id)
            ledger.balance = 0.0
            await db.commit()
    _run(_drain())

    resp = client.post(
        f"/api/v1/service-catalog/{org_id}/consume",
        json={"service_key": "grant_opportunity_analysis"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 402


def test_summary_requires_manage_service_funding_permission(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.get(f"/api/v1/service-catalog/{org_id}/summary", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert "ai_services_balance_cents" in resp.json()


def test_entitlements_visible_to_any_member(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "team")
    resp = client.get(f"/api/v1/service-catalog/{org_id}/entitlements", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert any(e["service_key"] == "grant_opportunity_analysis" for e in resp.json())


# ── FOA summarize wiring (routers/foa.py) ───────────────────────────────────
# Confirms the marketplace charge that was wired into the existing
# summarize endpoint (Version 3.0 "Grant Opportunity Analysis" feature)
# behaves per the opt-in-per-org_id precedent CreditEngine already
# established: a personal FOA never charges anything; an org-shared one
# consumes the org's complimentary allowance / paid balance exactly like
# calling POST /service-catalog/{org_id}/consume directly would.

def _insert_foa(**overrides) -> str:
    async def _body():
        defaults = dict(
            id=new_uuid(), agency="NSF", program_title="Test Opportunity", phase="phase_i",
            grant_type="sbir",
        )
        defaults.update(overrides)
        async with AsyncSessionLocal() as db:
            db.add(FOARecord(**defaults))
            await db.commit()
        return defaults["id"]
    return _run(_body())


def _fake_report(brief: str) -> dict:
    """Mirrors FOAParserEngine.analyze_opportunity()'s return shape (see
    engines/foa_parser.py) — Funding Opportunity Intelligence, Phase 1."""
    return {
        "report": {
            "executive_brief": brief,
            "eligibility_assessment": {"status": "Eligible", "explanation": "Looks eligible.", "issues": []},
            "complexity": {"level": "Moderate", "reason": "Standard requirements."},
            "opportunity_attractiveness": {"level": "High", "reason": "Strong strategic fit."},
            "disclaimer": "This report is AI-generated decision support...",
            "human_in_the_loop_note": "A qualified person must review this report...",
        },
        "summary": brief,
        "eligibility_status": "Eligible",
        "eligibility_summary": "Looks eligible.",
        "complexity": "Moderate",
        "attractiveness": "High",
        "attractiveness_reason": "Strong strategic fit.",
    }


def test_personal_foa_summarize_is_never_charged(client, registered_user, monkeypatch):
    import routers.foa as foa_router

    foa_id = _insert_foa(
        uploaded_by=registered_user["user_id"], raw_text="Some solicitation text.",
        ai_summary=None, org_id=None,
    )

    async def fake_analyze(raw_text):
        return _fake_report("A personal-use brief.")

    monkeypatch.setattr(foa_router.parser, "analyze_opportunity", fake_analyze)

    resp = client.post(f"/api/v1/foa/{foa_id}/summarize", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ai_summary"] == "A personal-use brief."
    assert body["intelligence_report"]["executive_brief"] == "A personal-use brief."
    assert body["eligibility_status"] == "Eligible"
    assert body["complexity"] == "Moderate"
    assert body["attractiveness"] == "High"
    # No AIServiceTransaction should have been written for a personal record.
    # Scoped to this FOA's own id (via the reference column) rather than a
    # global count: the test DB is shared across the whole suite (see
    # conftest.py), so a global count would be polluted by every other test
    # file's transactions.

    async def _transactions_for_this_foa():
        from models.db_models import AIServiceTransaction
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AIServiceTransaction))
            return [t for t in result.scalars().all() if (t.reference or {}).get("foa_id") == foa_id]

    assert _run(_transactions_for_this_foa()) == []


def test_org_scoped_foa_summarize_consumes_complimentary_allowance(client, registered_user, monkeypatch):
    import routers.foa as foa_router

    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "professional")

    foa_id = _insert_foa(
        uploaded_by=registered_user["user_id"], raw_text="Org-shared solicitation text.",
        ai_summary=None, org_id=org_id,
    )

    async def fake_analyze(raw_text):
        return _fake_report("An org-scoped brief.")

    monkeypatch.setattr(foa_router.parser, "analyze_opportunity", fake_analyze)

    resp = client.post(f"/api/v1/foa/{foa_id}/summarize", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ai_summary"] == "An org-scoped brief."
    assert body["intelligence_report"]["disclaimer"]
    assert body["intelligence_report"]["human_in_the_loop_note"]

    async def _get_transaction():
        from models.db_models import AIServiceTransaction
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AIServiceTransaction).where(AIServiceTransaction.org_id == org_id)
            )
            return result.scalar_one()

    transaction = _run(_get_transaction())
    assert transaction.service_key == "grant_opportunity_analysis"
    assert transaction.funding_source == "complimentary"
    assert transaction.reference == {"foa_id": foa_id}


def test_org_scoped_foa_summarize_402s_when_no_allowance_and_no_balance(client, registered_user, monkeypatch):
    import routers.foa as foa_router

    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "free")

    async def _drain():
        async with AsyncSessionLocal() as db:
            credit_engine = CreditEngine()
            ledger = await credit_engine.get_or_create_ledger(db, org_id)
            ledger.balance = 0.0
            await db.commit()
    _run(_drain())

    foa_id = _insert_foa(
        uploaded_by=registered_user["user_id"], raw_text="Org-shared solicitation text.",
        ai_summary=None, org_id=org_id,
    )

    async def fail_if_called(raw_text):
        raise AssertionError("analyze_opportunity() should never run when the charge is rejected")

    monkeypatch.setattr(foa_router.parser, "analyze_opportunity", fail_if_called)

    resp = client.post(f"/api/v1/foa/{foa_id}/summarize", headers=registered_user["headers"])
    assert resp.status_code == 402


# ── Admin — Configurable Pricing Controls (Phase 3.1) ────────────────────
# Enterprise Pricing spec §9.3. No admin-user-creation endpoint exists —
# is_superadmin is set directly via the ORM, same "insert what a future
# flow will eventually create" convention _set_org_plan already uses above.

def _make_superadmin(user_id: str) -> None:
    async def _body():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one()
            user.is_superadmin = True
            await db.commit()
    _run(_body())


def test_non_admin_cannot_update_catalog_price(client, registered_user):
    resp = client.patch(
        "/api/v1/service-catalog/admin/catalog/grant_opportunity_analysis",
        json={"subscriber_price_cents": 500},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 403


def test_non_admin_cannot_list_organizations(client, registered_user):
    resp = client.get("/api/v1/admin/organizations", headers=registered_user["headers"])
    assert resp.status_code == 403


def test_admin_can_update_catalog_price(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.patch(
        "/api/v1/service-catalog/admin/catalog/grant_opportunity_analysis",
        json={"subscriber_price_cents": 1500, "payg_price_cents": 1800},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subscriber_price_cents"] == 1500
    assert body["payg_price_cents"] == 1800

    # Reflected in a subsequent quote.
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "free")
    quote_resp = client.get(
        f"/api/v1/service-catalog/{org_id}/quote/grant_opportunity_analysis",
        headers=registered_user["headers"],
    )
    assert quote_resp.json()["price_cents"] == 1800


def test_admin_update_unknown_service_404s(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.patch(
        "/api/v1/service-catalog/admin/catalog/not_a_real_service",
        json={"subscriber_price_cents": 500},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 404


def test_admin_can_create_catalog_item(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.post(
        "/api/v1/service-catalog/admin/catalog",
        json={
            "service_key": "test_new_service", "category": "supporting_document",
            "name": "Test New Service", "workspace": "pre_award",
            "subscriber_price_cents": 999,
        },
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["service_key"] == "test_new_service"

    # Duplicate service_key is rejected.
    dupe_resp = client.post(
        "/api/v1/service-catalog/admin/catalog",
        json={
            "service_key": "test_new_service", "category": "supporting_document",
            "name": "Dupe", "workspace": "pre_award", "subscriber_price_cents": 100,
        },
        headers=registered_user["headers"],
    )
    assert dupe_resp.status_code == 409


def test_admin_can_create_and_update_allowance(client, registered_user):
    _make_superadmin(registered_user["user_id"])

    create_resp = client.post(
        "/api/v1/service-catalog/admin/allowances",
        json={"plan": "enterprise", "service_key": "grant_opportunity_analysis", "quantity": 20, "validity_days": 90},
        headers=registered_user["headers"],
    )
    assert create_resp.status_code == 200, create_resp.text
    allowance_id = create_resp.json()["id"]
    assert create_resp.json()["quantity"] == 20

    # Duplicate (plan, service_key) is rejected.
    dupe_resp = client.post(
        "/api/v1/service-catalog/admin/allowances",
        json={"plan": "enterprise", "service_key": "grant_opportunity_analysis", "quantity": 5},
        headers=registered_user["headers"],
    )
    assert dupe_resp.status_code == 409

    update_resp = client.patch(
        f"/api/v1/service-catalog/admin/allowances/{allowance_id}",
        json={"quantity": 50},
        headers=registered_user["headers"],
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["quantity"] == 50


def test_admin_allowance_for_unknown_service_404s(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.post(
        "/api/v1/service-catalog/admin/allowances",
        json={"plan": "team", "service_key": "not_a_real_service", "quantity": 1},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 404


def test_admin_can_list_and_override_org_plan(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _make_superadmin(registered_user["user_id"])

    list_resp = client.get("/api/v1/admin/organizations", headers=registered_user["headers"])
    assert list_resp.status_code == 200, list_resp.text
    assert any(o["id"] == org_id for o in list_resp.json())

    update_resp = client.patch(
        f"/api/v1/admin/organizations/{org_id}/plan",
        json={"plan": "organization"},
        headers=registered_user["headers"],
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["plan"] == "organization"

    # The new plan is what future entitlement grants read from.
    entitlements_resp = client.get(
        f"/api/v1/service-catalog/{org_id}/entitlements", headers=registered_user["headers"],
    )
    assert any(e["service_key"] == "grant_opportunity_analysis" and e["granted_quantity"] == 10 for e in entitlements_resp.json())
