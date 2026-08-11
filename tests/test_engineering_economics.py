"""
Phase 3 §4.7 — Administrator-Only Engineering Economics. Covers:

- engines/usage_tracking.py's pure helpers directly (estimate_cost_cents,
  usage_from_response) plus record_usage()/ensure_seeded() against a real
  DB session — same "engine methods are async, wrap in asyncio.run()"
  convention test_service_catalog.py and test_credit_engine.py use.
- engines/engineering_economics_engine.py's aggregation logic
  (get_operation_economics, get_marketplace_conversion,
  get_complimentary_conversion) against synthetic AIUsageRecord /
  AIServiceTransaction / MarketplacePurchase rows inserted directly via
  the ORM.
- The HTTP surface on routers/admin.py (GET /admin/economics, GET/PATCH
  /admin/model-pricing, GET/PATCH /admin/cost-config) — permission gating
  (403 for non-superadmin) and the update paths.

No OpenAI calls are made anywhere in this file — usage_from_response()
and estimate_cost_cents() are pure/deterministic, and record_usage() is
exercised with a hand-built fake response object rather than a real
completion, matching this suite's "no network calls anywhere" discipline
(see conftest.py's module docstring).
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database import AsyncSessionLocal
from engines import usage_tracking
from engines.engineering_economics_engine import EngineeringEconomicsEngine, _percentile
from models.db_models import (
    AIServiceTransaction, AIUsageRecord, MarketplaceListing, MarketplacePurchase,
    ModelPricingConfig, Organization, User, new_uuid,
)


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


# ── Pure helpers ─────────────────────────────────────────────────────────────

def test_percentile_empty_and_single():
    assert _percentile([], 50) == 0.0
    assert _percentile([7.0], 95) == 7.0


def test_percentile_matches_known_values():
    # 10 evenly spaced values 1..10 — nearest-rank-interpolated P50/P95 are
    # well-known reference points for this shape.
    values = [float(i) for i in range(1, 11)]
    assert _percentile(values, 50) == pytest.approx(5.5, abs=0.01)
    assert _percentile(values, 95) == pytest.approx(9.55, abs=0.01)


def test_estimate_cost_cents_uses_given_pricing():
    # 1000 prompt + 1000 completion tokens @ (0.25, 1.0) cents/1k =
    # 0.25 + 1.00 = 1.25 cents.
    cost = usage_tracking.estimate_cost_cents(
        "gpt-4o", 1000, 1000, pricing={"gpt-4o": (0.25, 1.0)},
    )
    assert cost == pytest.approx(1.25)


def test_estimate_cost_cents_falls_back_for_unknown_model():
    # No pricing dict entry for "made-up-model" -> FALLBACK_PRICING (gpt-4o's rate).
    cost = usage_tracking.estimate_cost_cents("made-up-model", 1000, 0, pricing={})
    assert cost == pytest.approx(usage_tracking.FALLBACK_PRICING[0])


def test_usage_from_response_tolerates_missing_usage():
    assert usage_tracking.usage_from_response(SimpleNamespace()) == (0, 0)
    assert usage_tracking.usage_from_response(
        SimpleNamespace(usage=SimpleNamespace(prompt_tokens=42, completion_tokens=7))
    ) == (42, 7)


# ── usage_tracking.record_usage() + ensure_seeded() against a real DB ───────

def test_ensure_seeded_is_idempotent(client):
    # `client` fixture triggers lifespan startup, which already calls
    # ensure_seeded() once — calling it again must not create duplicates
    # or raise on the unique `model` constraint.
    async def _body():
        async with AsyncSessionLocal() as db:
            await usage_tracking.ensure_seeded(db)
            await usage_tracking.ensure_seeded(db)
            await db.commit()
            result = await db.execute(select(ModelPricingConfig.model))
            models = [row[0] for row in result.all()]
            assert models.count("gpt-4o") == 1
            assert models.count("gpt-4o-mini") == 1
    _run(_body())


def test_record_usage_computes_cogs_from_seeded_pricing(client):
    async def _body():
        async with AsyncSessionLocal() as db:
            record = await usage_tracking.record_usage(
                db, operation="test:op", model="gpt-4o",
                prompt_tokens=1000, completion_tokens=1000,
                price_cents_charged=500, reference={"k": "v"},
            )
            await db.commit()
            assert record is not None
            # (1000/1000)*0.25 + (1000/1000)*1.0 = 1.25 cents, per the seeded
            # gpt-4o rate (see MODEL_PRICING_SEED).
            assert record.cogs_cents == pytest.approx(1.25)
            assert record.price_cents_charged == 500
    _run(_body())


def test_record_usage_never_raises_on_bad_input(client):
    # A usage-recording failure must never surface as an exception to the
    # caller — see record_usage()'s docstring. Passing an org_id that
    # doesn't exist would violate the FK in a strict backend; this must
    # still return None quietly rather than propagate.
    async def _body():
        async with AsyncSessionLocal() as db:
            result = await usage_tracking.record_usage(
                db, operation="test:op", model="gpt-4o",
                prompt_tokens=10, completion_tokens=10,
                org_id="not-a-real-org-id-" + uuid.uuid4().hex,
            )
            # SQLite with no FK enforcement configured may actually
            # succeed here; either a real AIUsageRecord or None is an
            # acceptable outcome — what matters is nothing raised.
            assert result is None or result.operation == "test:op"
    _run(_body())


# ── EngineeringEconomicsEngine aggregation ───────────────────────────────────

@pytest.fixture()
def econ_engine():
    return EngineeringEconomicsEngine()


def _insert_usage_records(rows):
    """rows: list of (operation, model, prompt_tokens, completion_tokens, cogs_cents, price_cents_charged)"""
    async def _body():
        async with AsyncSessionLocal() as db:
            for operation, model, pt, ct, cogs, price in rows:
                db.add(AIUsageRecord(
                    id=new_uuid(), operation=operation, model=model,
                    prompt_tokens=pt, completion_tokens=ct,
                    cogs_cents=cogs, price_cents_charged=price,
                ))
            await db.commit()
    _run(_body())


def test_get_operation_economics_aggregates_by_operation(client, econ_engine):
    _insert_usage_records([
        ("opA", "gpt-4o", 100, 100, 1.0, 500),
        ("opA", "gpt-4o", 200, 200, 2.0, 500),
        ("opA", "gpt-4o", 300, 300, 3.0, 500),
        ("opB", "gpt-4o-mini", 50, 50, 0.1, 0),
    ])

    async def _body():
        async with AsyncSessionLocal() as db:
            return await econ_engine.get_operation_economics(db)
    operations = _run(_body())

    by_name = {o["operation"]: o for o in operations}
    assert by_name["opA"]["transaction_count"] == 3
    assert by_name["opA"]["revenue_cents"] == 1500
    assert by_name["opA"]["cogs_cents"] == pytest.approx(6.0)
    assert by_name["opA"]["gross_margin_cents"] == pytest.approx(1494.0)
    assert by_name["opA"]["gross_margin_pct"] == pytest.approx(1494.0 / 1500 * 100.0, abs=0.01)
    assert by_name["opA"]["avg_prompt_tokens"] == pytest.approx(200.0)
    # P50 of [1.0, 2.0, 3.0] is the middle value.
    assert by_name["opA"]["p50_cogs_cents"] == pytest.approx(2.0)

    # Free/unmetered operation: $0 revenue -> gross_margin_pct is None,
    # not a divide-by-zero crash or a misleading 0%.
    assert by_name["opB"]["revenue_cents"] == 0
    assert by_name["opB"]["gross_margin_pct"] is None


def test_get_totals_rolls_up_every_operation(econ_engine):
    operations = [
        {"operation": "a", "transaction_count": 2, "revenue_cents": 100, "cogs_cents": 10.0},
        {"operation": "b", "transaction_count": 3, "revenue_cents": 200, "cogs_cents": 20.0},
    ]
    totals = econ_engine.get_totals(operations)
    assert totals["transaction_count"] == 5
    assert totals["revenue_cents"] == 300
    assert totals["cogs_cents"] == pytest.approx(30.0)
    assert totals["gross_margin_cents"] == pytest.approx(270.0)
    assert totals["gross_margin_pct"] == pytest.approx(90.0)


def test_get_totals_handles_zero_revenue(econ_engine):
    totals = econ_engine.get_totals([{"operation": "a", "transaction_count": 1, "revenue_cents": 0, "cogs_cents": 5.0}])
    assert totals["gross_margin_pct"] is None


def _create_org_row(name: str) -> str:
    org_id = new_uuid()
    async def _body():
        async with AsyncSessionLocal() as db:
            db.add(Organization(id=org_id, name=name, created_by=new_uuid(), plan="free"))
            await db.commit()
    _run(_body())
    return org_id


def test_get_marketplace_conversion(client, econ_engine):
    """Uses a before/after delta rather than exact totals — other test
    files in this suite (test_marketplace.py) also create real
    MarketplacePurchase rows against the same shared session-scoped test
    database (see conftest.py), so an exact-count assertion on the
    platform-wide totals would be order-dependent. The per-listing
    breakdown for *this test's own* listing_id is still checked exactly."""
    async def _get():
        async with AsyncSessionLocal() as db:
            return await econ_engine.get_marketplace_conversion(db)
    before = _run(_get())

    async def _body():
        async with AsyncSessionLocal() as db:
            buyer1 = new_uuid()
            buyer2 = new_uuid()
            db.add(User(id=buyer1, email=f"{buyer1}@example.com", hashed_password="x", full_name="B1", organization="O"))
            db.add(User(id=buyer2, email=f"{buyer2}@example.com", hashed_password="x", full_name="B2", organization="O"))
            org1 = Organization(id=new_uuid(), name="Buyer Org 1", created_by=buyer1, plan="free")
            org2 = Organization(id=new_uuid(), name="Buyer Org 2", created_by=buyer2, plan="free")
            db.add_all([org1, org2])
            creator = new_uuid()
            db.add(User(id=creator, email=f"{creator}@example.com", hashed_password="x", full_name="Creator", organization="O"))
            listing = MarketplaceListing(
                id=new_uuid(), listing_type="template_pack", name="Test Listing",
                price_cents=1000, status="published", created_by=creator,
            )
            db.add(listing)
            await db.flush()
            db.add(MarketplacePurchase(
                id=new_uuid(), listing_id=listing.id, buyer_org_id=org1.id,
                purchased_by=buyer1, price_cents_paid=1000,
            ))
            db.add(MarketplacePurchase(
                id=new_uuid(), listing_id=listing.id, buyer_org_id=org2.id,
                purchased_by=buyer2, price_cents_paid=1000,
            ))
            await db.commit()
            return listing.id

    listing_id = _run(_body())
    after = _run(_get())

    assert after["total_purchases"] == before["total_purchases"] + 2
    assert after["total_revenue_cents"] == before["total_revenue_cents"] + 2000
    assert after["listings_with_at_least_one_sale"] == before["listings_with_at_least_one_sale"] + 1
    assert after["published_listing_count"] == before["published_listing_count"] + 1

    # top_listings is capped to the top 10 by revenue — verify this
    # listing's own purchase rows directly instead of assuming it made the
    # cut (a listing bought for $20 total in this test may rank below
    # higher-value purchases created by other test files in this shared
    # session-scoped database).
    async def _verify():
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(MarketplacePurchase.price_cents_paid).where(MarketplacePurchase.listing_id == listing_id)
            )
            return [row[0] for row in result.all()]
    prices = _run(_verify())
    assert sorted(prices) == [1000, 1000]


