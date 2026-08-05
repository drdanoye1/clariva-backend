"""AI Credits router — exercised through the real HTTP endpoints."""
from __future__ import annotations

import uuid

from engines.credit_engine import DEFAULT_STARTING_BALANCE


def _register_and_login(client, label: str) -> dict:
    email = f"{label}-{uuid.uuid4().hex[:10]}@example.com"
    password = "TestPassword123!"
    resp = client.post("/api/v1/auth/register", json={
        "email": email, "password": password,
        "full_name": f"{label.title()} User", "organization": "Test Org",
    })
    assert resp.status_code == 201, resp.text
    login = client.post("/api/v1/auth/login", data={"username": email, "password": password})
    token = login.json()["access_token"]
    return {"email": email, "user_id": resp.json()["id"], "headers": {"Authorization": f"Bearer {token}"}}


def _create_org(client, owner_headers: dict) -> str:
    resp = client.post("/api/v1/organizations/", json={"name": f"Org {uuid.uuid4().hex[:8]}"}, headers=owner_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_new_org_starts_with_default_balance(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.get(f"/api/v1/organizations/{org_id}/credits", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["balance"] == DEFAULT_STARTING_BALANCE


def test_non_member_cannot_view_balance(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    outsider = _register_and_login(client, "outsider")
    resp = client.get(f"/api/v1/organizations/{org_id}/credits", headers=outsider["headers"])
    assert resp.status_code == 403


def test_owner_can_topup_and_balance_increases(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(f"/api/v1/organizations/{org_id}/credits/topup",
                        json={"amount": 50.0, "reason": "plan_allotment"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["balance"] == DEFAULT_STARTING_BALANCE + 50.0


def test_viewer_cannot_topup(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    viewer = _register_and_login(client, "viewer")
    client.post(f"/api/v1/organizations/{org_id}/invite",
                json={"email": viewer["email"], "role": "viewer"}, headers=registered_user["headers"])

    resp = client.post(f"/api/v1/organizations/{org_id}/credits/topup",
                        json={"amount": 10.0}, headers=viewer["headers"])
    assert resp.status_code == 403


def test_viewer_can_still_view_balance_and_transactions(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    viewer = _register_and_login(client, "viewer2")
    client.post(f"/api/v1/organizations/{org_id}/invite",
                json={"email": viewer["email"], "role": "viewer"}, headers=registered_user["headers"])
    client.post(f"/api/v1/organizations/{org_id}/credits/topup",
                json={"amount": 25.0, "reason": "test"}, headers=registered_user["headers"])

    balance_resp = client.get(f"/api/v1/organizations/{org_id}/credits", headers=viewer["headers"])
    assert balance_resp.status_code == 200
    assert balance_resp.json()["balance"] == DEFAULT_STARTING_BALANCE + 25.0

    txns_resp = client.get(f"/api/v1/organizations/{org_id}/credits/transactions", headers=viewer["headers"])
    assert txns_resp.status_code == 200
    assert any(t["reason"] == "test" for t in txns_resp.json())


def test_owner_can_set_and_list_allocation_cap(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(f"/api/v1/organizations/{org_id}/credits/allocations",
                        json={"cap": 10.0, "period": "monthly"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["cap"] == 10.0

    listing = client.get(f"/api/v1/organizations/{org_id}/credits/allocations", headers=registered_user["headers"])
    assert listing.status_code == 200
    assert any(a["cap"] == 10.0 for a in listing.json())


def test_editor_cannot_manage_allocations(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    editor = _register_and_login(client, "editor")
    client.post(f"/api/v1/organizations/{org_id}/invite",
                json={"email": editor["email"], "role": "editor"}, headers=registered_user["headers"])

    resp = client.post(f"/api/v1/organizations/{org_id}/credits/allocations",
                        json={"cap": 5.0}, headers=editor["headers"])
    assert resp.status_code == 403
