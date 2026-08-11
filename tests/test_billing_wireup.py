"""
Phase 3 §4.7 billing wire-up — tests for the four previously-unbilled
pieces of the service catalog that were wired up together: (1) Supporting
Documents + Budget Justification -> real doc_* catalog prices
(routers/documents_library.py, routers/budget.py), (2) Award Setup &
Activation + per-100-page overage (routers/awards.py::activate_award),
(3) Proposal Development tiered pricing, auto-derived from FOA/award data
(routers/proposals.py::generate_all_sections), (4) Post-Award Management
recurring monthly billing (scripts/run_monthly_billing.py).

Same "no real AI calls" policy as every other test file in this suite (see
conftest.py's module docstring): every charging path here is proven via
the drain-the-org's-balance-to-0-then-assert-402 technique from
test_service_catalog.py, since every one of these charges is applied
*before* the router ever reaches its OpenAI call — a 402 always aborts
with nothing generated and nothing persisted, so it's safe to exercise
through the real HTTP endpoint. The "org actually gets charged the right
amount and the right number of times" side is proven instead by charging
paths that don't require an AI call at all (activate_award — the
AI-generation report/narrative endpoints on the same router are separate
and untouched here).

Every org used below is left on the "free" plan specifically because
several of these catalog services (doc_cover_letter, award_setup_
activation, proposal_development_standard) have a one-time complimentary
"trial" allowance on the professional/team/organization plans (see
COMPLIMENTARY_ALLOWANCE_SEED) — using "free" (no complimentary allowance
at all) is the only way to guarantee a call actually reaches the paid AI
Services balance instead of silently being absorbed by a trial unit.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import storage
from database import AsyncSessionLocal
from engines.credit_engine import CreditEngine
from engines.service_catalog_engine import SERVICE_CATALOG_SEED
from engines.supporting_documents_engine import SUPPORTING_DOCUMENT_TYPES
from models.db_models import AIServiceTransaction, Award, Organization, Proposal
from routers.awards import AWARD_SETUP_INCLUDED_PAGES, AWARD_SETUP_PAGE_BLOCK, _post_award_tier_from_value
from routers.proposals import _derive_proposal_tier
from scripts import run_monthly_billing


def _run(coro):
    import asyncio
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fake_r2(monkeypatch):
    """Same fake as test_award_api.py — the award-activation overage test
    below uploads an intake document, which calls storage.upload_file()."""
    async def fake_upload_file(org_id, category, content, filename, content_type):
        return f"fake/{category}/{filename}"

    async def fake_get_download_url(storage_key, filename=None, expires_in=3600):
        return f"https://example-bucket.r2.example.com/{storage_key}"

    monkeypatch.setattr(storage, "upload_file", fake_upload_file)
    monkeypatch.setattr(storage, "get_download_url", fake_get_download_url)


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


def _set_balance(org_id: str, amount: float) -> None:
    async def _body():
        async with AsyncSessionLocal() as db:
            credit_engine = CreditEngine()
            ledger = await credit_engine.get_or_create_ledger(db, org_id)
            ledger.balance = amount
            await db.commit()
    _run(_body())


def _count_transactions(org_id: str, service_key: str) -> int:
    async def _body():
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AIServiceTransaction).where(
                    AIServiceTransaction.org_id == org_id,
                    AIServiceTransaction.service_key == service_key,
                )
            )
            return len(result.scalars().all())
    return _run(_body())


def _proposal_payload(**overrides) -> dict:
    payload = {
        "title": "Precision Agriculture Sensor Network",
        "agency": "NSF", "phase": "phase_i", "grant_type": "sbir",
        "org_context": {"organization_name": "Acme Research", "industry": "AgTech"},
        "research_focus": "Low-power soil sensors",
        "innovation_description": "A mesh-networked sensor array",
    }
    payload.update(overrides)
    return payload


def _create_proposal(client, headers: dict) -> str:
    resp = client.post("/api/v1/proposals/", json=_proposal_payload(), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["proposal_id"]


def _get_proposal_row(proposal_id: str) -> Proposal:
    async def _body():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Proposal).where(Proposal.id == proposal_id))
            return result.scalar_one()
    return _run(_body())


def _get_award_row(award_id: str) -> Award:
    async def _body():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Award).where(Award.id == award_id))
            return result.scalar_one()
    return _run(_body())


# ── Pure/no-DB: catalog mapping + tier-derivation helpers ──────────────────

def test_every_supporting_document_type_has_a_real_catalog_price():
    """SUPPORTING_DOCUMENT_TYPES is the frontend's type dropdown registry;
    generate_supporting_document() charges f"doc_{doc_type}". If a type were
    ever added to one registry and not the other, consume() would raise
    ServiceNotFoundError (an uncaught 500) the first time anyone generated
    it — this catches that class of bug without needing the app running."""
    catalog_keys = {s["service_key"] for s in SERVICE_CATALOG_SEED}
    for doc_type in SUPPORTING_DOCUMENT_TYPES:
        assert f"doc_{doc_type}" in catalog_keys, f"doc_{doc_type} missing from SERVICE_CATALOG_SEED"


def test_derive_proposal_tier_prefers_foa_complexity():
    assert _derive_proposal_tier(None) == "standard"
    assert _derive_proposal_tier(SimpleNamespace(complexity=None, estimated_award_ceiling=None)) == "standard"
    assert _derive_proposal_tier(SimpleNamespace(complexity="Low", estimated_award_ceiling=5_000_000)) == "standard"
    assert _derive_proposal_tier(SimpleNamespace(complexity="Moderate", estimated_award_ceiling=5_000_000)) == "standard"
    assert _derive_proposal_tier(SimpleNamespace(complexity="High", estimated_award_ceiling=None)) == "advanced"
    assert _derive_proposal_tier(SimpleNamespace(complexity="Very High", estimated_award_ceiling=None)) == "complex"
    # Unrecognized complexity string falls back to standard rather than raising.
    assert _derive_proposal_tier(SimpleNamespace(complexity="Unknown", estimated_award_ceiling=None)) == "standard"


def test_derive_proposal_tier_falls_back_to_ceiling_when_no_complexity():
    assert _derive_proposal_tier(SimpleNamespace(complexity=None, estimated_award_ceiling=100_000)) == "standard"
    assert _derive_proposal_tier(SimpleNamespace(complexity=None, estimated_award_ceiling=500_000)) == "advanced"
    assert _derive_proposal_tier(SimpleNamespace(complexity=None, estimated_award_ceiling=1_999_999)) == "advanced"
    assert _derive_proposal_tier(SimpleNamespace(complexity=None, estimated_award_ceiling=2_000_000)) == "complex"


def test_post_award_tier_from_value():
    assert _post_award_tier_from_value(None) == "standard"
    assert _post_award_tier_from_value(0) == "standard"
    assert _post_award_tier_from_value(-100) == "standard"
    assert _post_award_tier_from_value(499_999) == "standard"
    assert _post_award_tier_from_value(500_000) == "advanced"
    assert _post_award_tier_from_value(1_999_999) == "advanced"
    assert _post_award_tier_from_value(2_000_000) == "complex"


def test_due_for_billing():
    from datetime import datetime, timezone
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    assert run_monthly_billing._due_for_billing(SimpleNamespace(post_award_last_billed_at=None), now) is True
    same_month = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert run_monthly_billing._due_for_billing(SimpleNamespace(post_award_last_billed_at=same_month), now) is False
    last_month = datetime(2026, 7, 30, tzinfo=timezone.utc)
    assert run_monthly_billing._due_for_billing(SimpleNamespace(post_award_last_billed_at=last_month), now) is True


# ── Supporting documents: real per-type catalog price ───────────────────────

def test_generate_supporting_document_402s_on_drained_balance(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "free")
    _set_balance(org_id, 0.0)
    proposal_id = _create_proposal(client, registered_user["headers"])

    resp = client.post(
        f"/api/v1/organizations/{org_id}/documents/generate-supporting",
        json={"doc_type": "cover_letter", "proposal_id": proposal_id},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 402
    # Nothing was charged and nothing was generated.
    assert _count_transactions(org_id, "doc_cover_letter") == 0
    docs = client.get(f"/api/v1/organizations/{org_id}/documents", headers=registered_user["headers"])
    assert docs.json() == []


def test_generate_supporting_document_402s_for_a_second_doc_type(client, registered_user):
    """A second, differently-priced doc_type — catches a mapping mismatch
    that a single-type test could miss (e.g. a typo'd service_key for one
    specific type while others are fine)."""
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "free")
    _set_balance(org_id, 0.0)
    proposal_id = _create_proposal(client, registered_user["headers"])

    resp = client.post(
        f"/api/v1/organizations/{org_id}/documents/generate-supporting",
        json={"doc_type": "capability_statement", "proposal_id": proposal_id},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 402
    assert _count_transactions(org_id, "doc_capability_statement") == 0


def test_generate_justification_404s_before_charging_when_no_budget_saved(client, registered_user):
    """Regression coverage for an ordering bug found while writing these
    tests: the org-scoped charge used to run before the "budget must exist"
    check, so a call against a proposal with no saved budget would charge
    the org for a 404. Budget existence is now checked first — confirm no
    AIServiceTransaction is written even though plenty of balance is
    available (i.e. this isn't just a lucky 402)."""
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "free")
    _set_balance(org_id, 1000.0)  # deliberately NOT drained
    proposal_id = _create_proposal(client, registered_user["headers"])

    resp = client.post(
        f"/api/v1/budget/{proposal_id}/generate-justification",
        params={"org_id": org_id},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 404
    assert _count_transactions(org_id, "doc_budget_justification") == 0


def test_generate_justification_402s_on_drained_balance_when_org_scoped(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "free")
    proposal_id = _create_proposal(client, registered_user["headers"])

    budget_resp = client.put(
        f"/api/v1/budget/{proposal_id}",
        json={"other_direct": [{"category": "Materials", "description": "Sensors", "cost": 5000}]},
        headers=registered_user["headers"],
    )
    assert budget_resp.status_code == 200, budget_resp.text

    _set_balance(org_id, 0.0)
    resp = client.post(
        f"/api/v1/budget/{proposal_id}/generate-justification",
        params={"org_id": org_id},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 402
    assert _count_transactions(org_id, "doc_budget_justification") == 0


# ── Award Setup & Activation + per-100-page overage ─────────────────────────

def test_activate_award_402s_before_activating_and_leaves_award_received(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "free")
    _set_balance(org_id, 0.0)

    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Drained Balance Test", "funding_agency": "NSF", "org_id": org_id, "total_award_value": 100000.0},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    award = resp.json()
    assert award["award_status"] == "received"

    resp = client.post(f"/api/v1/awards/{award['id']}/activate", json={}, headers=registered_user["headers"])
    assert resp.status_code == 402

    row = _get_award_row(award["id"])
    assert row.award_status == "received"  # activation never happened
    assert row.post_award_tier is None
    assert _count_transactions(org_id, "award_setup_activation") == 0


def test_activate_award_charges_setup_fee_and_sets_tier(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "free")
    _set_balance(org_id, 1000.0)

    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Standard Tier Activation Test", "funding_agency": "NSF", "org_id": org_id, "total_award_value": 300000.0},
        headers=registered_user["headers"],
    )
    award = resp.json()

    resp = client.post(f"/api/v1/awards/{award['id']}/activate", json={}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text

    assert _count_transactions(org_id, "award_setup_activation") == 1
    assert _count_transactions(org_id, "award_setup_additional_pages") == 0  # no intake docs uploaded

    row = _get_award_row(award["id"])
    assert row.award_status == "active"
    assert row.post_award_tier == "standard"  # $300k < $500k band


def test_activate_award_stays_free_for_personal_unshared_award(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/awards",
        json={"proposal_id": proposal_id, "funding_agency": "NSF", "total_award_value": 9_000_000.0, "link_budget": False},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    award = resp.json()
    assert award["org_id"] is None

    resp = client.post(f"/api/v1/awards/{award['id']}/activate", json={}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text

    row = _get_award_row(award["id"])
    assert row.award_status == "active"
    assert row.post_award_tier is None  # tier is only assigned on the org-scoped charging path

    # The test DB is shared across the whole pytest session (see conftest.py),
    # so a global "zero transactions anywhere" count would be polluted by
    # earlier tests' orgs — filter to transactions referencing this specific
    # award instead.
    async def _transactions_for_this_award():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AIServiceTransaction))
            return [t for t in result.scalars().all() if (t.reference or {}).get("award_id") == award["id"]]
    assert _run(_transactions_for_this_award()) == []


def test_activate_award_bills_page_overage_in_100_page_blocks(client, registered_user, monkeypatch):
    import routers.awards as awards_router
    # Deterministic page count regardless of what pdfplumber/pypdf make of
    # fake PDF bytes — 340 pages -> 190 pages over the 150 included ->
    # ceil(190/100) = 2 additional_pages blocks. _extract_text_from_pdf runs
    # first in intake_award_document and raises 422 on non-real PDF bytes,
    # so it needs stubbing out too — this test only cares about page-count
    # billing math, not text extraction.
    monkeypatch.setattr(awards_router, "_count_pdf_pages", lambda content: 340)
    monkeypatch.setattr(awards_router, "_extract_text_from_pdf", lambda content: "fake extracted text")

    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "free")
    _set_balance(org_id, 1000.0)

    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Page Overage Test", "funding_agency": "NSF", "org_id": org_id, "total_award_value": 100000.0},
        headers=registered_user["headers"],
    )
    award = resp.json()

    resp = client.post(
        f"/api/v1/awards/{award['id']}/intake/document",
        data={"org_id": org_id},
        files={"file": ("notice.pdf", b"%PDF-1.4 fake bytes", "application/pdf")},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text

    resp = client.post(f"/api/v1/awards/{award['id']}/activate", json={}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text

    assert _count_transactions(org_id, "award_setup_activation") == 1
    assert _count_transactions(org_id, "award_setup_additional_pages") == 2

    # Sanity check on the constants the block math depends on.
    assert AWARD_SETUP_INCLUDED_PAGES == 150
    assert AWARD_SETUP_PAGE_BLOCK == 100


def test_activate_award_402s_when_overage_unaffordable_even_if_base_fee_is(client, registered_user, monkeypatch):
    """Confirms overage blocks are charged in the SAME try/except as the
    base fee — if the org can afford award_setup_activation but not the
    additional_pages blocks on top of it, the whole activation aborts (no
    partial charge, no partial activation), not just a partial bill."""
    import routers.awards as awards_router
    monkeypatch.setattr(awards_router, "_count_pdf_pages", lambda content: 340)  # 2 overage blocks
    monkeypatch.setattr(awards_router, "_extract_text_from_pdf", lambda content: "fake extracted text")

    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "free")
    # Free-plan PAYG price for award_setup_activation is $100.00 exactly —
    # leave just enough for the base fee and nothing for either overage block.
    _set_balance(org_id, 100.0)

    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Unaffordable Overage Test", "funding_agency": "NSF", "org_id": org_id, "total_award_value": 100000.0},
        headers=registered_user["headers"],
    )
    award = resp.json()
    resp = client.post(
        f"/api/v1/awards/{award['id']}/intake/document",
        data={"org_id": org_id},
        files={"file": ("notice.pdf", b"%PDF-1.4 fake bytes", "application/pdf")},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text

    resp = client.post(f"/api/v1/awards/{award['id']}/activate", json={}, headers=registered_user["headers"])
    assert resp.status_code == 402

    row = _get_award_row(award["id"])
    assert row.award_status == "received"  # activation did not proceed
    # get_db() rolls back the whole request on the raised HTTPException, so
    # the base fee charge that succeeded before the overage loop failed is
    # rolled back too — not left as a partial, unrefunded charge.
    assert _count_transactions(org_id, "award_setup_activation") == 0


# ── Proposal Development: auto-derived tier, one-time fee ───────────────────

def test_generate_all_sections_402s_before_ai_call_on_first_draft(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "free")
    _set_balance(org_id, 0.0)
    proposal_id = _create_proposal(client, registered_user["headers"])

    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/generate-all",
        params={"org_id": org_id},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 402

    row = _get_proposal_row(proposal_id)
    assert row.development_fee_charged is False
    assert _count_transactions(org_id, "proposal_development_standard") == 0

    get_resp = client.get(f"/api/v1/proposals/{proposal_id}", headers=registered_user["headers"])
    assert all(not (s["content"] or "").strip() for s in get_resp.json()["sections"])


def test_generate_all_sections_402s_via_flat_fee_path_once_already_charged(client, registered_user):
    """Once development_fee_charged is True (a prior full draft already
    happened), a further org-scoped call must take the flat per-section
    debit_or_402 branch, not re-charge the tier fee — proven here by
    forcing the flag directly (no need to actually run a draft, which
    would require a real AI call) and confirming it still 402s cleanly
    before generation rather than silently falling through."""
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "free")
    proposal_id = _create_proposal(client, registered_user["headers"])

    async def _mark_charged():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Proposal).where(Proposal.id == proposal_id))
            row = result.scalar_one()
            row.development_fee_charged = True
            await db.commit()
    _run(_mark_charged())

    _set_balance(org_id, 0.0)
    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/generate-all",
        params={"org_id": org_id},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 402
    assert _count_transactions(org_id, "proposal_development_standard") == 0


