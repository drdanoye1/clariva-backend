"""
Organizations router — RBAC (role changes) and audit logging, exercised
through the real HTTP endpoints against the isolated test database.
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


def test_create_org_makes_creator_owner(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    members = client.get(f"/api/v1/organizations/{org_id}/members", headers=registered_user["headers"]).json()
    assert len(members) == 1
    assert members[0]["role"] == "owner"


def test_owner_can_promote_and_demote_a_member(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    other = _register_and_login(client, "other")
    client.post(f"/api/v1/organizations/{org_id}/invite",
                json={"email": other["email"], "role": "viewer"}, headers=registered_user["headers"])

    resp = client.patch(
        f"/api/v1/organizations/{org_id}/members/{other['user_id']}/role",
        json={"role": "editor"}, headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "editor"

    members = client.get(f"/api/v1/organizations/{org_id}/members", headers=registered_user["headers"]).json()
    updated = next(m for m in members if m["user_id"] == other["user_id"])
    assert updated["role"] == "editor"


def test_cannot_demote_the_only_owner(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.patch(
        f"/api/v1/organizations/{org_id}/members/{registered_user['user_id']}/role",
        json={"role": "editor"}, headers=registered_user["headers"],
    )
    assert resp.status_code == 400


def test_viewer_cannot_change_roles(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    other = _register_and_login(client, "viewer")
    client.post(f"/api/v1/organizations/{org_id}/invite",
                json={"email": other["email"], "role": "viewer"}, headers=registered_user["headers"])

    resp = client.patch(
        f"/api/v1/organizations/{org_id}/members/{registered_user['user_id']}/role",
        json={"role": "viewer"}, headers=other["headers"],
    )
    assert resp.status_code == 403


def test_invalid_role_rejected(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.patch(
        f"/api/v1/organizations/{org_id}/members/{registered_user['user_id']}/role",
        json={"role": "superuser"}, headers=registered_user["headers"],
    )
    assert resp.status_code == 400


def test_audit_log_records_org_creation_and_invites(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    other = _register_and_login(client, "audited")
    client.post(f"/api/v1/organizations/{org_id}/invite",
                json={"email": other["email"], "role": "viewer"}, headers=registered_user["headers"])

    resp = client.get(f"/api/v1/organizations/{org_id}/audit-log", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    actions = [e["action"] for e in resp.json()]
    assert "org.created" in actions
    assert "member.invited" in actions


def test_audit_log_requires_view_audit_log_permission(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    other = _register_and_login(client, "vieweronly")
    client.post(f"/api/v1/organizations/{org_id}/invite",
                json={"email": other["email"], "role": "viewer"}, headers=registered_user["headers"])

    resp = client.get(f"/api/v1/organizations/{org_id}/audit-log", headers=other["headers"])
    assert resp.status_code == 403


def test_audit_log_requires_org_membership(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    outsider = _register_and_login(client, "outsider")
    resp = client.get(f"/api/v1/organizations/{org_id}/audit-log", headers=outsider["headers"])
    assert resp.status_code == 403
