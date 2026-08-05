"""
Marketplace router — listing groundwork only (see
engines/marketplace_engine.py's docstring), exercised through real HTTP
endpoints.
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


def test_create_listing_starts_as_draft_and_is_not_browsable_until_published(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/marketplace",
        json={"listing_type": "template_pack", "name": "NSF SBIR Template Pack", "vendor_org_id": org_id, "price_cents": 4900},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    listing_id = resp.json()["id"]
    assert resp.json()["status"] == "draft"

    resp = client.get("/api/v1/marketplace", headers=registered_user["headers"])
    assert resp.status_code == 200
    assert not any(l["id"] == listing_id for l in resp.json())

    resp = client.patch(f"/api/v1/marketplace/{listing_id}", json={"status": "published"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text

    resp = client.get("/api/v1/marketplace", headers=registered_user["headers"])
    assert any(l["id"] == listing_id for l in resp.json())


def test_editor_cannot_create_listing_for_org(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    editor = _register_and_login(client, "editor")
    _invite_member(client, org_id, registered_user["headers"], editor["email"], "editor")

    resp = client.post(
        "/api/v1/marketplace",
        json={"listing_type": "connector", "name": "Sneaky Connector", "vendor_org_id": org_id},
        headers=editor["headers"],
    )
    assert resp.status_code == 403


def test_platform_listing_cannot_be_edited_via_org_endpoint(client, registered_user):
    resp = client.post(
        "/api/v1/marketplace",
        json={"listing_type": "ai_capability", "name": "Premium Scoring", "vendor_org_id": None},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    listing_id = resp.json()["id"]

    resp = client.patch(f"/api/v1/marketplace/{listing_id}", json={"status": "published"}, headers=registered_user["headers"])
    assert resp.status_code == 403


def test_list_mine_requires_permission(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    client.post(
        "/api/v1/marketplace",
        json={"listing_type": "template_pack", "name": "Pack", "vendor_org_id": org_id},
        headers=registered_user["headers"],
    )
    resp = client.get("/api/v1/marketplace/mine", params={"org_id": org_id}, headers=registered_user["headers"])
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    stranger = _register_and_login(client, "stranger")
    resp = client.get("/api/v1/marketplace/mine", params={"org_id": org_id}, headers=stranger["headers"])
    assert resp.status_code == 403
