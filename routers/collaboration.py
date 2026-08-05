"""
Collaboration router — workspace hierarchy (departments/teams), external
guest access, threaded comments, task assignment, approval requests,
notifications, and a per-proposal activity feed (Clariva Enterprise™ PRD
§11). Mounted at /api/v1 (each route path is already fully qualified,
e.g. /api/v1/organizations/{org_id}/departments, /api/v1/proposals/{id}/comments).

Access model (see docs/ARCHITECTURE.md §8 and workspace_access.py):
- Department/Team management is org-level, gated by rbac.py's
  "manage_workspace" permission (owner-only) via _assert_permission,
  reused directly from routers/organizations.py exactly like credits.py
  and proposals.py already do.
- Comments/Tasks/Approvals are nested under a specific proposal so they can
  be gated through workspace_access.py's resolve_proposal_access(), which
  already knows how to check ownership, org-sharing, and scoped guest
  access in one place — proposals.py's owner-only `_get_proposal_or_404`
  is deliberately NOT reused here, since collaboration features need to
  work for shared-with members and guests too, not just the owner.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import (
    AuditLog, OrgProposal, User, WorkspaceGuestAccess, WorkspaceTask,
)
from models.schemas import (
    ActivityEntryOut, ApprovalDecisionRequest, ApprovalRequestCreate,
    ApprovalRequestOut, CommentCreate, CommentOut, CommentUpdate,
    DepartmentCreate, DepartmentOut, GuestAccessOut, GuestInviteRequest,
    NotificationOut, TeamCreate, TeamMemberOut, TeamOut, TeamUpdate,
    WorkspaceTaskCreate, WorkspaceTaskOut, WorkspaceTaskUpdate,
)
from routers.auth import get_current_user
from routers.organizations import _assert_member, _assert_permission
from engines.collaboration_engine import CollaborationEngine
from workspace_access import assert_can_comment, assert_can_edit, assert_can_view
from audit import log_action

router = APIRouter()
engine = CollaborationEngine()


async def _users_by_id(db: AsyncSession, user_ids: set) -> dict:
    """Batch-load users for display-name joins — same pattern as
    organizations.py::get_audit_log."""
    if not user_ids:
        return {}
    result = await db.execute(select(User).where(User.id.in_(user_ids)))
    return {u.id: u for u in result.scalars().all()}


# ── Departments ──────────────────────────────────────────────────────────────

@router.post("/organizations/{org_id}/departments", response_model=DepartmentOut, status_code=201)
async def create_department(
    org_id: str, body: DepartmentCreate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _assert_permission(org_id, current_user.id, "manage_workspace", db)
    dept = await engine.create_department(db, org_id, current_user.id, body.name)
    await log_action(db, actor_id=current_user.id, action="department.created",
                      org_id=org_id, object_type="department", object_id=dept.id)
    return dept


@router.get("/organizations/{org_id}/departments", response_model=List[DepartmentOut])
async def list_departments(
    org_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _assert_member(org_id, current_user.id, db)
    return await engine.list_departments(db, org_id)


@router.delete("/departments/{department_id}", status_code=204)
async def delete_department(
    department_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    dept = await engine.get_department_or_404(db, department_id)
    await _assert_permission(dept.org_id, current_user.id, "manage_workspace", db)
    await engine.delete_department(db, department_id)
    await log_action(db, actor_id=current_user.id, action="department.deleted",
                      org_id=dept.org_id, object_type="department", object_id=department_id)


# ── Teams ────────────────────────────────────────────────────────────────────

@router.post("/organizations/{org_id}/teams", response_model=TeamOut, status_code=201)
async def create_team(
    org_id: str, body: TeamCreate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _assert_permission(org_id, current_user.id, "manage_workspace", db)
    team = await engine.create_team(db, org_id, current_user.id, body.name, body.department_id)
    await log_action(db, actor_id=current_user.id, action="team.created",
                      org_id=org_id, object_type="team", object_id=team.id)
    return team


@router.get("/organizations/{org_id}/teams", response_model=List[TeamOut])
async def list_teams(
    org_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _assert_member(org_id, current_user.id, db)
    return await engine.list_teams(db, org_id)


@router.patch("/teams/{team_id}", response_model=TeamOut)
async def update_team(
    team_id: str, body: TeamUpdate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    team = await engine.get_team_or_404(db, team_id)
    await _assert_permission(team.org_id, current_user.id, "manage_workspace", db)
    return await engine.update_team(db, team_id, body.model_dump(exclude_unset=True))


@router.delete("/teams/{team_id}", status_code=204)
async def delete_team(
    team_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    team = await engine.get_team_or_404(db, team_id)
    await _assert_permission(team.org_id, current_user.id, "manage_workspace", db)
    await engine.delete_team(db, team_id)


@router.post("/teams/{team_id}/members", response_model=TeamMemberOut, status_code=201)
async def add_team_member(
    team_id: str, user_id: str,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    team = await engine.get_team_or_404(db, team_id)
    await _assert_permission(team.org_id, current_user.id, "manage_workspace", db)
    # The person being added must already be an org member — a team is a
    # grouping within the org's existing membership, not a separate invite.
    await _assert_member(team.org_id, user_id, db)
    tm = await engine.add_team_member(db, team_id, user_id)
    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    return TeamMemberOut(
        id=tm.id, team_id=tm.team_id, user_id=tm.user_id,
        email=target.email if target else "", full_name=target.full_name if target else "",
        role=tm.role,
    )


@router.get("/teams/{team_id}/members", response_model=List[TeamMemberOut])
async def list_team_members(
    team_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    team = await engine.get_team_or_404(db, team_id)
    await _assert_member(team.org_id, current_user.id, db)
    members = await engine.list_team_members(db, team_id)
    users = await _users_by_id(db, {m.user_id for m in members})
    return [
        TeamMemberOut(
            id=m.id, team_id=m.team_id, user_id=m.user_id,
            email=users[m.user_id].email if m.user_id in users else "",
            full_name=users[m.user_id].full_name if m.user_id in users else "",
            role=m.role,
        )
        for m in members
    ]


@router.delete("/teams/{team_id}/members/{user_id}", status_code=204)
async def remove_team_member(
    team_id: str, user_id: str,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    team = await engine.get_team_or_404(db, team_id)
    await _assert_permission(team.org_id, current_user.id, "manage_workspace", db)
    await engine.remove_team_member(db, team_id, user_id)


# ── Guest Access ─────────────────────────────────────────────────────────────

@router.post("/organizations/{org_id}/proposals/{proposal_id}/guests", response_model=GuestAccessOut, status_code=201)
async def invite_guest(
    org_id: str, proposal_id: str, body: GuestInviteRequest,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _assert_permission(org_id, current_user.id, "manage_guests", db)
    shared = await db.execute(
        select(OrgProposal).where(OrgProposal.org_id == org_id, OrgProposal.proposal_id == proposal_id)
    )
    if not shared.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="This proposal is not shared with this organization.")

    guest, target = await engine.invite_guest(db, org_id, proposal_id, body.email, current_user.id, body.can_comment)
    await log_action(db, actor_id=current_user.id, action="guest.invited", org_id=org_id,
                      object_type="proposal", object_id=proposal_id, detail={"email": body.email})
    return GuestAccessOut(
        id=guest.id, org_id=guest.org_id, proposal_id=guest.proposal_id, user_id=guest.user_id,
        email=target.email, can_comment=guest.can_comment, created_at=guest.created_at,
    )


@router.get("/organizations/{org_id}/proposals/{proposal_id}/guests", response_model=List[GuestAccessOut])
async def list_guests(
    org_id: str, proposal_id: str,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _assert_member(org_id, current_user.id, db)
    guests = await engine.list_guest_access(db, proposal_id)
    users = await _users_by_id(db, {g.user_id for g in guests})
    return [
        GuestAccessOut(
            id=g.id, org_id=g.org_id, proposal_id=g.proposal_id, user_id=g.user_id,
            email=users[g.user_id].email if g.user_id in users else "",
            can_comment=g.can_comment, created_at=g.created_at,
        )
        for g in guests
    ]


@router.delete("/guests/{guest_access_id}", status_code=204)
async def revoke_guest(
    guest_access_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(WorkspaceGuestAccess).where(WorkspaceGuestAccess.id == guest_access_id))
    guest = result.scalar_one_or_none()
    if not guest:
        raise HTTPException(status_code=404, detail="Guest access not found")
    await _assert_permission(guest.org_id, current_user.id, "manage_guests", db)
    await engine.revoke_guest_access(db, guest_access_id)
    await log_action(db, actor_id=current_user.id, action="guest.revoked", org_id=guest.org_id,
                      object_type="proposal", object_id=guest.proposal_id)


# ── Comments ─────────────────────────────────────────────────────────────────

@router.get("/proposals/{proposal_id}/comments", response_model=List[CommentOut])
async def list_comments(
    proposal_id: str, object_type: str = "proposal", object_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await assert_can_view(proposal_id, current_user.id, db)
    comments = await engine.list_comments(db, object_type, object_id or proposal_id)
    users = await _users_by_id(db, {c.author_id for c in comments})
    return [
        CommentOut(
            id=c.id, object_type=c.object_type, object_id=c.object_id,
            parent_comment_id=c.parent_comment_id, author_id=c.author_id,
            author_name=users[c.author_id].full_name if c.author_id in users else "Unknown",
            content=c.content, mentions=c.mentions or [], edited=c.edited, created_at=c.created_at,
        )
        for c in comments
    ]


@router.post("/proposals/{proposal_id}/comments", response_model=CommentOut, status_code=201)
async def create_comment(
    proposal_id: str, body: CommentCreate, object_type: str = "proposal", object_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    access = await assert_can_comment(proposal_id, current_user.id, db)
    comment = await engine.create_comment(
        db, access.org_id, object_type, object_id or proposal_id,
        current_user.id, body.content, body.parent_comment_id,
    )
    await log_action(db, actor_id=current_user.id, action="comment.created", org_id=access.org_id,
                      object_type="proposal", object_id=proposal_id)
    return CommentOut(
        id=comment.id, object_type=comment.object_type, object_id=comment.object_id,
        parent_comment_id=comment.parent_comment_id, author_id=comment.author_id,
        author_name=current_user.full_name, content=comment.content,
        mentions=comment.mentions or [], edited=comment.edited, created_at=comment.created_at,
    )


@router.patch("/comments/{comment_id}", response_model=CommentOut)
async def update_comment(
    comment_id: str, body: CommentUpdate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    comment = await engine.get_comment_or_404(db, comment_id)
    if comment.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own comments.")
    updated = await engine.update_comment(db, comment_id, body.content)
    return CommentOut(
        id=updated.id, object_type=updated.object_type, object_id=updated.object_id,
        parent_comment_id=updated.parent_comment_id, author_id=updated.author_id,
        author_name=current_user.full_name, content=updated.content,
        mentions=updated.mentions or [], edited=updated.edited, created_at=updated.created_at,
    )


@router.delete("/comments/{comment_id}", status_code=204)
async def delete_comment(
    comment_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    comment = await engine.get_comment_or_404(db, comment_id)
    if comment.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only delete your own comments.")
    await engine.delete_comment(db, comment_id)


# ── Tasks ────────────────────────────────────────────────────────────────────

@router.get("/proposals/{proposal_id}/tasks", response_model=List[WorkspaceTaskOut])
async def list_proposal_tasks(
    proposal_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    access = await assert_can_view(proposal_id, current_user.id, db)
    return await engine.list_tasks(db, access.org_id, proposal_id=proposal_id)


@router.post("/proposals/{proposal_id}/tasks", response_model=WorkspaceTaskOut, status_code=201)
async def create_task(
    proposal_id: str, body: WorkspaceTaskCreate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    access = await assert_can_edit(proposal_id, current_user.id, db)
    data = body.model_dump()
    data["proposal_id"] = proposal_id
    task = await engine.create_task(db, access.org_id, current_user.id, data)
    # object_id=proposal_id (not task.id) so this shows up in that proposal's
    # activity feed below, same convention as comment.created/approval.requested.
    await log_action(db, actor_id=current_user.id, action="task.created", org_id=access.org_id,
                      object_type="proposal", object_id=proposal_id, detail={"task_id": task.id})
    return task


@router.get("/tasks/mine", response_model=List[WorkspaceTaskOut])
async def list_my_tasks(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    result = await db.execute(
        select(WorkspaceTask).where(WorkspaceTask.assignee_id == current_user.id)
        .order_by(WorkspaceTask.created_at.desc())
    )
    return list(result.scalars().all())


@router.patch("/tasks/{task_id}", response_model=WorkspaceTaskOut)
async def update_task(
    task_id: str, body: WorkspaceTaskUpdate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    task = await engine.get_task_or_404(db, task_id)
    if task.proposal_id:
        await assert_can_edit(task.proposal_id, current_user.id, db)
    updated = await engine.update_task(db, task_id, body.model_dump(exclude_unset=True))
    return updated


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(
    task_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    task = await engine.get_task_or_404(db, task_id)
    if task.proposal_id:
        await assert_can_edit(task.proposal_id, current_user.id, db)
    await engine.delete_task(db, task_id)


# ── Approval Requests ────────────────────────────────────────────────────────

@router.post("/proposals/{proposal_id}/approvals", response_model=ApprovalRequestOut, status_code=201)
async def create_approval_request(
    proposal_id: str, body: ApprovalRequestCreate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    access = await assert_can_edit(proposal_id, current_user.id, db)
    req = await engine.create_approval_request(
        db, access.org_id, body.object_type, body.object_id, current_user.id, body.approver_id, body.notes,
    )
    await log_action(db, actor_id=current_user.id, action="approval.requested", org_id=access.org_id,
                      object_type="proposal", object_id=proposal_id)
    return req


@router.get("/proposals/{proposal_id}/approvals", response_model=List[ApprovalRequestOut])
async def list_approval_requests(
    proposal_id: str, status: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    access = await assert_can_view(proposal_id, current_user.id, db)
    if not access.org_id:
        return []  # personal proposals have no org-scoped approval list to browse
    return await engine.list_approval_requests(db, access.org_id, status=status)


@router.post("/approvals/{approval_id}/decide", response_model=ApprovalRequestOut)
async def decide_approval_request(
    approval_id: str, body: ApprovalDecisionRequest,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    req = await engine.get_approval_request_or_404(db, approval_id)
    is_designated_approver = req.approver_id == current_user.id
    has_override = False
    if not is_designated_approver and req.org_id:
        try:
            await _assert_permission(req.org_id, current_user.id, "manage_approvals", db)
            has_override = True
        except HTTPException:
            pass
    if not (is_designated_approver or has_override):
        raise HTTPException(status_code=403, detail="You are not authorized to decide this approval request.")

    decided = await engine.decide_approval_request(db, approval_id, current_user.id, body.approved, body.decision_notes)
    await log_action(db, actor_id=current_user.id, action="approval.decided", org_id=decided.org_id,
                      object_type=decided.object_type, object_id=decided.object_id,
                      detail={"status": decided.status})
    return decided


# ── Notifications ────────────────────────────────────────────────────────────

@router.get("/notifications", response_model=List[NotificationOut])
async def list_notifications(
    unread_only: bool = False, limit: int = 50,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    return await engine.list_notifications(db, current_user.id, unread_only=unread_only, limit=limit)


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
async def mark_notification_read(
    notification_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    return await engine.mark_notification_read(db, notification_id, current_user.id)


# ── Activity Feed ────────────────────────────────────────────────────────────

@router.get("/proposals/{proposal_id}/activity", response_model=List[ActivityEntryOut])
async def get_proposal_activity(
    proposal_id: str, limit: int = 100,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """
    Unlike organizations.py's owner-only /audit-log (a security/compliance
    log), this is a collaboration-facing feed: visible to anyone with view
    access to the proposal (owner, shared org member, or guest), reading
    the same underlying AuditLog table Phase 1/2 already write to.
    """
    await assert_can_view(proposal_id, current_user.id, db)
    result = await db.execute(
        select(AuditLog).where(AuditLog.object_id == proposal_id)
        .order_by(AuditLog.created_at.desc()).limit(min(limit, 500))
    )
    entries = result.scalars().all()
    users = await _users_by_id(db, {e.actor_id for e in entries})
    return [
        ActivityEntryOut(
            id=e.id, actor_id=e.actor_id,
            actor_name=users[e.actor_id].full_name if e.actor_id in users else None,
            action=e.action, object_type=e.object_type, object_id=e.object_id,
            detail=e.detail, created_at=e.created_at,
        )
        for e in entries
    ]
