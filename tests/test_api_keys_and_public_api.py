"""
API key management (routers/api_keys.py) and the Public API
(routers/public_api.py) — exercised through real HTTP endpoints, including
end-to-end: issue a key via the normal JWT-authenticated flow, then use
that key's plaintext value as an X-API-Key header against the public API,
exactly as an external partner would.
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


def _create_org(client, owner_headers: dict) -> str:
    resp = client.post("/api/v1/organizations/", json={"name": f"Org {uuid.uuid4().hex[:8]}"}, headers=owner_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _invite_member(client, org_id: str, owner_headers: dict, email: str, role: str) -> None:
    resp = client.post(f"/api/v1/organizations/{org_id}/invite", json={"email": email, "role": role}, headers=owner_headers)
    assert resp.status_code == 200, resp.text


def _proposal_payload(**overrides) -> dict:
    payload = {
        "title": "Rural Broadband Expansion", "agency": "USDA", "phase": "phase_i", "grant_type": "sbir",
        "org_context": {"organization_name": "Acme Research", "industry": "Telecom"},
        "research_focus": "Fixed wireless access", "innovation_description": "Mesh backhaul",
    }
    payload.update(overrides)
    return payload


def _create_proposal(client, headers: dict) -> str:
    resp = client.post("/api/v1/proposals/", json=_proposal_payload(), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["proposal_id"]


def _share_proposal(client, org_id: str, proposal_id: str, owner_headers: dict) -> None:
    resp = client.post(f"/api/v1/organizations/{org_id}/proposals", json={"proposal_id": proposal_id}, headers=owner_headers)
    assert resp.status_code == 200, resp.text


# ── API key management ───────────────────────────────────────────────────────

def test_owner_can_create_list_and_revoke_key_editor_cannot_create(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    editor = _register_and_login(client, "editor")
    _invite_member(client, org_id, registered_user["headers"], editor["email"], "editor")

    resp = client.post(f"/api/v1/organizations/{org_id}/api-keys", json={"name": "CI Bot", "role": "editor"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["key"].startswith("sk_live_")
    key_id = body["id"]

    resp = client.post(f"/api/v1/organizations/{org_id}/api-keys", json={"name": "Sneaky", "role": "viewer"}, headers=editor["headers"])
    assert resp.status_code == 403

    resp = client.get(f"/api/v1/organizations/{org_id}/api-keys", headers=registered_user["headers"])
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["key_prefix"] == body["key_prefix"]

    resp = client.delete(f"/api/v1/organizations/{org_id}/api-keys/{key_id}", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text


def test_invalid_role_rejected(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(f"/api/v1/organizations/{org_id}/api-keys", json={"name": "Bad", "role": "superuser"}, headers=registered_user["headers"])
    assert resp.status_code == 400


# ── Public API ───────────────────────────────────────────────────────────────

def test_public_api_requires_key(client):
    resp = client.get("/api/v1/public/proposals")
    assert resp.status_code == 401


def test_public_api_rejects_revoked_key(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(f"/api/v1/organizations/{org_id}/api-keys", json={"name": "Temp", "role": "owner"}, headers=registered_user["headers"])
    key, key_id = resp.json()["key"], resp.json()["id"]

    resp = client.get("/api/v1/public/proposals", headers={"X-API-Key": key})
    assert resp.status_code == 200

    client.delete(f"/api/v1/organizations/{org_id}/api-keys/{key_id}", headers=registered_user["headers"])
    resp = client.get("/api/v1/public/proposals", headers={"X-API-Key": key})
    assert resp.status_code == 401


def test_viewer_key_can_read_but_not_write_pipeline(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(f"/api/v1/organizations/{org_id}/api-keys", json={"name": "Read Only", "role": "viewer"}, headers=registered_user["headers"])
    key = resp.json()["key"]

    resp = client.get("/api/v1/public/pipeline", headers={"X-API-Key": key})
    assert resp.status_code == 200
    assert resp.json() == []

    resp = client.post(
        "/api/v1/public/pipeline", json={"agency": "NSF", "program_title": "New Opportunity"},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 403


def test_editor_key_can_create_pipeline_entry_scoped_to_its_org(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(f"/api/v1/organizations/{org_id}/api-keys", json={"name": "Writer", "role": "editor"}, headers=registered_user["headers"])
    key = resp.json()["key"]

    resp = client.post(
        "/api/v1/public/pipeline", json={"agency": "NSF", "program_title": "New Opportunity via API"},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["org_id"] == org_id
    assert resp.json()["pipeline_stage"] == "identified"

    resp = client.get("/api/v1/public/pipeline", headers={"X-API-Key": key})
    assert len(resp.json()) == 1


def test_public_api_proposal_and_award_are_scoped_to_key_org(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    _share_proposal(client, org_id, proposal_id, registered_user["headers"])

    award_resp = client.post(
        "/api/v1/awards", json={"proposal_id": proposal_id, "funding_agency": "USDA", "link_budget": False},
        headers=registered_user["headers"],
    )
    assert award_resp.status_code == 200, award_resp.text
    award_id = award_resp.json()["id"]
    assert award_resp.json()["org_id"] == org_id

    resp = client.post(f"/api/v1/organizations/{org_id}/api-keys", json={"name": "Partner", "role": "viewer"}, headers=registered_user["headers"])
    key = resp.json()["key"]

    resp = client.get("/api/v1/public/proposals", headers={"X-API-Key": key})
    assert resp.status_code == 200
    assert any(p["proposal_id"] == proposal_id for p in resp.json())

    resp = client.get(f"/api/v1/public/awards/{award_id}", headers={"X-API-Key": key})
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == award_id

    # A key from an unrelated org must not be able to see this org's award.
    other_owner = _register_and_login(client, "other")
    other_org = _create_org(client, other_owner["headers"])
    other_key = client.post(f"/api/v1/organizations/{other_org}/api-keys", json={"name": "Other", "role": "owner"}, headers=other_owner["headers"]).json()["key"]
    resp = client.get(f"/api/v1/public/awards/{award_id}", headers={"X-API-Key": other_key})
    assert resp.status_code == 404
