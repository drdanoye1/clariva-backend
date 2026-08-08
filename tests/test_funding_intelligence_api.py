"""
Funding Intelligence — foa.py's Phase 4 extensions and the new
routers/funding_intelligence.py, exercised through real HTTP endpoints.

FOARecord rows are inserted directly via the ORM (bypassing
upload/parse-text/parse-url, which call OpenAI and are out of scope for
this suite — see conftest.py's module docstring) so these tests can focus
on the pipeline/permission/reporting surface itself. The sync endpoint's
Grants.gov call is monkeypatched at the shared router-module engine
instance for the same "no real network calls in tests" reason.
"""
from __future__ import annotations

import uuid

import pytest

from database import AsyncSessionLocal
from models.db_models import FOARecord, new_uuid


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


def _invite_member(client, org_id: str, owner_headers: dict, email: str, role: str) -> None:
    resp = client.post(f"/api/v1/organizations/{org_id}/invite", json={"email": email, "role": role}, headers=owner_headers)
    assert resp.status_code == 200, resp.text


async def _insert_foa(**overrides) -> str:
    defaults = dict(
        id=new_uuid(), agency="NSF", program_title="Test Opportunity", phase="phase_i",
        grant_type="sbir",
    )
    defaults.update(overrides)
    async with AsyncSessionLocal() as db:
        db.add(FOARecord(**defaults))
        await db.commit()
    return defaults["id"]


def _insert_foa_sync(**overrides) -> str:
    import asyncio
    return asyncio.run(_insert_foa(**overrides))


# ── Pipeline stage / Bid-No-Go / assign ─────────────────────────────────────

