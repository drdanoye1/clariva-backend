"""
Version 3.0 architecture upgrade, Phase 3 §4.5 — Bulk Opportunity
Intelligence. Covers the two new endpoints on routers/foa.py:
POST /foa/bulk/rank (free Fit-Score ranking pass) and POST
/foa/bulk/analyze (paid deep analysis, looped per selected opportunity
through the same `_analyze_single_opportunity` helper the single-record
"Analyze Opportunity" endpoint uses — see test_service_catalog.py's
existing summarize() coverage for that helper's charge-gating behavior,
which this file does not re-test).

Same conventions as test_service_catalog.py: org plan is set directly via
the ORM (no "upgrade plan" endpoint exists yet), and FOARecords are
inserted directly for the same reason test_funding_intelligence_api.py
does. Uses the real `client` (TestClient) fixture for these endpoints
since they're plain HTTP handlers, not engine methods needing their own
asyncio.run() wrapper — matching test_funding_intelligence_api.py's style
for router-level tests.
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from database import AsyncSessionLocal
from models.db_models import FOARecord, Organization, User, new_uuid


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


def _set_org_plan(org_id: str, plan: str) -> None:
    async def _body():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Organization).where(Organization.id == org_id))
            org = result.scalar_one()
            org.plan = plan
            await db.commit()
    _run(_body())


def _insert_foa(**overrides) -> str:
    async def _body():
        defaults = dict(
            id=new_uuid(), agency="NSF", program_title="Test Opportunity", phase="phase_i",
            grant_type="sbir", pipeline_stage="identified",
        )
        defaults.update(overrides)
        async with AsyncSessionLocal() as db:
            db.add(FOARecord(**defaults))
            await db.commit()
        return defaults["id"]
    return _run(_body())


def _fake_report(brief: str) -> dict:
    """Mirrors FOAParserEngine.analyze_opportunity()'s return shape — see
    test_service_catalog.py's identical helper."""
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


# ── /bulk/rank ────────────────────────────────────────────────────────────────

def test_bulk_rank_requires_org_id(client):
    user = _register_and_login(client, "rank-noorg")
    resp = client.post("/api/v1/foa/bulk/rank", json={"foa_ids": ["x"]}, headers=user["headers"])
    assert resp.status_code == 400


def test_bulk_rank_403_for_free_plan(client):
    user = _register_and_login(client, "rank-free")
    org_id = _create_org(client, user["headers"])
    _set_org_plan(org_id, "free")
    resp = client.post("/api/v1/foa/bulk/rank", json={"foa_ids": [], "org_id": org_id}, headers=user["headers"])
    assert resp.status_code == 403


def test_bulk_rank_403_for_professional_plan(client):
    """Professional is a real paid plan but not one of the three the spec
    names ('Team, Organization and Enterprise') — confirms the gate checks
    an explicit allow-list, not just 'any paid plan'."""
    user = _register_and_login(client, "rank-pro")
    org_id = _create_org(client, user["headers"])
    _set_org_plan(org_id, "professional")
    resp = client.post("/api/v1/foa/bulk/rank", json={"foa_ids": [], "org_id": org_id}, headers=user["headers"])
    assert resp.status_code == 403


def test_bulk_rank_ranks_descending_and_stays_free(client, monkeypatch):
    import routers.foa as foa_router

    user = _register_and_login(client, "rank-team")
    org_id = _create_org(client, user["headers"])
    _set_org_plan(org_id, "team")

    low  = _insert_foa(org_id=org_id, program_title="Low Fit")
    high = _insert_foa(org_id=org_id, program_title="High Fit")
    mid  = _insert_foa(org_id=org_id, program_title="Mid Fit")

    fake_scores = {low: 20, high: 90, mid: 55}

    class _FakeFit:
        def __init__(self, score):
            self.overall_score = score
            self.bucket = "x"
            self.recommendation = "PURSUE"

    def fake_score(record, profile):
        return _FakeFit(fake_scores[record.id])

    monkeypatch.setattr(foa_router, "score_opportunity", fake_score)

    resp = client.post(
        "/api/v1/foa/bulk/rank",
        json={"foa_ids": [low, high, mid], "org_id": org_id},
        headers=user["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    titles = [r["program_title"] for r in body["results"]]
    assert titles == ["High Fit", "Mid Fit", "Low Fit"]
    assert body["disclaimer"]

    # Ranking must never write an AIServiceTransaction — it's the free step.
    # Scoped to this test's own org_id rather than a global count: the test
    # DB is shared across the whole suite (see conftest.py), so a global
    # count would be polluted by every other test file's orgs.
    async def _count_transactions():
        from models.db_models import AIServiceTransaction
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AIServiceTransaction).where(AIServiceTransaction.org_id == org_id)
            )
            return len(result.scalars().all())
    assert _run(_count_transactions()) == 0


def test_bulk_rank_excludes_records_outside_the_org(client):
    """A foa_id belonging to a different org (or personal) must not leak
    into another org's bulk-rank results, even if the caller lists it."""
    user = _register_and_login(client, "rank-scope")
    org_id = _create_org(client, user["headers"])
    _set_org_plan(org_id, "team")

    other_org_foa = _insert_foa(org_id="some-other-org", program_title="Not Mine")
    mine = _insert_foa(org_id=org_id, program_title="Mine")

    resp = client.post(
        "/api/v1/foa/bulk/rank",
        json={"foa_ids": [other_org_foa, mine], "org_id": org_id},
        headers=user["headers"],
    )
    assert resp.status_code == 200, resp.text
    ids = [r["foa_id"] for r in resp.json()["results"]]
    assert ids == [mine]


