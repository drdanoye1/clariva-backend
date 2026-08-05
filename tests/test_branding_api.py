"""
White-label branding endpoints on organizations.py (Clariva Enterprise™
PRD §20) — exercised through real HTTP endpoints.
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
    return {"email": email, "headers": {"Authorization": f"Bearer {token}"}}


def _create_org(client, owner_headers: dict) -> str:
    resp = client.post("/api/v1/organizations/", json={"name": f"Org {uuid.uuid4().hex[:8]}"}, headers=owner_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _invite_member(client, org_id: str, owner_headers: dict, email: str, role: str) -> None:
    resp = client.post(f"/api/v1/organizations/{org_id}/invite", json={"email": email, "role": role}, headers=owner_headers)
    assert resp.status_code == 200, resp.text


def test_default_branding_is_unset(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.get(f"/api/v1/organizations/{org_id}/branding", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["white_label_enabled"] is False
    assert body["brand_name"] is None


def test_owner_can_update_branding_member_can_view_editor_cannot_update(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    editor = _register_and_login(client, "editor")
    _invite_member(client, org_id, registered_user["headers"], editor["email"], "editor")

    resp = client.patch(
        f"/api/v1/organizations/{org_id}/branding",
        json={"white_label_enabled": True, "brand_name": "Acme Grants", "primary_color": "#1d4ed8"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["brand_name"] == "Acme Grants"

    resp = client.get(f"/api/v1/organizations/{org_id}/branding", headers=editor["headers"])
    assert resp.status_code == 200
    assert resp.json()["brand_name"] == "Acme Grants"

    resp = client.patch(f"/api/v1/organizations/{org_id}/branding", json={"brand_name": "Hijacked"}, headers=editor["headers"])
    assert resp.status_code == 403


def test_non_member_cannot_view_branding(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    stranger = _register_and_login(client, "stranger")
    resp = client.get(f"/api/v1/organizations/{org_id}/branding", headers=stranger["headers"])
    assert resp.status_code == 403