def test_get_complimentary_conversion(client, econ_engine):
    # A unique, per-test service_key rather than a real catalog key like
    # "grant_opportunity_analysis" — other test files (test_service_catalog.py)
    # write real AIServiceTransaction rows against that real key in this
    # same shared session-scoped test database (see conftest.py), which
    # would make an exact-count assertion order-dependent. No FK
    # enforcement is configured for SQLite in this test DB (see
    # database.py), so an unseeded service_key is safe to use here.
    service_key = f"test-econ-service-{uuid.uuid4().hex[:10]}"

    async def _body():
        async with AsyncSessionLocal() as db:
            org_converted = Organization(id=new_uuid(), name="Converted Org", created_by=new_uuid(), plan="free")
            org_free_only = Organization(id=new_uuid(), name="Free-Only Org", created_by=new_uuid(), plan="free")
            db.add_all([org_converted, org_free_only])
            await db.flush()
            # org_converted used the complimentary allowance, then later paid.
            db.add(AIServiceTransaction(
                id=new_uuid(), org_id=org_converted.id, service_key=service_key,
                funding_source="complimentary", price_cents=0,
            ))
            db.add(AIServiceTransaction(
                id=new_uuid(), org_id=org_converted.id, service_key=service_key,
                funding_source="ai_services_balance", price_cents=1000,
            ))
            # org_free_only only ever used the complimentary allowance.
            db.add(AIServiceTransaction(
                id=new_uuid(), org_id=org_free_only.id, service_key=service_key,
                funding_source="complimentary", price_cents=0,
            ))
            await db.commit()

    _run(_body())

    async def _get():
        async with AsyncSessionLocal() as db:
            return await econ_engine.get_complimentary_conversion(db)
    breakdown = _run(_get())

    row = next(r for r in breakdown if r["service_key"] == service_key)
    assert row["orgs_used_complimentary"] == 2
    assert row["orgs_used_paid"] == 1
    assert row["orgs_converted"] == 1
    assert row["conversion_rate_pct"] == pytest.approx(50.0)