def test_bulk_rank_over_limit_400s(client):
    user = _register_and_login(client, "rank-limit")
    org_id = _create_org(client, user["headers"])
    _set_org_plan(org_id, "team")
    resp = client.post(
        "/api/v1/foa/bulk/rank",
        json={"foa_ids": [f"id-{i}" for i in range(51)], "org_id": org_id},
        headers=user["headers"],
    )
    assert resp.status_code == 400


# ── /bulk/analyze ─────────────────────────────────────────────────────────────

def test_bulk_analyze_requires_org_id(client):
    user = _register_and_login(client, "analyze-noorg")
    resp = client.post("/api/v1/foa/bulk/analyze", json={"foa_ids": ["x"]}, headers=user["headers"])
    assert resp.status_code == 400


def test_bulk_analyze_403_for_free_plan(client):
    user = _register_and_login(client, "analyze-free")
    org_id = _create_org(client, user["headers"])
    _set_org_plan(org_id, "free")
    resp = client.post("/api/v1/foa/bulk/analyze", json={"foa_ids": [], "org_id": org_id}, headers=user["headers"])
    assert resp.status_code == 403


def test_bulk_analyze_runs_each_selected_item_and_charges(client, monkeypatch):
    import routers.foa as foa_router

    user = _register_and_login(client, "analyze-team")
    org_id = _create_org(client, user["headers"])
    _set_org_plan(org_id, "team")

    foa_a = _insert_foa(org_id=org_id, program_title="Opportunity A", raw_text="Solicitation A text.")
    foa_b = _insert_foa(org_id=org_id, program_title="Opportunity B", raw_text="Solicitation B text.")

    async def fake_analyze(raw_text):
        return _fake_report(f"Brief for: {raw_text}")

    monkeypatch.setattr(foa_router.parser, "analyze_opportunity", fake_analyze)

    resp = client.post(
        "/api/v1/foa/bulk/analyze",
        json={"foa_ids": [foa_a, foa_b], "org_id": org_id},
        headers=user["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["analyzed_count"] == 2
    assert body["failed_count"] == 0
    assert {r["foa_id"] for r in body["results"]} == {foa_a, foa_b}
    assert all(r["success"] and not r["already_analyzed"] for r in body["results"])

    # Each item is its own AI Services transaction — bulk didn't bypass
    # per-item charging.
    async def _count_transactions():
        from models.db_models import AIServiceTransaction
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AIServiceTransaction).where(AIServiceTransaction.org_id == org_id)
            )
            return len(result.scalars().all())
    assert _run(_count_transactions()) == 2


def test_bulk_analyze_one_failure_does_not_abort_the_rest(client, monkeypatch):
    import routers.foa as foa_router

    user = _register_and_login(client, "analyze-partial")
    org_id = _create_org(client, user["headers"])
    _set_org_plan(org_id, "team")

    good = _insert_foa(org_id=org_id, program_title="Has Content", raw_text="Real solicitation text.")
    # No raw_text and not a grants_gov record -> _analyze_single_opportunity
    # raises 400 "No content available" for this one specifically.
    bad = _insert_foa(org_id=org_id, program_title="No Content", raw_text=None, source="manual", external_url=None)

    async def fake_analyze(raw_text):
        return _fake_report("A brief.")

    monkeypatch.setattr(foa_router.parser, "analyze_opportunity", fake_analyze)

    resp = client.post(
        "/api/v1/foa/bulk/analyze",
        json={"foa_ids": [good, bad], "org_id": org_id},
        headers=user["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["analyzed_count"] == 1
    assert body["failed_count"] == 1
    by_id = {r["foa_id"]: r for r in body["results"]}
    assert by_id[good]["success"] is True
    assert by_id[bad]["success"] is False
    assert by_id[bad]["error"]


def test_bulk_analyze_is_idempotent_for_already_analyzed_records(client, monkeypatch):
    import routers.foa as foa_router

    user = _register_and_login(client, "analyze-idempotent")
    org_id = _create_org(client, user["headers"])
    _set_org_plan(org_id, "team")

    already = _insert_foa(
        org_id=org_id, program_title="Already Analyzed", raw_text="text",
        intelligence_report={"executive_brief": "Existing brief.", "disclaimer": "x", "human_in_the_loop_note": "y"},
    )

    async def fail_if_called(raw_text):
        raise AssertionError("analyze_opportunity() should never run for an already-analyzed record")

    monkeypatch.setattr(foa_router.parser, "analyze_opportunity", fail_if_called)

    resp = client.post(
        "/api/v1/foa/bulk/analyze",
        json={"foa_ids": [already], "org_id": org_id},
        headers=user["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["analyzed_count"] == 1
    assert body["results"][0]["already_analyzed"] is True

    async def _count_transactions():
        from models.db_models import AIServiceTransaction
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AIServiceTransaction).where(AIServiceTransaction.org_id == org_id)
            )
            return len(result.scalars().all())
    assert _run(_count_transactions()) == 0  # never re-spent


def test_bulk_analyze_over_limit_400s(client):
    user = _register_and_login(client, "analyze-limit")
    org_id = _create_org(client, user["headers"])
    _set_org_plan(org_id, "team")
    resp = client.post(
        "/api/v1/foa/bulk/analyze",
        json={"foa_ids": [f"id-{i}" for i in range(51)], "org_id": org_id},
        headers=user["headers"],
    )
    assert resp.status_code == 400
