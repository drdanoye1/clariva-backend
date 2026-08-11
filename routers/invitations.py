"""
Invitations router — the public (no-auth) half of the invite flow.

Everything that requires an existing org member (creating, listing,
resending, revoking an invitation) lives in routers/organizations.py next
to the RBAC helpers it needs. This router is intentionally separate and
mounted with no auth dependency at all: an invitee following an emailed
link doesn't have a Clariva account yet, so there's nothing to
authenticate them with until *after* they accept.

Token lookups (`Invitation.token`) are the only way in here — there's no
way to enumerate or guess your way to another org's pending invitations,
same trust model as document_library_engine.py's `share_token` and
WorkspaceGuestAccess links.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import Department, Invitation, Organization, OrgMembership, Team, TeamMembership, User
from models.schemas import LoginResponse
from routers.auth import _issue_tokens, hash_password
from audit import log_action

router = APIRouter()


class InvitationPreviewOut(BaseModel):
    email: str
    role: str
    org_name: str
    team_name: Optional[str] = None
    department_name: Optional[str] = None
    valid: bool
    reason: Optional[str] = None  # populated when valid=False, e.g. "expired" | "revoked" | "accepted"


class InvitationAcceptRequest(BaseModel):
    full_name: str
    password: str


async def _load_invitation(token: str, db: AsyncSession) -> Invitation:
    result = await db.execute(select(Invitation).where(Invitation.token == token))
    invitation = result.scalar_one_or_none()
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found.")
    return invitation


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Strip tzinfo so a comparison works regardless of which backend handed
    the value back — same helper/rationale as engines/award_engine.py's
    _naive(). Reused as a plain (not imported) local copy rather than
    importing across the router/engine boundary."""
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _invitation_validity(invitation: Invitation) -> Optional[str]:
    """None = valid; otherwise a short reason code for why it isn't.

    invitation.expires_at comes from a DateTime(timezone=True) column. On
    Postgres (production) SQLAlchemy hands back a timezone-aware datetime;
    on SQLite (the test DB) it comes back naive. A previous version of this
    function always compared against a tz-aware `datetime.now(timezone.utc)`
    to fix the Postgres case — which broke the SQLite case instead,
    "can't compare offset-naive and offset-aware datetimes" either way,
    just from whichever side was actually naive. Stripping tzinfo from BOTH
    sides before comparing works on both backends."""
    if invitation.status == "revoked":
        return "revoked"
    if invitation.status == "accepted":
        return "accepted"
    if invitation.expires_at and _naive(invitation.expires_at) < _naive(datetime.now(timezone.utc)):
        return "expired"
    return None


@router.get("/by-token/{token}", response_model=InvitationPreviewOut)
async def preview_invitation(token: str, db: AsyncSession = Depends(get_db)):
    """Lets the accept-invite page show who's inviting the person to what,
    before they commit to setting a password."""
    invitation = await _load_invitation(token, db)

    org_result = await db.execute(select(Organization).where(Organization.id == invitation.org_id))
    org = org_result.scalar_one_or_none()

    team_name = None
    if invitation.team_id:
        t_result = await db.execute(select(Team).where(Team.id == invitation.team_id))
        team = t_result.scalar_one_or_none()
        team_name = team.name if team else None

    department_name = None
    if invitation.department_id:
        d_result = await db.execute(select(Department).where(Department.id == invitation.department_id))
        department = d_result.scalar_one_or_none()
        department_name = department.name if department else None

    reason = _invitation_validity(invitation)
    return InvitationPreviewOut(
        email=invitation.email, role=invitation.role,
        org_name=org.name if org else "this organization",
        team_name=team_name, department_name=department_name,
        valid=reason is None, reason=reason,
    )


@router.post("/by-token/{token}/accept", response_model=LoginResponse)
async def accept_invitation(token: str, body: InvitationAcceptRequest, db: AsyncSession = Depends(get_db)):
    """Creates the invitee's account, adds them to the org (and team, if
    one was specified) with the role the inviter chose, and logs them in —
    one submit, no separate register-then-login round trip."""
    invitation = await _load_invitation(token, db)
    reason = _invitation_validity(invitation)
    if reason:
        raise HTTPException(status_code=400, detail=f"This invitation has been {reason}.")

    if not body.full_name.strip():
        raise HTTPException(status_code=400, detail="Full name is required.")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    existing = await db.execute(select(User).where(User.email == invitation.email))
    if existing.scalar_one_or_none():
        # The email registered independently after the invite went out —
        # nothing to create; send them to log in normally instead.
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists. Please log in instead.",
        )

    org_result = await db.execute(select(Organization).where(Organization.id == invitation.org_id))
    org = org_result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="The inviting organization no longer exists.")

    user = User(
        id=str(uuid.uuid4()),
        email=invitation.email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name.strip(),
        organization=org.name,
    )
    db.add(user)
    await db.flush()

    db.add(OrgMembership(
        id=str(uuid.uuid4()), org_id=invitation.org_id, user_id=user.id,
        role=invitation.role, invited_by=invitation.invited_by,
    ))
    if invitation.team_id:
        db.add(TeamMembership(id=str(uuid.uuid4()), team_id=invitation.team_id, user_id=user.id))

    invitation.status = "accepted"
    invitation.accepted_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(user)

    await log_action(db, actor_id=user.id, action="member.invitation_accepted",
                      org_id=invitation.org_id, object_type="user", object_id=user.id,
                      detail={"role": invitation.role})

    return _issue_tokens(user)
