"""
Engine 13 — Collaboration Engine.

Covers the deterministic, DB-only surface: workspace hierarchy (departments/
teams), guest invites, workspace tasks, threaded comments with @mention
parsing, notifications, and approval request decisions. Nothing here makes
an OpenAI call (this engine doesn't have any AI-generation methods), so the
whole engine is in scope for this suite.

Like test_credit_engine.py and test_scope_of_work_engine.py, these methods
are async and need a real DB session, so each test wraps its body in
asyncio.run() rather than pulling in pytest-asyncio for just this file. The
`client` fixture is included (unused directly, except where a real User row
is needed for email-lookup paths) in every test purely to trigger the app's
lifespan startup, which is what creates the Phase 3 tables in the test
database. Most org_id/proposal_id/user_id values are fake strings without a
backing row — SQLite's FK enforcement is off by default in this app, same
accepted pattern as the other engine-level test files.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from database import AsyncSessionLocal
from engines.collaboration_engine import CollaborationEngine


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def engine():
    return CollaborationEngine()


def _id(prefix: str) -> str:
    return f"test-{prefix}-{uuid.uuid4().hex[:12]}"


def _register(client, label: str) -> dict:
    email = f"{label}-{uuid.uuid4().hex[:10]}@example.com"
    resp = client.post("/api/v1/auth/register", json={
        "email": email, "password": "TestPassword123!",
        "full_name": f"{label.title()} User", "organization": "Test Org",
    })
    assert resp.status_code == 201, resp.text
    return {"id": resp.json()["id"], "email": email}


# ── Departments & Teams ──────────────────────────────────────────────────────

def test_create_and_list_departments(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            dept = await engine.create_department(db, org_id, "user-1", "Research")
            await db.commit()
        async with AsyncSessionLocal() as db:
            depts = await engine.list_departments(db, org_id)
            return dept, depts

    dept, depts = _run(_body())
    assert dept.name == "Research"
    assert [d.name for d in depts] == ["Research"]


def test_delete_department_removes_it(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            dept = await engine.create_department(db, org_id, "user-1", "Ops")
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.delete_department(db, dept.id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.list_departments(db, org_id)

    depts = _run(_body())
    assert depts == []


def test_create_team_with_invalid_department_raises_404(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.create_team(db, org_id, "user-1", "Alpha", department_id="does-not-exist")
            return exc_info.value.status_code

    assert _run(_body()) == 404


def test_add_team_member_and_prevent_duplicates(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            team = await engine.create_team(db, org_id, "user-1", "Alpha")
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.add_team_member(db, team.id, "user-2")
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.add_team_member(db, team.id, "user-2")
            dup_status = exc_info.value.status_code
        async with AsyncSessionLocal() as db:
            members = await engine.list_team_members(db, team.id)
            return dup_status, members

    dup_status, members = _run(_body())
    assert dup_status == 409
    assert len(members) == 1


def test_remove_team_member(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            team = await engine.create_team(db, org_id, "user-1", "Alpha")
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.add_team_member(db, team.id, "user-2")
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.remove_team_member(db, team.id, "user-2")
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.list_team_members(db, team.id)

    assert _run(_body()) == []


# ── Guest Access ─────────────────────────────────────────────────────────────

def test_invite_guest_requires_existing_account(client, engine):
    org_id = _id("org")
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.invite_guest(db, org_id, proposal_id, "nobody@example.com", "user-1")
            return exc_info.value.status_code

    assert _run(_body()) == 404


def test_invite_guest_creates_access_and_notification(client, engine):
    org_id = _id("org")
    proposal_id = _id("proposal")
    guest = _register(client, "guest")

    async def _body():
        async with AsyncSessionLocal() as db:
            access, target = await engine.invite_guest(db, org_id, proposal_id, guest["email"], "owner-1")
            await db.commit()
        async with AsyncSessionLocal() as db:
            notes = await engine.list_notifications(db, target.id)
            return access, target, notes

    access, target, notes = _run(_body())
    assert access.user_id == target.id
    assert access.can_comment is True
    assert any(n.type == "guest_invited" for n in notes)


def test_invite_guest_twice_is_rejected(client, engine):
    org_id = _id("org")
    proposal_id = _id("proposal")
    guest = _register(client, "guest2")

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.invite_guest(db, org_id, proposal_id, guest["email"], "owner-1")
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.invite_guest(db, org_id, proposal_id, guest["email"], "owner-1")
            return exc_info.value.status_code

    assert _run(_body()) == 409


def test_revoke_guest_access(client, engine):
    org_id = _id("org")
    proposal_id = _id("proposal")
    guest = _register(client, "guest3")

    async def _body():
        async with AsyncSessionLocal() as db:
            access, _ = await engine.invite_guest(db, org_id, proposal_id, guest["email"], "owner-1")
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.revoke_guest_access(db, access.id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.list_guest_access(db, proposal_id)

    assert _run(_body()) == []


# ── Workspace Tasks ──────────────────────────────────────────────────────────

def test_create_task_notifies_assignee(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            task = await engine.create_task(db, org_id, "creator-1", {
                "title": "Draft the budget narrative", "assignee_id": "assignee-1",
            })
            await db.commit()
        async with AsyncSessionLocal() as db:
            notes = await engine.list_notifications(db, "assignee-1")
            return task, notes

    task, notes = _run(_body())
    assert task.status == "open"
    assert any(n.type == "task_assigned" for n in notes)


def test_create_task_no_self_notification_when_creator_is_assignee(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_task(db, org_id, "solo-1", {"title": "Self task", "assignee_id": "solo-1"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.list_notifications(db, "solo-1")

    notes = _run(_body())
    assert notes == []


def test_update_task_reassignment_notifies_new_assignee(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            task = await engine.create_task(db, org_id, "creator-1", {"title": "Task"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            updated = await engine.update_task(db, task.id, {"assignee_id": "new-assignee", "status": "in_progress"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            notes = await engine.list_notifications(db, "new-assignee")
            return updated, notes

    updated, notes = _run(_body())
    assert updated.status == "in_progress"
    assert any(n.type == "task_assigned" for n in notes)


def test_list_tasks_filters_by_proposal_and_assignee(client, engine):
    org_id = _id("org")
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_task(db, org_id, "creator-1", {"title": "In scope", "proposal_id": proposal_id, "assignee_id": "a1"})
            await engine.create_task(db, org_id, "creator-1", {"title": "Other proposal", "assignee_id": "a1"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            scoped = await engine.list_tasks(db, org_id, proposal_id=proposal_id)
            return scoped

    scoped = _run(_body())
    assert [t.title for t in scoped] == ["In scope"]


def test_delete_task(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            task = await engine.create_task(db, org_id, "creator-1", {"title": "Temp"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.delete_task(db, task.id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.list_tasks(db, org_id)

    assert _run(_body()) == []


# ── Comments ─────────────────────────────────────────────────────────────────

def test_create_comment_parses_mentions(client, engine):
    org_id = _id("org")
    proposal_id = _id("prop")
    mentioned = _register(client, "mentioned")

    async def _body():
        async with AsyncSessionLocal() as db:
            comment = await engine.create_comment(
                db, org_id, "proposal", proposal_id, "author-1",
                f"Great work @{mentioned['email']}, can you review this?",
            )
            await db.commit()
        async with AsyncSessionLocal() as db:
            notes = await engine.list_notifications(db, mentioned["id"])
            return comment, notes

    comment, notes = _run(_body())
    assert comment.mentions == [mentioned["id"]]
    assert any(n.type == "mention" for n in notes)


def test_create_comment_without_mentions(client, engine):
    org_id = _id("org")
    proposal_id = _id("prop")

    async def _body():
        async with AsyncSessionLocal() as db:
            comment = await engine.create_comment(db, org_id, "proposal", proposal_id, "author-1", "No mentions here.")
            await db.commit()
            return comment

    comment = _run(_body())
    assert comment.mentions == []


def test_update_comment_sets_edited_flag(client, engine):
    org_id = _id("org")
    proposal_id = _id("prop")

    async def _body():
        async with AsyncSessionLocal() as db:
            comment = await engine.create_comment(db, org_id, "proposal", proposal_id, "author-1", "Original")
            await db.commit()
        async with AsyncSessionLocal() as db:
            updated = await engine.update_comment(db, comment.id, "Edited content")
            await db.commit()
            return updated

    updated = _run(_body())
    assert updated.content == "Edited content"
    assert updated.edited is True


def test_delete_comment(client, engine):
    org_id = _id("org")
    proposal_id = _id("prop")

    async def _body():
        async with AsyncSessionLocal() as db:
            comment = await engine.create_comment(db, org_id, "proposal", proposal_id, "author-1", "Bye")
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.delete_comment(db, comment.id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.list_comments(db, "proposal", proposal_id)

    assert _run(_body()) == []


def test_list_comments_ordered_by_creation(client, engine):
    org_id = _id("org")
    proposal_id = _id("prop")

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_comment(db, org_id, "proposal", proposal_id, "author-1", "First")
            await engine.create_comment(db, org_id, "proposal", proposal_id, "author-1", "Second")
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.list_comments(db, "proposal", proposal_id)

    comments = _run(_body())
    assert [c.content for c in comments] == ["First", "Second"]


# ── Notifications ────────────────────────────────────────────────────────────

def test_mark_notification_read_requires_ownership(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            n = await engine.notify(db, "owner-1", "info", "hello")
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.mark_notification_read(db, n.id, "someone-else")
            wrong_owner_status = exc_info.value.status_code
        async with AsyncSessionLocal() as db:
            marked = await engine.mark_notification_read(db, n.id, "owner-1")
            return wrong_owner_status, marked

    wrong_owner_status, marked = _run(_body())
    assert wrong_owner_status == 404
    assert marked.read is True


def test_list_notifications_unread_only_filter(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            n1 = await engine.notify(db, "owner-2", "info", "one")
            await engine.notify(db, "owner-2", "info", "two")
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.mark_notification_read(db, n1.id, "owner-2")
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.list_notifications(db, "owner-2", unread_only=True)

    unread = _run(_body())
    assert len(unread) == 1
    assert unread[0].message == "two"


# ── Approval Requests ────────────────────────────────────────────────────────

def test_create_approval_request_notifies_approver(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            req = await engine.create_approval_request(db, org_id, "document", "doc-1", "requester-1", approver_id="approver-1")
            await db.commit()
        async with AsyncSessionLocal() as db:
            notes = await engine.list_notifications(db, "approver-1")
            return req, notes

    req, notes = _run(_body())
    assert req.status == "pending"
    assert any(n.type == "approval_requested" for n in notes)


def test_decide_approval_request_approves_and_notifies_requester(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            req = await engine.create_approval_request(db, org_id, "document", "doc-2", "requester-2", approver_id="approver-2")
            await db.commit()
        async with AsyncSessionLocal() as db:
            decided = await engine.decide_approval_request(db, req.id, "approver-2", True, decision_notes="Looks good")
            await db.commit()
        async with AsyncSessionLocal() as db:
            notes = await engine.list_notifications(db, "requester-2")
            return decided, notes

    decided, notes = _run(_body())
    assert decided.status == "approved"
    assert decided.decision_notes == "Looks good"
    assert any(n.type == "approval_decided" for n in notes)


def test_decide_approval_request_twice_raises_400(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            req = await engine.create_approval_request(db, org_id, "document", "doc-3", "requester-3")
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.decide_approval_request(db, req.id, "someone", False)
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.decide_approval_request(db, req.id, "someone", True)
            return exc_info.value.status_code

    assert _run(_body()) == 400


def test_list_approval_requests_filters_by_status(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            r1 = await engine.create_approval_request(db, org_id, "document", "doc-4", "requester-4")
            await engine.create_approval_request(db, org_id, "document", "doc-5", "requester-4")
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.decide_approval_request(db, r1.id, "someone", True)
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.list_approval_requests(db, org_id, status="pending")

    pending = _run(_body())
    assert len(pending) == 1
