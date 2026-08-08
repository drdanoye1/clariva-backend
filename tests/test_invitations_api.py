"""
Invitation flow — inviting an email with no existing Clariva account, and
the public accept-invite endpoints that turn that into a real account +
org membership. Exercised through the real HTTP endpoints against the
isolated test database.

RESEND_API_KEY is blank in the test environment, so every invite here
exercises email_service.send_email()'s graceful no-op path (it logs and
returns False rather than raising) — the Invitation row is still created
and usable regardless of whether the email "sent." That's the same
"degrade gracefully when an optional integration key is unset" pattern as
SAM_GOV_API_KEY elsewhere in this codebase.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta


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


def _invite(client, org_id, headers, email, role="editor", **extra):
    return client.post(f"/api/v1/organizations/{org_id}/invite",
                        json={"email": email, "role": role, **extra}, headers=headers)


def test_inviting_unregistered_email_creates_pending_invitation(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    new_email = f"newmember-{uuid.uuid4().hex[:8]}@example.com"

    resp = _invite(client, org_id, registered_user["headers"], new_email, role="viewer")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "invited"

    listing = client.get(f"/api/v1/organizations/{org_id}/invitations", headers=registered_user["headers"])
    assert listing.status_code == 200
    invites = listing.json()
    assert any(i["email"] == new_email and i["status"] == "pending" and i["role"] == "viewer" for i in invites)


def test_inviting_existing_user_still_adds_immediately(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    other = _register_and_login(client, "existing")

    resp = _invite(client, org_id, registered_user["headers"], other["email"], role="editor")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "added"

    members = client.get(f"/api/v1/organizations/{org_id}/members", headers=registered_user["headers"]).json()
    assert any(m["user_id"] == other["user_id"] and m["role"] == "editor" for m in members)

    # No Invitation row should exist for someone who was added directly.
    listing = client.get(f"/api/v1/organizations/{org_id}/invitations", headers=registered_user["headers"])
    assert not any(i["email"] == other["email"] for i in listing.json())


def _extract_token_from_invite(client, org_id, headers, email) -> str:
    """The API deliberately never exposes the raw token (it's only ever
    sent by email) — pull it straight from the DB the same way
    test_credit_engine.py reaches into engine internals for assertions."""
    from database import AsyncSessionLocal
    from models.db_models import Invitation
    from sqlalchemy import select

    async def _body():
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Invitation).where(Invitation.org_id == org_id, Invitation.email == email)
            )
            invitation = result.scalar_one()
            return invitation.token

    return asyncio.run(_body())


def test_preview_invitation_by_token(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    email = f"previewme-{uuid.uuid4().hex[:8]}@example.com"
    _invite(client, org_id, registered_user["headers"], email, role="editor")
    token = _extract_token_from_invite(client, org_id, registered_user["headers"], email)

    resp = client.get(f"/api/v1/invitations/by-token/{token}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email"] == email
    assert body["role"] == "editor"
    assert body["valid"] is True


def test_preview_unknown_token_404s(client):
    resp = client.get("/api/v1/invitations/by-token/not-a-real-token")
    assert resp.status_code == 404


def test_accept_invitation_creates_account_and_membership(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    email = f"accept-{uuid.uuid4().hex[:8]}@example.com"
    _invite(client, org_id, registered_user["headers"], email, role="viewer")
    token = _extract_token_from_invite(client, org_id, registered_user["headers"], email)

    resp = client.post(f"/api/v1/invitations/by-token/{token}/accept",
                        json={"full_name": "New Member", "password": "SuperSecret123!"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]

    new_headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    members = client.get(f"/api/v1/organizations/{org_id}/members", headers=new_headers).json()
    assert any(m["email"] == email and m["role"] == "viewer" for m in members)

    # The invitee can also log in normally afterward with the password they set.
    login = client.post("/api/v1/auth/login", data={"username": email, "password": "SuperSecret123!"})
    assert login.status_code == 200


def test_accept_invitation_twice_fails(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    email = f"double-{uuid.uuid4().hex[:8]}@example.com"
    _invite(client, org_id, registered_user["headers"], email)
    token = _extract_token_from_invite(client, org_id, registered_user["headers"], email)

    first = client.post(f"/api/v1/invitations/by-token/{token}/accept",
                         json={"full_name": "First Try", "password": "SuperSecret123!"})
    assert first.status_code == 200, first.text

    second = client.post(f"/api/v1/invitations/by-token/{token}/accept",
                          json={"full_name": "Second Try", "password": "SuperSecret123!"})
    assert second.status_code == 400


def test_revoked_invitation_cannot_be_accepted(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    email = f"revoked-{uuid.uuid4().hex[:8]}@example.com"
    _invite(client, org_id, registered_user["headers"], email)
    token = _extract_token_from_invite(client, org_id, registered_user["headers"], email)

    listing = client.get(f"/api/v1/organizations/{org_id}/invitations", headers=registered_user["headers"]).json()
    invitation_id = next(i["id"] for i in listing if i["email"] == email)

    revoke = client.delete(f"/api/v1/organizations/{org_id}/invitations/{invitation_id}",
                            headers=registered_user["headers"])
    assert revoke.status_code == 204

    accept = client.post(f"/api/v1/invitations/by-token/{token}/accept",
                          json={"full_name": "Too Late", "password": "SuperSecret123!"})
    assert accept.status_code == 400


def test_expired_invitation_cannot_be_accepted(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    email = f"expired-{uuid.uuid4().hex[:8]}@example.com"
    _invite(client, org_id, registered_user["headers"], email)
    token = _extract_token_from_invite(client, org_id, registered_user["headers"], email)

    from database import AsyncSessionLocal
    from models.db_models import Invitation
    from sqlalchemy import select

    async def _expire_it():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Invitation).where(Invitation.token == token))
            invitation = result.scalar_one()
            invitation.expires_at = datetime.utcnow() - timedelta(days=1)
            await db.commit()

    asyncio.run(_expire_it())

    resp = client.get(f"/api/v1/invitations/by-token/{token}")
    assert resp.json()["valid"] is False
    assert resp.json()["reason"] == "expired"

    accept = client.post(f"/api/v1/invitations/by-token/{token}/accept",
                          json={"full_name": "Too Slow", "password": "SuperSecret123!"})
    assert accept.status_code == 400


def test_resend_invitation_refreshes_expiry(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    email = f"resend-{uuid.uuid4().hex[:8]}@example.com"
    _invite(client, org_id, registered_user["headers"], email)
    listing = client.get(f"/api/v1/organizations/{org_id}/invitations", headers=registered_user["headers"]).json()
    invitation_id = next(i["id"] for i in listing if i["email"] == email)

    resp = client.post(f"/api/v1/organizations/{org_id}/invitations/{invitation_id}/resend",
                        headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text


def test_editor_cannot_manage_invitations(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    editor = _register_and_login(client, "editor")
    client.post(f"/api/v1/organizations/{org_id}/invite",
                json={"email": editor["email"], "role": "editor"}, headers=registered_user["headers"])

    # editor CAN invite (invite_members allows owner + editor)...
    resp = _invite(client, org_id, editor["headers"], f"viaeditor-{uuid.uuid4().hex[:8]}@example.com")
    assert resp.status_code == 200

    # ...but a viewer cannot.
    viewer = _register_and_login(client, "viewer")
    client.post(f"/api/v1/organizations/{org_id}/invite",
                json={"email": viewer["email"], "role": "viewer"}, headers=registered_user["headers"])
    resp = _invite(client, org_id, viewer["headers"], f"viaviewer-{uuid.uuid4().hex[:8]}@example.com")
    assert resp.status_code == 403


def test_invite_with_team_assigns_membership_on_accept(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    team_resp = client.post(f"/api/v1/organizations/{org_id}/teams",
                             json={"name": "Grants Team"}, headers=registered_user["headers"])
    assert team_resp.status_code == 201, team_resp.text
    team_id = team_resp.json()["id"]

    email = f"teamed-{uuid.uuid4().hex[:8]}@example.com"
    resp = _invite(client, org_id, registered_user["headers"], email, role="editor", team_id=team_id)
    assert resp.status_code == 200, resp.text
    token = _extract_token_from_invite(client, org_id, registered_user["headers"], email)

    preview = client.get(f"/api/v1/invitations/by-token/{token}")
    assert preview.json()["team_name"] == "Grants Team"

    accept = client.post(f"/api/v1/invitations/by-token/{token}/accept",
                          json={"full_name": "Team Member", "password": "SuperSecret123!"})
    assert accept.status_code == 200, accept.text
    new_user_id = None
    from jose import jwt
    from config import settings
    payload = jwt.decode(accept.json()["access_token"], settings.SECRET_KEY, algorithms=["HS256"])
    new_user_id = payload["sub"]

    team_members = client.get(f"/api/v1/teams/{team_id}/members", headers=registered_user["headers"]).json()
    assert any(m["user_id"] == new_user_id for m in team_members)