# ── Post-Award Management: recurring monthly billing script ────────────────

def test_monthly_billing_bills_active_org_scoped_awards_and_is_idempotent(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "free")
    _set_balance(org_id, 1000.0)

    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Monthly Billing Test", "funding_agency": "NSF", "org_id": org_id, "total_award_value": 300000.0},
        headers=registered_user["headers"],
    )
    award = resp.json()
    resp = client.post(f"/api/v1/awards/{award['id']}/activate", json={}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text

    row = _get_award_row(award["id"])
    assert row.post_award_tier == "standard"
    assert row.post_award_last_billed_at is None

    _run(run_monthly_billing.main())

    row = _get_award_row(award["id"])
    assert row.post_award_last_billed_at is not None
    assert _count_transactions(org_id, "post_award_management_standard") == 1

    # Running again in the same month must not double-bill.
    _run(run_monthly_billing.main())
    assert _count_transactions(org_id, "post_award_management_standard") == 1


def test_monthly_billing_skips_awards_with_no_tier(client, registered_user):
    """An award predating the post_award_tier column (or one that somehow
    never got a tier assigned) must be skipped, never defaulted — see
    run_monthly_billing.py's module docstring."""
    org_id = _create_org(client, registered_user["headers"])
    _set_org_plan(org_id, "free")
    _set_balance(org_id, 1000.0)

    proposal_id = _create_proposal(client, registered_user["headers"])

    # Create an award WITHOUT going through the normal org-scoped
    # path (personal award), so it never gets a post_award_tier, then force
    # org_id/status directly to simulate a pre-migration row.
    resp = client.post(
        "/api/v1/awards",
        json={"proposal_id": proposal_id, "funding_agency": "NSF", "link_budget": False},
        headers=registered_user["headers"],
    )
    award = resp.json()

    async def _force_legacy_row():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Award).where(Award.id == award["id"]))
            row = result.scalar_one()
            row.org_id = org_id
            row.award_status = "active"
            row.post_award_tier = None
            await db.commit()
    _run(_force_legacy_row())

    _run(run_monthly_billing.main())
    assert _count_transactions(org_id, "post_award_management_standard") == 0
    assert _count_transactions(org_id, "post_award_management_advanced") == 0
    assert _count_transactions(org_id, "post_award_management_complex") == 0
