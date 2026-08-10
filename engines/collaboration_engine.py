"""
Engine 13 — Collaboration Engine
Workspace hierarchy (departments/teams), task assignment, threaded comments
with @mention parsing, notifications, configurable approval requests, and
scoped external guest access (Clariva Enterprise™ PRD §11).

Design notes (same discipline as engines/credit_engine.py and
engines/scope_of_work_engine.py):
- Every method takes an already-open AsyncSession and only flushes, never
  commits — the caller's request-scoped session commits once, after the
  router handler returns (see database.py::get_db).
- No permission checks live here — that's the router's job (via rbac.py's
  ROLE_PERMISSIONS and workspace_access.py's proposal-access resolver),
  consistent with keeping engines free of FastAPI/HTTP concerns.
- Every create/update path that returns a row for serialization calls
  `await db.refresh(obj)` after `await db.flush()`, because
  created_at/updated_at use server_default=func.now()/onupdate=func.now(),
  which this app's AsyncSession + aiosqlite setup does not eagerly fetch —
  see the MissingGreenlet fix and rationale first documented in
  credit_engine.py and repeated in scope_of_work_engine.py.
- Activity-feed / audit-log entries (log_action calls) are the router's
  responsibility, not this engine's — same layering as organizations.py,
  credits.py, and proposals.py in Phase 1/2.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import (
    ApprovalRequest, Award, AwardAmendment, AwardReport, Comment, Department,
    Notification, Team, TeamMembership, User, WorkspaceGuestAccess, WorkspaceTask,
    new_uuid,
)
from notifications import notify as _shared_notify

# Simplest unambiguous @mention syntax: @user@example.com. Matching on
# full email avoids the name-collision ambiguity of @firstname mentions
# without needing a separate username field anywhere in this codebase.
MENTION_PATTERN = re.compile(r"@([\w.\-]+@[\w.\-]+\.\w+)")


class CollaborationEngine:
    # ── Departments ──────────────────────────────────────────────────────────

    async def create_department(self, db: AsyncSession, org_id: str, user_id: str, name: str) -> Department:
        dept = Department(id=new_uuid(), org_id=org_id, name=name, created_by=user_id)
        db.add(dept)
        await db.flush()
        await db.refresh(dept)
        return dept

    async def list_departments(self, db: AsyncSession, org_id: str) -> List[Department]:
        result = await db.execute(select(Department).where(Department.org_id == org_id).order_by(Department.created_at))
        return list(result.scalars().all())

    async def get_department_or_404(self, db: AsyncSession, department_id: str) -> Department:
        result = await db.execute(select(Department).where(Department.id == department_id))
        dept = result.scalar_one_or_none()
        if not dept:
            raise HTTPException(status_code=404, detail="Department not found")
        return dept

    async def delete_department(self, db: AsyncSession, department_id: str) -> None:
        dept = await self.get_department_or_404(db, department_id)
        await db.delete(dept)
        await db.flush()

    # ── Teams ────────────────────────────────────────────────────────────────

    async def create_team(
        self, db: AsyncSession, org_id: str, user_id: str, name: str, department_id: Optional[str] = None,
    ) -> Team:
        if department_id:
            await self.get_department_or_404(db, department_id)
        team = Team(id=new_uuid(), org_id=org_id, department_id=department_id, name=name, created_by=user_id)
        db.add(team)
        await db.flush()
        await db.refresh(team)
        return team

    async def list_teams(self, db: AsyncSession, org_id: str) -> List[Team]:
        result = await db.execute(select(Team).where(Team.org_id == org_id).order_by(Team.created_at))
        return list(result.scalars().all())

    async def get_team_or_404(self, db: AsyncSession, team_id: str) -> Team:
        result = await db.execute(select(Team).where(Team.id == team_id))
        team = result.scalar_one_or_none()
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        return team

    async def update_team(self, db: AsyncSession, team_id: str, data: Dict[str, Any]) -> Team:
        team = await self.get_team_or_404(db, team_id)
        if data.get("department_id"):
            await self.get_department_or_404(db, data["department_id"])
        for field in ("name", "department_id"):
            if field in data and data[field] is not None:
                setattr(team, field, data[field])
        await db.flush()
        await db.refresh(team)
        return team

    async def delete_team(self, db: AsyncSession, team_id: str) -> None:
        team = await self.get_team_or_404(db, team_id)
        await db.delete(team)
        await db.flush()

    async def add_team_member(self, db: AsyncSession, team_id: str, user_id: str, role: str = "member") -> TeamMembership:
        await self.get_team_or_404(db, team_id)
        existing = await db.execute(
            select(TeamMembership).where(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Already a team member.")
        tm = TeamMembership(id=new_uuid(), team_id=team_id, user_id=user_id, role=role)
        db.add(tm)
        await db.flush()
        await db.refresh(tm)
        return tm

    async def list_team_members(self, db: AsyncSession, team_id: str) -> List[TeamMembership]:
        result = await db.execute(select(TeamMembership).where(TeamMembership.team_id == team_id))
        return list(result.scalars().all())

    async def remove_team_member(self, db: AsyncSession, team_id: str, user_id: str) -> None:
        result = await db.execute(
            select(TeamMembership).where(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
        )
        tm = result.scalar_one_or_none()
        if not tm:
            raise HTTPException(status_code=404, detail="Not a team member.")
        await db.delete(tm)
        await db.flush()

    # ── Guest Access (PRD §11 — external collaboration) ─────────────────────

    async def invite_guest(
        self, db: AsyncSession, org_id: str, proposal_id: str, email: str,
        invited_by: str, can_comment: bool = True,
    ) -> Tuple[WorkspaceGuestAccess, User]:
        """Same "must already have an account" convention as
        organizations.py::invite_member — no separate guest-signup flow."""
        result = await db.execute(select(User).where(User.email == email))
        target = result.scalar_one_or_none()
        if not target:
            raise HTTPException(status_code=404, detail=f"No user found with email '{email}'. They must register first.")

        existing = await db.execute(
            select(WorkspaceGuestAccess).where(
                WorkspaceGuestAccess.proposal_id == proposal_id, WorkspaceGuestAccess.user_id == target.id
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="This user already has guest access to this proposal.")

        guest = WorkspaceGuestAccess(
            id=new_uuid(), org_id=org_id, proposal_id=proposal_id, user_id=target.id,
            invited_by=invited_by, can_comment=can_comment,
        )
        db.add(guest)
        await db.flush()
        await db.refresh(guest)
        await self.notify(db, target.id, "guest_invited", "You've been given guest access to a proposal.",
                           object_type="proposal", object_id=proposal_id)
        return guest, target

    async def list_guest_access(self, db: AsyncSession, proposal_id: str) -> List[WorkspaceGuestAccess]:
        result = await db.execute(select(WorkspaceGuestAccess).where(WorkspaceGuestAccess.proposal_id == proposal_id))
        return list(result.scalars().all())

    async def revoke_guest_access(self, db: AsyncSession, guest_access_id: str) -> None:
        result = await db.execute(select(WorkspaceGuestAccess).where(WorkspaceGuestAccess.id == guest_access_id))
        guest = result.scalar_one_or_none()
        if not guest:
            raise HTTPException(status_code=404, detail="Guest access not found")
        await db.delete(guest)
        await db.flush()

    # ── Workspace Tasks (generic, distinct from Phase 2's SOW Task) ─────────

    async def create_task(self, db: AsyncSession, org_id: str, created_by: str, data: Dict[str, Any]) -> WorkspaceTask:
        task = WorkspaceTask(
            id=new_uuid(), org_id=org_id, created_by=created_by,
            proposal_id=data.get("proposal_id"), title=data["title"],
            description=data.get("description"), assignee_id=data.get("assignee_id"),
            due_date=data.get("due_date"),
        )
        db.add(task)
        await db.flush()
        await db.refresh(task)
        if task.assignee_id and task.assignee_id != created_by:
            await self.notify(db, task.assignee_id, "task_assigned", f"You were assigned a task: {task.title}",
                               object_type="workspace_task", object_id=task.id)
        return task

    async def list_tasks(
        self, db: AsyncSession, org_id: str, proposal_id: Optional[str] = None, assignee_id: Optional[str] = None,
    ) -> List[WorkspaceTask]:
        query = select(WorkspaceTask).where(WorkspaceTask.org_id == org_id)
        if proposal_id:
            query = query.where(WorkspaceTask.proposal_id == proposal_id)
        if assignee_id:
            query = query.where(WorkspaceTask.assignee_id == assignee_id)
        result = await db.execute(query.order_by(WorkspaceTask.created_at.desc()))
        return list(result.scalars().all())

    async def get_task_or_404(self, db: AsyncSession, task_id: str) -> WorkspaceTask:
        result = await db.execute(select(WorkspaceTask).where(WorkspaceTask.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return task

    async def update_task(self, db: AsyncSession, task_id: str, data: Dict[str, Any]) -> WorkspaceTask:
        task = await self.get_task_or_404(db, task_id)
        reassigned = "assignee_id" in data and data["assignee_id"] != task.assignee_id and data["assignee_id"]
        for field in ("title", "description", "assignee_id", "status", "due_date"):
            if field in data and data[field] is not None:
                setattr(task, field, data[field])
        await db.flush()
        await db.refresh(task)
        if reassigned:
            await self.notify(db, task.assignee_id, "task_assigned", f"You were assigned a task: {task.title}",
                               object_type="workspace_task", object_id=task.id)
        return task

    async def delete_task(self, db: AsyncSession, task_id: str) -> None:
        task = await self.get_task_or_404(db, task_id)
        await db.delete(task)
        await db.flush()

    # ── Comments (polymorphic, threaded, PRD §11) ───────────────────────────

    def _parse_mentions(self, content: str) -> List[str]:
        return sorted(set(MENTION_PATTERN.findall(content)))

    async def create_comment(
        self, db: AsyncSession, org_id: str, object_type: str, object_id: str,
        author_id: str, content: str, parent_comment_id: Optional[str] = None,
    ) -> Comment:
        mention_emails = self._parse_mentions(content)
        mentioned_user_ids: List[str] = []
        if mention_emails:
            result = await db.execute(select(User).where(User.email.in_(mention_emails)))
            mentioned_user_ids = [u.id for u in result.scalars().all()]

        comment = Comment(
            id=new_uuid(), org_id=org_id, object_type=object_type, object_id=object_id,
            parent_comment_id=parent_comment_id, author_id=author_id, content=content,
            mentions=mentioned_user_ids,
        )
        db.add(comment)
        await db.flush()
        await db.refresh(comment)

        for uid in mentioned_user_ids:
            if uid != author_id:
                await self.notify(db, uid, "mention", "You were mentioned in a comment.",
                                   object_type=object_type, object_id=object_id)
        return comment

    async def list_comments(self, db: AsyncSession, object_type: str, object_id: str) -> List[Comment]:
        result = await db.execute(
            select(Comment).where(Comment.object_type == object_type, Comment.object_id == object_id)
            .order_by(Comment.created_at)
        )
        return list(result.scalars().all())

    async def get_comment_or_404(self, db: AsyncSession, comment_id: str) -> Comment:
        result = await db.execute(select(Comment).where(Comment.id == comment_id))
        comment = result.scalar_one_or_none()
        if not comment:
            raise HTTPException(status_code=404, detail="Comment not found")
        return comment

    async def update_comment(self, db: AsyncSession, comment_id: str, content: str) -> Comment:
        comment = await self.get_comment_or_404(db, comment_id)
        comment.content = content
        comment.edited = True
        await db.flush()
        await db.refresh(comment)
        return comment

    async def delete_comment(self, db: AsyncSession, comment_id: str) -> None:
        comment = await self.get_comment_or_404(db, comment_id)
        await db.delete(comment)
        await db.flush()

    # ── Notifications ────────────────────────────────────────────────────────

    async def notify(
        self, db: AsyncSession, user_id: str, type_: str, message: str,
        object_type: Optional[str] = None, object_id: Optional[str] = None,
    ) -> Notification:
        """Delegates to the shared notifications.py::notify() helper (see
        that module's docstring for why) — dedupe=False preserves this
        method's original always-write behavior exactly."""
        return await _shared_notify(
            db, user_id, type_, message, object_type=object_type, object_id=object_id, dedupe=False,
        )

    async def list_notifications(
        self, db: AsyncSession, user_id: str, unread_only: bool = False, limit: int = 50,
    ) -> List[Notification]:
        query = select(Notification).where(Notification.user_id == user_id)
        if unread_only:
            query = query.where(Notification.read.is_(False))
        result = await db.execute(query.order_by(Notification.created_at.desc()).limit(min(limit, 200)))
        return list(result.scalars().all())

    async def mark_notification_read(self, db: AsyncSession, notification_id: str, user_id: str) -> Notification:
        result = await db.execute(select(Notification).where(Notification.id == notification_id))
        n = result.scalar_one_or_none()
        if not n or n.user_id != user_id:
            raise HTTPException(status_code=404, detail="Notification not found")
        n.read = True
        await db.flush()
        await db.refresh(n)
        return n

    # ── Approval Requests (PRD §11 — configurable approval workflows) ──────

    async def create_approval_request(
        self, db: AsyncSession, org_id: str, object_type: str, object_id: str,
        requested_by: str, approver_id: Optional[str] = None, notes: Optional[str] = None,
    ) -> ApprovalRequest:
        req = ApprovalRequest(
            id=new_uuid(), org_id=org_id, object_type=object_type, object_id=object_id,
            requested_by=requested_by, approver_id=approver_id, notes=notes,
        )
        db.add(req)
        await db.flush()
        await db.refresh(req)
        if approver_id:
            await self.notify(db, approver_id, "approval_requested", "An approval request needs your decision.",
                               object_type=object_type, object_id=object_id)
        return req

    async def list_approval_requests(
        self, db: AsyncSession, org_id: str, object_type: Optional[str] = None,
        object_id: Optional[str] = None, status: Optional[str] = None,
    ) -> List[ApprovalRequest]:
        query = select(ApprovalRequest).where(ApprovalRequest.org_id == org_id)
        if object_type:
            query = query.where(ApprovalRequest.object_type == object_type)
        if object_id:
            query = query.where(ApprovalRequest.object_id == object_id)
        if status:
            query = query.where(ApprovalRequest.status == status)
        result = await db.execute(query.order_by(ApprovalRequest.created_at.desc()))
        return list(result.scalars().all())

    async def get_approval_request_or_404(self, db: AsyncSession, request_id: str) -> ApprovalRequest:
        result = await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == request_id))
        req = result.scalar_one_or_none()
        if not req:
            raise HTTPException(status_code=404, detail="Approval request not found")
        return req

    async def decide_approval_request(
        self, db: AsyncSession, request_id: str, decided_by: str, approved: bool,
        decision_notes: Optional[str] = None,
    ) -> ApprovalRequest:
        req = await self.get_approval_request_or_404(db, request_id)
        if req.status != "pending":
            raise HTTPException(status_code=400, detail="This request has already been decided.")
        req.status = "approved" if approved else "rejected"
        req.decided_by = decided_by
        req.decision_notes = decision_notes
        req.decided_at = datetime.utcnow()

        # Phase 5 — Award & Project Management (PRD §16): amendments route
        # through this same generic approval flow rather than a parallel
        # award-specific one. Deciding the ApprovalRequest is also what
        # decides the AwardAmendment and, on approval, applies its
        # `effective_changes` to the Award — there is no separate manual
        # "apply amendment" step.
        if req.object_type == "award_amendment":
            amendment_result = await db.execute(select(AwardAmendment).where(AwardAmendment.id == req.object_id))
            amendment = amendment_result.scalar_one_or_none()
            if amendment and amendment.status == "pending":
                amendment.status = req.status
                amendment.decided_at = req.decided_at
                if req.status == "approved" and amendment.effective_changes:
                    award_result = await db.execute(select(Award).where(Award.id == amendment.award_id))
                    award = award_result.scalar_one_or_none()
                    if award:
                        for field, value in amendment.effective_changes.items():
                            if hasattr(award, field):
                                setattr(award, field, value)

        # Phase 5 (cont'd) — Human-in-the-loop post-award reports (PRD §17):
        # deciding the ApprovalRequest is also what decides the AwardReport,
        # exactly like award_amendment above. Approval is the only thing
        # that unlocks AwardEngine.export_report() — a rejected report just
        # goes back to the requester as a rejected draft-in-spirit; there is
        # no automatic re-submission.
        if req.object_type == "award_report":
            report_result = await db.execute(select(AwardReport).where(AwardReport.id == req.object_id))
            report = report_result.scalar_one_or_none()
            if report and report.status == "pending_approval":
                report.status = req.status
                report.decided_at = req.decided_at

        await db.flush()
        await db.refresh(req)
        await self.notify(db, req.requested_by, "approval_decided", f"Your approval request was {req.status}.",
                           object_type=req.object_type, object_id=req.object_id)
        return req
