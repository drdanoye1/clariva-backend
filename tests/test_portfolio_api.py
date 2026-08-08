"""
Portfolio Dashboard router — exercised through the real HTTP endpoint
(Version 3.0 architecture upgrade, Phase 12). PortfolioEngine's math itself
is covered directly in tests/test_portfolio_engine.py; this file just
confirms the endpoint wires org_ids resolution and auth correctly.
"""
from __future__ import annotations

import uuid


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


def _proposal_payload(**overrides) -> dict:
    payload = {
        "title": "Portfolio Dashboard Test Proposal",
        "agency": "NSF", "phase": "phase_i", "grant_type": "sbir",
        "org_context": {"organization_name": "Acme Research", "industry": "AgTech"},
        "research_focus": "Low-power soil sensors",
        "innovation_description": "A mesh-networked sensor array",
    }
    payload.update(overrides)
    return payload


def test_summary_requires_auth(client):
    resp = client.get("/api/v1/portfolio/summary")
    assert resp.status_code == 401


def test_summary_reflects_proposal_and_award_lifecycle(client, registered_user):
    resp = client.get("/api/v1/portfolio/summary", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    baseline_pre_award = resp.json()["pre_award_count"]

    resp = client.post("/api/v1/proposals/", json=_proposal_payload(), headers=registered_user["headers"])
    assert resp.status_code == 201, resp.text
    proposal_id = resp.json()["proposal_id"]

    resp = client.get("/api/v1/portfolio/summary", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["pre_award_count"] == baseline_pre_award + 1

    resp = client.post(
        "/api/v1/awards", json={"proposal_id": proposal_id, "funding_agency": "NSF", "link_budget": False},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["award_status"] == "received"
    award_id = resp.json()["id"]

    resp = client.get("/api/v1/portfolio/summary", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pre_award_count"] == baseline_pre_award  # moved out of pre-award...
    assert body["award_received_count"] >= 1              # ...into award received

    resp = client.post(f"/api/v1/awards/{award_id}/activate", json={}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text

    resp = client.get("/api/v1/portfolio/summary", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["post_award_count"] >= 1