def test_owner_can_update_pipeline_stage_on_personal_opportunity(client, registered_user):
    foa_id = _insert_foa_sync(uploaded_by=registered_user["user_id"])
    resp = client.patch(f"/api/v1/foa/{foa_id}/pipeline-stage", json={"stage": "qualifying"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["pipeline_stage"] == "qualifying"


def test_non_owner_cannot_touch_personal_opportunity(client, registered_user):
    foa_id = _insert_foa_sync(uploaded_by=registered_user["user_id"])
    stranger = _register_and_login(client, "stranger")
    resp = client.patch(f"/api/v1/foa/{foa_id}/pipeline-stage", json={"stage": "qualifying"}, headers=stranger["headers"])
    assert resp.status_code == 404


def test_invalid_stage_rejected(client, registered_user):
    foa_id = _insert_foa_sync(uploaded_by=registered_user["user_id"])
    resp = client.patch(f"/api/v1/foa/{foa_id}/pipeline-stage", json={"stage": "not_a_stage"}, headers=registered_user["headers"])
    assert resp.status_code == 400


def test_org_shared_pipeline_stage_requires_manage_pipeline(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    foa_id = _insert_foa_sync(uploaded_by=registered_user["user_id"], org_id=org_id)
    viewer = _register_and_login(client, "viewer")
    editor = _register_and_login(client, "editor")
    _invite_member(client, org_id, registered_user["headers"], viewer["email"], "viewer")
    _invite_member(client, org_id, registered_user["headers"], editor["email"], "editor")

    resp = client.patch(f"/api/v1/foa/{foa_id}/pipeline-stage", json={"stage": "pursuing"}, headers=viewer["headers"])
    assert resp.status_code == 403

    resp = client.patch(f"/api/v1/foa/{foa_id}/pipeline-stage", json={"stage": "pursuing"}, headers=editor["headers"])
    assert resp.status_code == 200, resp.text


def test_bid_no_go_endpoint_auto_advances_stage(client, registered_user):
    foa_id = _insert_foa_sync(uploaded_by=registered_user["user_id"])
    resp = client.patch(f"/api/v1/foa/{foa_id}/bid-no-go", json={"decision": "bid", "rationale": "Great fit"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bid_no_go_decision"] == "bid"
    assert body["pipeline_stage"] == "qualifying"


def test_bid_no_go_invalid_decision_rejected(client, registered_user):
    foa_id = _insert_foa_sync(uploaded_by=registered_user["user_id"])
    resp = client.patch(f"/api/v1/foa/{foa_id}/bid-no-go", json={"decision": "maybe"}, headers=registered_user["headers"])
    assert resp.status_code == 400


def test_bid_no_go_can_be_reset_to_undecided_without_reverting_stage(client, registered_user):
    """The frontend's reset ("x") control sends decision="undecided" — this
    must clear the decision but must NOT revert a stage that was already
    auto-advanced by the original bid/no_go call (see set_bid_no_go's
    auto-advance-only-forward comment in funding_intelligence_engine.py)."""
    foa_id = _insert_foa_sync(uploaded_by=registered_user["user_id"])
    client.patch(f"/api/v1/foa/{foa_id}/bid-no-go", json={"decision": "bid"}, headers=registered_user["headers"])

    resp = client.patch(f"/api/v1/foa/{foa_id}/bid-no-go", json={"decision": "undecided"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bid_no_go_decision"] == "undecided"
    assert body["pipeline_stage"] == "qualifying"  # untouched by the reset


def test_assign_opportunity(client, registered_user):
    foa_id = _insert_foa_sync(uploaded_by=registered_user["user_id"])
    resp = client.patch(f"/api/v1/foa/{foa_id}/assign", json={"assigned_to": registered_user["user_id"]}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["assigned_to"] == registered_user["user_id"]


# ── Sharing to an org ────────────────────────────────────────────────────────

def test_share_opportunity_requires_ownership_and_permission(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    foa_id = _insert_foa_sync(uploaded_by=registered_user["user_id"])

    stranger = _register_and_login(client, "sharestranger")
    resp = client.post(f"/api/v1/foa/{foa_id}/share", json={"org_id": org_id}, headers=stranger["headers"])
    assert resp.status_code == 404

    resp = client.post(f"/api/v1/foa/{foa_id}/share", json={"org_id": org_id}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["org_id"] == org_id

    resp = client.post(f"/api/v1/foa/{foa_id}/share", json={"org_id": org_id}, headers=registered_user["headers"])
    assert resp.status_code == 409


# ── Stage history ────────────────────────────────────────────────────────────

def test_stage_history_reflects_transitions(client, registered_user):
    foa_id = _insert_foa_sync(uploaded_by=registered_user["user_id"])
    client.patch(f"/api/v1/foa/{foa_id}/pipeline-stage", json={"stage": "qualifying"}, headers=registered_user["headers"])
    client.patch(f"/api/v1/foa/{foa_id}/pipeline-stage", json={"stage": "pursuing"}, headers=registered_user["headers"])

    resp = client.get(f"/api/v1/foa/{foa_id}/stage-history", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    stages = [e["to_stage"] for e in resp.json()]
    assert stages == ["qualifying", "pursuing"]


# ── Listing & comparison ─────────────────────────────────────────────────────

def test_list_pipeline_personal_scope(client, registered_user):
    _insert_foa_sync(uploaded_by=registered_user["user_id"], program_title="Mine")
    other = _register_and_login(client, "otherowner")
    _insert_foa_sync(uploaded_by=other["user_id"], program_title="Not Mine")

    resp = client.get("/api/v1/foa/pipeline", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    titles = [r["program_title"] for r in resp.json()]
    assert "Mine" in titles
    assert "Not Mine" not in titles


def test_list_pipeline_org_scope_requires_membership(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _insert_foa_sync(uploaded_by=registered_user["user_id"], org_id=org_id)
    outsider = _register_and_login(client, "pipelineoutsider")

    resp = client.get("/api/v1/foa/pipeline", params={"org_id": org_id}, headers=outsider["headers"])
    assert resp.status_code == 403

    resp = client.get("/api/v1/foa/pipeline", params={"org_id": org_id}, headers=registered_user["headers"])
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_compare_skips_inaccessible_records(client, registered_user):
    mine_id = _insert_foa_sync(uploaded_by=registered_user["user_id"], program_title="Mine")
    other = _register_and_login(client, "compareother")
    theirs_id = _insert_foa_sync(uploaded_by=other["user_id"], program_title="Theirs")

    resp = client.get("/api/v1/foa/compare", params={"ids": f"{mine_id},{theirs_id}"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    titles = [r["program_title"] for r in resp.json()]
    assert titles == ["Mine"]


def test_get_foa_returns_400_when_not_yet_parsed(client, registered_user):
    foa_id = _insert_foa_sync(uploaded_by=registered_user["user_id"], parsed_template=None)
    resp = client.get(f"/api/v1/foa/{foa_id}", headers=registered_user["headers"])
    assert resp.status_code == 400


def test_enrich_requires_grants_gov_source(client, registered_user):
    foa_id = _insert_foa_sync(uploaded_by=registered_user["user_id"], source="manual")
    resp = client.post(f"/api/v1/foa/{foa_id}/enrich", headers=registered_user["headers"])
    assert resp.status_code == 400


# ── Sync (network mocked) ────────────────────────────────────────────────────

def test_sync_endpoint_creates_records_and_reports_sam_gov_unconfigured(client, registered_user, monkeypatch):
    import routers.funding_intelligence as fi_router

    async def fake_fetch_grants_gov(**kwargs):
        return [{"id": "999", "number": "SYNC-TEST-1", "title": "Synced Opportunity", "agencyCode": "NSF", "closeDate": ""}]

    monkeypatch.setattr(fi_router.engine, "fetch_grants_gov", fake_fetch_grants_gov)

    resp = client.post("/api/v1/funding/sync", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    results = {r["source"]: r for r in resp.json()}
    assert results["grants_gov"]["created_count"] == 1
    assert results["sam_gov"]["configured"] is False

    listed = client.get("/api/v1/foa/pipeline", headers=registered_user["headers"]).json()
    assert any(r["program_title"] == "Synced Opportunity" for r in listed)


def test_sync_requires_manage_watchlists_for_org_scope(client, registered_user, monkeypatch):
    import routers.funding_intelligence as fi_router

    async def fake_fetch_grants_gov(**kwargs):
        return []

    monkeypatch.setattr(fi_router.engine, "fetch_grants_gov", fake_fetch_grants_gov)

    org_id = _create_org(client, registered_user["headers"])
    viewer = _register_and_login(client, "syncviewer")
    _invite_member(client, org_id, registered_user["headers"], viewer["email"], "viewer")

    resp = client.post("/api/v1/funding/sync", params={"org_id": org_id}, headers=viewer["headers"])
    assert resp.status_code == 403

    resp = client.post("/api/v1/funding/sync", params={"org_id": org_id}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text


# ── Watchlists ───────────────────────────────────────────────────────────────

def test_watchlist_crud_personal(client, registered_user):
    resp = client.post("/api/v1/funding/watchlists", json={"name": "My Watchlist", "keyword": "solar"}, headers=registered_user["headers"])
    assert resp.status_code == 201, resp.text
    watchlist_id = resp.json()["id"]

    resp = client.get("/api/v1/funding/watchlists", headers=registered_user["headers"])
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.patch(f"/api/v1/funding/watchlists/{watchlist_id}", json={"active": False}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["active"] is False

    resp = client.delete(f"/api/v1/funding/watchlists/{watchlist_id}", headers=registered_user["headers"])
    assert resp.status_code == 204


def test_watchlist_org_scope_requires_manage_watchlists(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    viewer = _register_and_login(client, "watchlistviewer")
    _invite_member(client, org_id, registered_user["headers"], viewer["email"], "viewer")

    resp = client.post("/api/v1/funding/watchlists", json={"name": "Org WL"}, params={"org_id": org_id}, headers=viewer["headers"])
    assert resp.status_code == 403

    resp = client.post("/api/v1/funding/watchlists", json={"name": "Org WL"}, params={"org_id": org_id}, headers=registered_user["headers"])
    assert resp.status_code == 201, resp.text


def test_watchlist_not_found(client, registered_user):
    resp = client.patch("/api/v1/funding/watchlists/does-not-exist", json={"active": False}, headers=registered_user["headers"])
    assert resp.status_code == 404


# ── Pipeline report ──────────────────────────────────────────────────────────

def test_pipeline_report_personal(client, registered_user):
    foa_id = _insert_foa_sync(uploaded_by=registered_user["user_id"], estimated_award_ceiling=25000.0)
    client.patch(f"/api/v1/foa/{foa_id}/pipeline-stage", json={"stage": "awarded"}, headers=registered_user["headers"])

    resp = client.get("/api/v1/funding/pipeline-report", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_opportunities"] == 1
    assert body["by_stage"]["awarded"] == 1
    assert body["win_rate"] == 1.0


def test_pipeline_report_org_scope_requires_membership(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    outsider = _register_and_login(client, "reportoutsider")
    resp = client.get("/api/v1/funding/pipeline-report", params={"org_id": org_id}, headers=outsider["headers"])
    assert resp.status_code == 403