# ── HTTP surface (routers/admin.py) ─────────────────────────────────────────

def test_economics_dashboard_requires_superadmin(client, registered_user):
    resp = client.get("/api/v1/admin/economics", headers=registered_user["headers"])
    assert resp.status_code == 403


def test_economics_dashboard_returns_seeded_pricing_and_costs(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.get("/api/v1/admin/economics", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "operations" in body
    assert "totals" in body
    assert "marketplace" in body
    assert "complimentary_conversion" in body
    model_names = {m["model"] for m in body["model_pricing"]}
    assert "gpt-4o" in model_names
    assert "gpt-4o-mini" in model_names
    cost_keys = {c["key"] for c in body["platform_costs"]}
    assert "search_api_cost_cents" in cost_keys
    assert "rag_retrieval_cost_cents" in cost_keys


def test_non_admin_cannot_list_or_update_model_pricing(client, registered_user):
    list_resp = client.get("/api/v1/admin/model-pricing", headers=registered_user["headers"])
    assert list_resp.status_code == 403

    patch_resp = client.patch(
        "/api/v1/admin/model-pricing/gpt-4o",
        json={"input_cost_cents_per_1k": 1.0},
        headers=registered_user["headers"],
    )
    assert patch_resp.status_code == 403


def test_admin_can_update_model_pricing(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.patch(
        "/api/v1/admin/model-pricing/gpt-4o",
        json={"input_cost_cents_per_1k": 0.5, "output_cost_cents_per_1k": 2.0},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["input_cost_cents_per_1k"] == pytest.approx(0.5)
    assert body["output_cost_cents_per_1k"] == pytest.approx(2.0)

    # Reflected in a subsequent record_usage() COGS computation.
    async def _body():
        async with AsyncSessionLocal() as db:
            record = await usage_tracking.record_usage(
                db, operation="test:pricing_reflected", model="gpt-4o",
                prompt_tokens=1000, completion_tokens=1000,
            )
            await db.commit()
            assert record.cogs_cents == pytest.approx(0.5 + 2.0)
    _run(_body())


def test_admin_update_unknown_model_pricing_404s(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.patch(
        "/api/v1/admin/model-pricing/not-a-real-model",
        json={"input_cost_cents_per_1k": 1.0},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 404


def test_non_admin_cannot_list_or_update_cost_config(client, registered_user):
    list_resp = client.get("/api/v1/admin/cost-config", headers=registered_user["headers"])
    assert list_resp.status_code == 403

    patch_resp = client.patch(
        "/api/v1/admin/cost-config/search_api_cost_cents",
        json={"value_cents": 5.0},
        headers=registered_user["headers"],
    )
    assert patch_resp.status_code == 403


def test_admin_can_update_cost_config(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.patch(
        "/api/v1/admin/cost-config/search_api_cost_cents",
        json={"value_cents": 3.5},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["value_cents"] == pytest.approx(3.5)


def test_admin_update_unknown_cost_config_key_404s(client, registered_user):
    _make_superadmin(registered_user["user_id"])
    resp = client.patch(
        "/api/v1/admin/cost-config/not-a-real-key",
        json={"value_cents": 1.0},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 404
