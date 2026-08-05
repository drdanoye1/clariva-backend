"""
Collaboration router (Phase 3, PRD §11) — exercised through the real HTTP
endpoints: workspace hierarchy (departments/teams), scoped guest access,
threaded comments, workspace tasks, approval requests, notifications, and
the per-proposal activity feed.
"""
from __future__ import annotations

import uuid


def _org_context() -> dict:
    return {"organization_name": "Acme Research", "industry": "Biotech"}


def _proposal_payload(**overrides) -> dict:
    payload = {
        "title": "Novel Gene Therapy Platform",
        "agency": "NSF",
        "phase": "phase_i",
        "grant_type": "sbir",
        "org_context": _org_context(),
        "research_focus": "CRISPR-based delivery mechanisms",
        "innovation_description": "A new lipid nanoparticle delivery system",
    }
    payload.update(overrides)
    return payload


def _create_proposal(client, headers: dict) -> str:
    resp = client.post("/api/v1/proposals/", json=_proposal_payload(), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["proposal_id"]


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


def _share_proposal(client, org_id: str, proposal_id: str, owner_headers: dict) -> None:
    resp = client.post(f"/api/v1/organizations/{org_id}/proposals", json={"proposal_id": proposal_id}, headers=owner_headers)
    assert resp.status_code == 200, resp.text


# ── Departments & Teams ──────────────────────────────────────────────────────

def test_owner_can_create_department_editor_cannot(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    editor = _register_and_login(client, "editor")
    _invite_member(client, org_id, registered_user["headers"], editor["email"], "editor")

    resp = client.post(f"/api/v1/organizations/{org_id}/departments", json={"name": "Research"}, headers=registered_user["headers"])
    assert resp.status_code == 201, resp.text

    resp = client.post(f"/api/v1/organizations/{org_id}/departments", json={"name": "Nope"}, headers=editor["headers"])
    assert resp.status_code == 403


def test_create_team_under_department_and_manage_membership(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    dept = client.post(f"/api/v1/organizations/{org_id}/departments", json={"name": "Research"}, headers=registered_user["headers"]).json()

    resp = client.post(
        f"/api/v1/organizations/{org_id}/teams",
        json={"name": "Genomics Team", "department_id": dept["id"]}, headers=registered_user["headers"],
    )
    assert resp.status_code == 201, resp.text
    team_id = resp.json()["id"]

    member = _register_and_login(client, "teammate")
    _invite_member(client, org_id, registered_user["headers"], member["email"], "viewer")

    resp = client.post(f"/api/v1/teams/{team_id}/members",
                        params={"user_id": member["user_id"]}, headers=registered_user["headers"])
    assert resp.status_code == 201, resp.text

    members = client.get(f"/api/v1/teams/{team_id}/members", headers=registered_user["headers"]).json()
    assert any(m["user_id"] == member["user_id"] for m in members)

    resp = client.delete(f"/api/v1/teams/{team_id}/members/{member['user_id']}", headers=registered_user["headers"])
    assert resp.status_code == 204


def test_adding_non_org_member_to_team_fails(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    team_id = client.post(f"/api/v1/organizations/{org_id}/teams", json={"name": "Alpha"}, headers=registered_user["headers"]).json()["id"]
    outsider = _register_and_login(client, "outsider")

    resp = client.post(f"/api/v1/teams/{team_id}/members", params={"user_id": outsider["user_id"]}, headers=registered_user["headers"])
    assert resp.status_code == 403


# ── Guest Access ─────────────────────────────────────────────────────────────

def test_guest_invite_requires_proposal_shared_to_org(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    guest = _register_and_login(client, "guest")

    resp = client.post(
        f"/api/v1/organizations/{org_id}/proposals/{proposal_id}/guests",
        json={"email": guest["email"]}, headers=registered_user["headers"],
    )
    assert resp.status_code == 400


def test_guest_invite_grants_comment_access_to_proposal(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    _share_proposal(client, org_id, proposal_id, registered_user["headers"])
    guest = _register_and_login(client, "guest")

    resp = client.post(
        f"/api/v1/organizations/{org_id}/proposals/{proposal_id}/guests",
        json={"email": guest["email"]}, headers=registered_user["headers"],
    )
    assert resp.status_code == 201, resp.text

    # The guest now has comment access via workspace_access.py, despite not
    # being an OrgMembership row and despite not owning the proposal.
    resp = client.post(f"/api/v1/proposals/{proposal_id}/comments", json={"content": "Looks great!"}, headers=guest["headers"])
    assert resp.status_code == 201, resp.text

    guests = client.get(f"/api/v1/organizations/{org_id}/proposals/{proposal_id}/guests", headers=registered_user["headers"]).json()
    assert len(guests) == 1
    guest_access_id = guests[0]["id"]

    resp = client.delete(f"/api/v1/guests/{guest_access_id}", headers=registered_user["headers"])
    assert resp.status_code == 204

    resp = client.post(f"/api/v1/proposals/{proposal_id}/comments", json={"content": "Should fail now"}, headers=guest["headers"])
    assert resp.status_code == 403


def test_stranger_has_no_access_to_proposal_comments(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    stranger = _register_and_login(client, "stranger")
    resp = client.get(f"/api/v1/proposals/{proposal_id}/comments", headers=stranger["headers"])
    assert resp.status_code == 403


# ── Comments ─────────────────────────────────────────────────────────────────

def test_comment_crud_author_only_edit_and_delete(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    resp = client.post(f"/api/v1/proposals/{proposal_id}/comments", json={"content": "Initial comment"}, headers=registered_user["headers"])
    assert resp.status_code == 201, resp.text
    comment_id = resp.json()["id"]
    assert resp.json()["author_name"]

    other = _register_and_login(client, "othercommenter")
    # Not shared/accessible -> cannot even view, let alone edit
    resp = client.patch(f"/api/v1/comments/{comment_id}", json={"content": "Hijacked"}, headers=other["headers"])
    assert resp.status_code == 403

    resp = client.patch(f"/api/v1/comments/{comment_id}", json={"content": "Updated by author"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["edited"] is True
    assert resp.json()["content"] == "Updated by author"

    resp = client.delete(f"/api/v1/comments/{comment_id}", headers=registered_user["headers"])
    assert resp.status_code == 204

    remaining = client.get(f"/api/v1/proposals/{proposal_id}/comments", headers=registered_user["headers"]).json()
    assert remaining == []


# ── Workspace Tasks ──────────────────────────────────────────────────────────

def test_create_and_list_tasks_on_a_proposal(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/tasks",
        json={"title": "Draft the abstract", "assignee_id": registered_user["user_id"]},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]

    tasks = client.get(f"/api/v1/proposals/{proposal_id}/tasks", headers=registered_user["headers"]).json()
    assert len(tasks) == 1

    mine = client.get("/api/v1/tasks/mine", headers=registered_user["headers"]).json()
    assert any(t["id"] == task_id for t in mine)

    resp = client.patch(f"/api/v1/tasks/{task_id}", json={"status": "done"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "done"

    resp = client.delete(f"/api/v1/tasks/{task_id}", headers=registered_user["headers"])
    assert resp.status_code == 204


def test_task_creation_requires_edit_access(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    stranger = _register_and_login(client, "taskstranger")
    resp = client.post(f"/api/v1/proposals/{proposal_id}/tasks", json={"title": "Sneaky task"}, headers=stranger["headers"])
    assert resp.status_code == 403


# ── Approval Requests ────────────────────────────────────────────────────────

def test_approval_request_decided_by_designated_approver(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    _share_proposal(client, org_id, proposal_id, registered_user["headers"])
    approver = _register_and_login(client, "approver")
    _invite_member(client, org_id, registered_user["headers"], approver["email"], "viewer")

    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/approvals",
        json={"object_type": "proposal", "object_id": proposal_id, "approver_id": approver["user_id"]},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 201, resp.text
    approval_id = resp.json()["id"]

    outsider = _register_and_login(client, "notapprover")
    resp = client.post(f"/api/v1/approvals/{approval_id}/decide", json={"approved": True}, headers=outsider["headers"])
    assert resp.status_code == 403

    resp = client.post(f"/api/v1/approvals/{approval_id}/decide", json={"approved": True, "decision_notes": "Approved."}, headers=approver["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"

    listed = client.get(f"/api/v1/proposals/{proposal_id}/approvals", headers=registered_user["headers"]).json()
    assert any(a["id"] == approval_id for a in listed)


def test_approval_list_is_empty_for_personal_unshared_proposal(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    resp = client.get(f"/api/v1/proposals/{proposal_id}/approvals", headers=registered_user["headers"])
    assert resp.status_code == 200
    assert resp.json() == []


# ── Notifications & Activity Feed ────────────────────────────────────────────

def test_notifications_list_and_mark_read(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    _share_proposal(client, org_id, proposal_id, registered_user["headers"])
    guest = _register_and_login(client, "notifguest")
    client.post(f"/api/v1/organizations/{org_id}/proposals/{proposal_id}/guests", json={"email": guest["email"]}, headers=registered_user["headers"])

    notes = client.get("/api/v1/notifications", headers=guest["headers"]).json()
    assert len(notes) >= 1
    note_id = notes[0]["id"]

    resp = client.post(f"/api/v1/notifications/{note_id}/read", headers=guest["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["read"] is True

    unread = client.get("/api/v1/notifications", params={"unread_only": True}, headers=guest["headers"]).json()
    assert all(n["id"] != note_id for n in unread)


def test_activity_feed_shows_comment_and_task_actions(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    client.post(f"/api/v1/proposals/{proposal_id}/comments", json={"content": "Hello"}, headers=registered_user["headers"])
    client.post(f"/api/v1/proposals/{proposal_id}/tasks", json={"title": "Do the thing"}, headers=registered_user["headers"])

    resp = client.get(f"/api/v1/proposals/{proposal_id}/activity", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    actions = [e["action"] for e in resp.json()]
    assert "comment.created" in actions
    assert "task.created" in actions


def test_activity_feed_requires_view_access(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    stranger = _register_and_login(client, "activitystranger")
    resp = client.get(f"/api/v1/proposals/{proposal_id}/activity", headers=stranger["headers"])
    assert resp.status_code == 403
