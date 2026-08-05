"""
Connector Framework & Webhook Platform router — exercised through real HTTP
endpoints. The one real HTTP call (a connector "test") is monkeypatched at
the shared router-module engine instance, same pattern
test_funding_intelligence_api.py established for Grants.gov sync.
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


def test_list_connector_types(client, registered_user):
    resp = client.get("/api/v1/connectors/types", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    types = {t["connector_type"]: t for t in resp.json()}
    assert types["webhook"]["functional"] is True
    assert types["salesforce"]["functional"] is False


def test_owner_can_create_and_editor_cannot(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    editor = _register_and_login(client, "editor")
    _invite_member(client, org_id, registered_user["headers"], editor["email"], "editor")

    resp = client.post(
        "/api/v1/connectors", params={"org_id": org_id},
        json={"connector_type": "webhook", "name": "My Webhook", "config": {"target_url": "https://example.com/hook"}},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    connector_id = resp.json()["id"]

    resp = client.post(
        "/api/v1/connectors", params={"org_id": org_id},
        json={"connector_type": "slack", "name": "Editor's Slack", "config": {"webhook_url": "https://hooks.slack.com/x"}},
        headers=editor["headers"],
    )
    assert resp.status_code == 403

    # But the editor can still view connectors (member-level read access).
    resp = client.get("/api/v1/connectors", params={"org_id": org_id}, headers=editor["headers"])
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["id"] == connector_id


def test_test_connector_endpoint_uses_mocked_http(client, registered_user, monkeypatch):
    import routers.connectors as connectors_router

    async def fake_post(url, **kwargs):
        class _Resp:
            status_code = 200
        return _Resp()
    monkeypatch.setattr(connectors_router.engine, "_post", fake_post)

    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/connectors", params={"org_id": org_id},
        json={"connector_type": "webhook", "name": "Hook", "config": {"target_url": "https://example.com/hook"}},
        headers=registered_user["headers"],
    )
    connector_id = resp.json()["id"]

    resp = client.post(f"/api/v1/connectors/{connector_id}/test", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True

    resp = client.get(f"/api/v1/connectors/{connector_id}/events", headers=registered_user["headers"])
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["event_type"] == "test"


def test_delete_connector(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/connectors", params={"org_id": org_id},
        json={"connector_type": "webhook", "name": "Hook", "config": {"target_url": "https://example.com/hook"}},
        headers=registered_user["headers"],
    )
    connector_id = resp.json()["id"]

    resp = client.delete(f"/api/v1/connectors/{connector_id}", headers=registered_user["headers"])
    assert resp.status_code == 200

    resp = client.get("/api/v1/connectors", params={"org_id": org_id}, headers=registered_user["headers"])
    assert resp.json() == []
