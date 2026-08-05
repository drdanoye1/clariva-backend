"""
Document Library router (Phase 3, PRD §14) — exercised through the real
HTTP endpoints: documents, versions, sharing (incl. the unauthenticated
external share-token endpoint), search (keyword fallback only — see
test_document_library_engine.py's module docstring on why no real OpenAI
calls happen in this suite), and retention policies.
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


def _create_document(client, org_id: str, headers: dict, **overrides) -> dict:
    payload = {"title": "Program Narrative", "library_type": "document", "content": "Initial draft content."}
    payload.update(overrides)
    resp = client.post(f"/api/v1/organizations/{org_id}/documents", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Documents & Versions ─────────────────────────────────────────────────────

def test_owner_creates_document_editor_can_too_viewer_cannot(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    editor = _register_and_login(client, "editor")
    viewer = _register_and_login(client, "viewer")
    _invite_member(client, org_id, registered_user["headers"], editor["email"], "editor")
    _invite_member(client, org_id, registered_user["headers"], viewer["email"], "viewer")

    doc = _create_document(client, org_id, registered_user["headers"])
    assert doc["version_count"] == 1
    assert doc["latest_version"]["version_number"] == 1
    assert doc["latest_version"]["content"] == "Initial draft content."

    resp = client.post(f"/api/v1/organizations/{org_id}/documents", json={"title": "Editor Doc"}, headers=editor["headers"])
    assert resp.status_code == 201, resp.text

    resp = client.post(f"/api/v1/organizations/{org_id}/documents", json={"title": "Viewer Doc"}, headers=viewer["headers"])
    assert resp.status_code == 403


def test_list_documents_requires_org_membership(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _create_document(client, org_id, registered_user["headers"])
    outsider = _register_and_login(client, "outsider")

    resp = client.get(f"/api/v1/organizations/{org_id}/documents", headers=outsider["headers"])
    assert resp.status_code == 403

    resp = client.get(f"/api/v1/organizations/{org_id}/documents", headers=registered_user["headers"])
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_publish_new_version_updates_latest_and_history(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"], content="v1")

    resp = client.post(
        f"/api/v1/documents/{doc['id']}/versions",
        json={"content": "v2", "change_note": "Incorporated reviewer feedback"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["version_number"] == 2

    versions = client.get(f"/api/v1/documents/{doc['id']}/versions", headers=registered_user["headers"]).json()
    assert [v["version_number"] for v in versions] == [2, 1]

    detail = client.get(f"/api/v1/documents/{doc['id']}", headers=registered_user["headers"]).json()
    assert detail["version_count"] == 2
    assert detail["latest_version"]["version_number"] == 2


def test_archive_and_restore_document(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"])

    resp = client.patch(f"/api/v1/documents/{doc['id']}/archive", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "archived"

    active = client.get(f"/api/v1/organizations/{org_id}/documents", headers=registered_user["headers"]).json()
    assert active == []

    resp = client.patch(f"/api/v1/documents/{doc['id']}/restore", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"


def test_delete_document(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"])

    resp = client.delete(f"/api/v1/documents/{doc['id']}", headers=registered_user["headers"])
    assert resp.status_code == 204

    resp = client.get(f"/api/v1/documents/{doc['id']}", headers=registered_user["headers"])
    assert resp.status_code == 404


# ── Sharing ──────────────────────────────────────────────────────────────────

def test_external_share_link_is_publicly_viewable_without_auth(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"], content="Confidential-ish content")

    resp = client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        json={"external_email": "funder@example.org", "permission": "view", "expires_in_days": 30},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["share_token"]
    assert token

    # No Authorization header at all — this is the public link endpoint.
    resp = client.get(f"/api/v1/shared/{token}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "Confidential-ish content"

    resp = client.get("/api/v1/shared/not-a-real-token")
    assert resp.status_code == 404


def test_internal_share_and_revoke(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"])
    colleague = _register_and_login(client, "colleague")

    resp = client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        json={"shared_with_user_id": colleague["user_id"], "permission": "comment"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 201, resp.text
    share_id = resp.json()["id"]
    assert resp.json()["share_token"] is None

    shares = client.get(f"/api/v1/documents/{doc['id']}/shares", headers=registered_user["headers"]).json()
    assert len(shares) == 1

    resp = client.delete(f"/api/v1/shares/{share_id}", headers=registered_user["headers"])
    assert resp.status_code == 204

    shares = client.get(f"/api/v1/documents/{doc['id']}/shares", headers=registered_user["headers"]).json()
    assert shares == []


def test_sharing_requires_manage_document_sharing_permission(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"])
    viewer = _register_and_login(client, "vieweronly")
    _invite_member(client, org_id, registered_user["headers"], viewer["email"], "viewer")

    resp = client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        json={"external_email": "nope@example.org"}, headers=viewer["headers"],
    )
    assert resp.status_code == 403


# ── Search ───────────────────────────────────────────────────────────────────

def test_search_falls_back_to_keyword_matching(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _create_document(client, org_id, registered_user["headers"], title="Renewable Energy Storage Study", content="battery chemistry research")
    _create_document(client, org_id, registered_user["headers"], title="Unrelated Staffing Plan", content="personnel and hours")

    resp = client.get(f"/api/v1/organizations/{org_id}/documents/search", params={"q": "renewable"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    results = resp.json()
    assert len(results) == 1
    assert results[0]["title"] == "Renewable Energy Storage Study"


# ── Retention Policies ───────────────────────────────────────────────────────

def test_set_retention_policy_requires_manage_retention_permission(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    editor = _register_and_login(client, "retentioneditor")
    _invite_member(client, org_id, registered_user["headers"], editor["email"], "editor")

    resp = client.put(
        f"/api/v1/organizations/{org_id}/retention-policies",
        json={"library_type": "document", "retention_days": 365}, headers=editor["headers"],
    )
    assert resp.status_code == 403

    resp = client.put(
        f"/api/v1/organizations/{org_id}/retention-policies",
        json={"library_type": "document", "retention_days": 365}, headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["retention_days"] == 365

    policies = client.get(f"/api/v1/organizations/{org_id}/retention-policies", headers=registered_user["headers"]).json()
    assert len(policies) == 1


def test_archive_expired_manual_action(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    _create_document(client, org_id, registered_user["headers"])

    client.put(f"/api/v1/organizations/{org_id}/retention-policies", json={"library_type": "document", "retention_days": 1}, headers=registered_user["headers"])

    resp = client.post(f"/api/v1/organizations/{org_id}/documents/archive-expired", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    # The document was just created, so it's not old enough to be archived yet.
    assert resp.json()["archived_count"] == 0
