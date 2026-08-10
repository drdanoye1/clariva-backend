"""Organizations router — create org, invite members, share proposals."""

from __future__ import annotations
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

import email_service
from config import settings
from database import get_db
from models.db_models import (
    AuditLog, Invitation, Organization, OrgMembership, OrgProposal,
    Proposal, Team, TeamMembership, User,
)
from models.schemas import OrganizationBrandingOut, OrganizationBrandingUpdate
from routers.auth import get_current_user
from audit import log_action
from rbac import ROLES, is_valid_role, roles_with_permission

router = APIRouter()

INVITATION_EXPIRY_DAYS = 7


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class OrgCreate(BaseModel):
    name: str

class OrgOut(BaseModel):
    id: str
    name: str
    created_by: str
    member_count: int
    proposal_count: int

class MemberOut(BaseModel):
    user_id: str
    email: str
    full_name: str
    role: str
    joined_at: Optional[str] = None

class InviteRequest(BaseModel):
    email: str
    role: str = "editor"  # owner | editor | viewer
    team_id: Optional[str] = None
    department_id: Optional[str] = None

class InvitationOut(BaseModel):
    id: str
    org_id: str
    email: str
    role: str
    team_id: Optional[str] = None
    department_id: Optional[str] = None
    status: str
    expires_at: Optional[str] = None
    created_at: Optional[str] = None

class ShareProposalRequest(BaseModel):
    proposal_id: str

class RoleUpdateRequest(BaseModel):
    role: str  # owner | editor | viewer

class AuditLogOut(BaseModel):
    id: str
    actor_id: str
    actor_email: Optional[str] = None
    actor_name: Optional[str] = None
    action: str
    object_type: Optional[str] = None
    object_id: Optional[str] = None
    detail: Optional[dict] = None
    created_at: Optional[str] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/", status_code=201)
async def create_organization(
    body: OrgCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Organization name required.")

    org = Organization(
        id=str(uuid.uuid4()),
        name=body.name.strip(),
        created_by=current_user.id,
    )
    db.add(org)
    await db.flush()

    # Add creator as owner
    membership = OrgMembership(
        id=str(uuid.uuid4()),
        org_id=org.id,
        user_id=current_user.id,
        role="owner",
        invited_by=current_user.id,
    )
    db.add(membership)
    await db.flush()

    await log_action(db, actor_id=current_user.id, action="org.created",
                      org_id=org.id, object_type="organization", object_id=org.id,
                      detail={"name": org.name})

    return {"id": org.id, "name": org.name, "role": "owner"}


@router.get("/", response_model=List[dict])
async def list_my_organizations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all organizations the current user belongs to."""
    memberships_result = await db.execute(
        select(OrgMembership).where(OrgMembership.user_id == current_user.id)
    )
    memberships = memberships_result.scalars().all()
    if not memberships:
        return []

    org_ids = [m.org_id for m in memberships]
    orgs_result = await db.execute(
        select(Organization).where(Organization.id.in_(org_ids))
    )
    orgs = {o.id: o for o in orgs_result.scalars().all()}

    result = []
    for m in memberships:
        org = orgs.get(m.org_id)
        if not org:
            continue
        # Count members
        mc_result = await db.execute(
            select(OrgMembership).where(OrgMembership.org_id == org.id)
        )
        member_count = len(mc_result.scalars().all())
        pc_result = await db.execute(
            select(OrgProposal).where(OrgProposal.org_id == org.id)
        )
        proposal_count = len(pc_result.scalars().all())
        result.append({
            "id": org.id,
            "name": org.name,
            "role": m.role,
            "member_count": member_count,
            "proposal_count": proposal_count,
            "created_at": org.created_at.isoformat() if org.created_at else None,
        })
    return result


@router.get("/{org_id}/members", response_model=List[dict])
async def list_members(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_member(org_id, current_user.id, db)
    result = await db.execute(
        select(OrgMembership).where(OrgMembership.org_id == org_id)
    )
    memberships = result.scalars().all()
    out = []
    for m in memberships:
        u_result = await db.execute(select(User).where(User.id == m.user_id))
        u = u_result.scalar_one_or_none()
        if u:
            out.append({
                "user_id": u.id,
                "email": u.email,
                "full_name": u.full_name,
                "role": m.role,
                "joined_at": m.created_at.isoformat() if m.created_at else None,
            })
    return out


@router.post("/{org_id}/invite")
async def invite_member(
    org_id: str,
    body: InviteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Invite someone to the organization by email. If they already have a
    Clariva account, they're added immediately (unchanged behavior). If
    not, a pending Invitation is created and emailed instead of the old
    404 — the org's admin no longer has to separately tell the invitee to
    go self-register first."""
    await _assert_permission(org_id, current_user.id, "invite_members", db)

    if not is_valid_role(body.role):
        raise HTTPException(status_code=400, detail=f"Invalid role. Choose: {', '.join(ROLES)}")

    org = await _get_org_or_404(org_id, db)

    # Look up the invitee
    target_result = await db.execute(select(User).where(User.email == body.email))
    target = target_result.scalar_one_or_none()

    if not target:
        # No account yet — create (or refresh) a pending Invitation and
        # email a signup link instead of erroring.
        existing_invite = await db.execute(
            select(Invitation).where(
                Invitation.org_id == org_id, Invitation.email == body.email, Invitation.status == "pending",
            )
        )
        invitation = existing_invite.scalar_one_or_none()
        if invitation:
            invitation.role = body.role
            invitation.team_id = body.team_id
            invitation.department_id = body.department_id
            invitation.expires_at = datetime.now(timezone.utc) + timedelta(days=INVITATION_EXPIRY_DAYS)
        else:
            invitation = Invitation(
                id=str(uuid.uuid4()), org_id=org_id, email=body.email, role=body.role,
                team_id=body.team_id, department_id=body.department_id,
                token=secrets.token_urlsafe(32), invited_by=current_user.id,
                expires_at=datetime.now(timezone.utc) + timedelta(days=INVITATION_EXPIRY_DAYS),
            )
            db.add(invitation)
        await db.flush()

        await _send_invitation_email(db, org, invitation, current_user)

        await log_action(db, actor_id=current_user.id, action="member.invitation_sent",
                          org_id=org_id, object_type="invitation", object_id=invitation.id,
                          detail={"role": body.role, "email": body.email})

        return {"message": f"Invitation sent to {body.email}.", "invitation_id": invitation.id, "status": "invited"}

    # Check already a member
    existing = await db.execute(
        select(OrgMembership).where(OrgMembership.org_id == org_id, OrgMembership.user_id == target.id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="User is already a member of this organization.")

    membership = OrgMembership(
        id=str(uuid.uuid4()),
        org_id=org_id,
        user_id=target.id,
        role=body.role,
        invited_by=current_user.id,
    )
    db.add(membership)
    if body.team_id:
        db.add(TeamMembership(id=str(uuid.uuid4()), team_id=body.team_id, user_id=target.id))
    await db.flush()

    await log_action(db, actor_id=current_user.id, action="member.invited",
                      org_id=org_id, object_type="user", object_id=target.id,
                      detail={"role": body.role, "email": target.email})

    return {"message": f"{target.full_name} added as {body.role}.", "user_id": target.id, "status": "added"}


async def _send_invitation_email(db: AsyncSession, org: Organization, invitation: Invitation, inviter: User) -> None:
    team_name = None
    if invitation.team_id:
        t_result = await db.execute(select(Team).where(Team.id == invitation.team_id))
        team = t_result.scalar_one_or_none()
        team_name = team.name if team else None
    accept_url = f"{settings.FRONTEND_URL.rstrip('/')}/accept-invite?token={invitation.token}"
    html = email_service.render_invitation_email(
        org_name=org.name, inviter_name=inviter.full_name, role=invitation.role,
        accept_url=accept_url, team_name=team_name,
    )
    await email_service.send_email(invitation.email, f"You're invited to join {org.name} on Clariva", html)


@router.get("/{org_id}/invitations", response_model=List[InvitationOut])
async def list_invitations(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pending/past invitations for the org — same permission as inviting."""
    await _assert_permission(org_id, current_user.id, "invite_members", db)
    result = await db.execute(
        select(Invitation).where(Invitation.org_id == org_id).order_by(Invitation.created_at.desc())
    )
    return [
        InvitationOut(
            id=i.id, org_id=i.org_id, email=i.email, role=i.role,
            team_id=i.team_id, department_id=i.department_id, status=i.status,
            expires_at=i.expires_at.isoformat() if i.expires_at else None,
            created_at=i.created_at.isoformat() if i.created_at else None,
        )
        for i in result.scalars().all()
    ]


@router.post("/{org_id}/invitations/{invitation_id}/resend", response_model=InvitationOut)
async def resend_invitation(
    org_id: str,
    invitation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_permission(org_id, current_user.id, "invite_members", db)
    invitation = await _get_invitation_or_404(org_id, invitation_id, db)
    if invitation.status != "pending":
        raise HTTPException(status_code=400, detail=f"Cannot resend a {invitation.status} invitation.")

    invitation.expires_at = datetime.now(timezone.utc) + timedelta(days=INVITATION_EXPIRY_DAYS)
    await db.flush()

    org = await _get_org_or_404(org_id, db)
    await _send_invitation_email(db, org, invitation, current_user)

    return InvitationOut(
        id=invitation.id, org_id=invitation.org_id, email=invitation.email, role=invitation.role,
        team_id=invitation.team_id, department_id=invitation.department_id, status=invitation.status,
        expires_at=invitation.expires_at.isoformat(), created_at=invitation.created_at.isoformat() if invitation.created_at else None,
    )


@router.delete("/{org_id}/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(
    org_id: str,
    invitation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_permission(org_id, current_user.id, "invite_members", db)
    invitation = await _get_invitation_or_404(org_id, invitation_id, db)
    invitation.status = "revoked"
    await db.flush()

    await log_action(db, actor_id=current_user.id, action="member.invitation_revoked",
                      org_id=org_id, object_type="invitation", object_id=invitation.id)


async def _get_invitation_or_404(org_id: str, invitation_id: str, db: AsyncSession) -> Invitation:
    result = await db.execute(
        select(Invitation).where(Invitation.id == invitation_id, Invitation.org_id == org_id)
    )
    invitation = result.scalar_one_or_none()
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found.")
    return invitation


@router.delete("/{org_id}/members/{user_id}", status_code=204)
async def remove_member(
    org_id: str,
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_permission(org_id, current_user.id, "remove_members", db)
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot remove yourself. Transfer ownership first.")
    result = await db.execute(
        select(OrgMembership).where(OrgMembership.org_id == org_id, OrgMembership.user_id == user_id)
    )
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Member not found.")
    await db.delete(m)

    await log_action(db, actor_id=current_user.id, action="member.removed",
                      org_id=org_id, object_type="user", object_id=user_id)


@router.patch("/{org_id}/members/{user_id}/role")
async def update_member_role(
    org_id: str,
    user_id: str,
    body: RoleUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Change a member's role. Requires manage_roles (owner today)."""
    await _assert_permission(org_id, current_user.id, "manage_roles", db)

    if not is_valid_role(body.role):
        raise HTTPException(status_code=400, detail=f"Invalid role. Choose: {', '.join(ROLES)}")

    target = await _get_membership(org_id, user_id, db)
    if target.role == body.role:
        return {"message": "No change.", "user_id": user_id, "role": body.role}

    if target.role == "owner" and body.role != "owner":
        # Never allow the last owner to be demoted — an org must always
        # have at least one owner able to manage it.
        owners_result = await db.execute(
            select(OrgMembership).where(OrgMembership.org_id == org_id, OrgMembership.role == "owner")
        )
        if len(owners_result.scalars().all()) <= 1:
            raise HTTPException(status_code=400, detail="Cannot demote the only owner. Promote another member to owner first.")

    previous_role = target.role
    target.role = body.role
    await db.flush()

    await log_action(db, actor_id=current_user.id, action="member.role_changed",
                      org_id=org_id, object_type="user", object_id=user_id,
                      detail={"from": previous_role, "to": body.role})

    return {"message": f"Role updated to {body.role}.", "user_id": user_id, "role": body.role}


@router.get("/{org_id}/audit-log", response_model=List[AuditLogOut])
async def get_audit_log(
    org_id: str,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_permission(org_id, current_user.id, "view_audit_log", db)

    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.org_id == org_id)
        .order_by(AuditLog.created_at.desc())
        .limit(min(limit, 500))
    )
    entries = result.scalars().all()

    # Join actor email/name for display without a separate frontend round-trip.
    actor_ids = {e.actor_id for e in entries}
    actors: dict = {}
    if actor_ids:
        u_result = await db.execute(select(User).where(User.id.in_(actor_ids)))
        actors = {u.id: u for u in u_result.scalars().all()}

    out = []
    for e in entries:
        actor = actors.get(e.actor_id)
        out.append(AuditLogOut(
            id=e.id,
            actor_id=e.actor_id,
            actor_email=actor.email if actor else None,
            actor_name=actor.full_name if actor else None,
            action=e.action,
            object_type=e.object_type,
            object_id=e.object_id,
            detail=e.detail,
            created_at=e.created_at.isoformat() if e.created_at else None,
        ))
    return out


@router.post("/{org_id}/proposals")
async def share_proposal(
    org_id: str,
    body: ShareProposalRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Share one of the user's proposals with the organization."""
    await _assert_permission(org_id, current_user.id, "share_proposals", db)

    # Verify proposal belongs to user
    p_result = await db.execute(
        select(Proposal).where(Proposal.id == body.proposal_id, Proposal.owner_id == current_user.id)
    )
    if not p_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Proposal not found or not yours.")

    # Check already shared
    existing = await db.execute(
        select(OrgProposal).where(OrgProposal.org_id == org_id, OrgProposal.proposal_id == body.proposal_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Proposal already shared with this organization.")

    link = OrgProposal(
        id=str(uuid.uuid4()),
        org_id=org_id,
        proposal_id=body.proposal_id,
        shared_by=current_user.id,
    )
    db.add(link)
    await db.flush()

    await log_action(db, actor_id=current_user.id, action="proposal.shared",
                      org_id=org_id, object_type="proposal", object_id=body.proposal_id)

    return {"message": "Proposal shared successfully."}


@router.get("/{org_id}/proposals", response_model=List[dict])
async def list_org_proposals(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_member(org_id, current_user.id, db)
    result = await db.execute(
        select(OrgProposal).where(OrgProposal.org_id == org_id)
    )
    links = result.scalars().all()
    out = []
    for link in links:
        p_result = await db.execute(select(Proposal).where(Proposal.id == link.proposal_id))
        p = p_result.scalar_one_or_none()
        if p:
            out.append({
                "proposal_id": p.id,
                "title": p.title,
                "agency": p.agency,
                "phase": p.phase,
                "status": p.status,
                "shared_by": link.shared_by,
                "shared_at": link.created_at.isoformat() if link.created_at else None,
            })
    return out


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_membership(org_id: str, user_id: str, db: AsyncSession) -> OrgMembership:
    result = await db.execute(
        select(OrgMembership).where(OrgMembership.org_id == org_id, OrgMembership.user_id == user_id)
    )
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=403, detail="You are not a member of this organization.")
    return m

async def _assert_member(org_id: str, user_id: str, db: AsyncSession) -> OrgMembership:
    return await _get_membership(org_id, user_id, db)

async def _assert_role(org_id: str, user_id: str, allowed_roles: List[str], db: AsyncSession) -> OrgMembership:
    m = await _get_membership(org_id, user_id, db)
    if m.role not in allowed_roles:
        raise HTTPException(status_code=403, detail=f"Requires role: {' or '.join(allowed_roles)}.")
    return m

async def _assert_permission(org_id: str, user_id: str, permission: str, db: AsyncSession) -> OrgMembership:
    """RBAC entry point (see rbac.py) — resolves a permission to its allowed
    roles and delegates to _assert_role, so every permission check in this
    router reads from the one registry instead of a hardcoded role list."""
    return await _assert_role(org_id, user_id, roles_with_permission(permission), db)


# ── White-label branding (Phase 6, PRD §20) ─────────────────────────────────
# Any member may view an org's branding (it's what the org looks like to
# everyone in it); only "manage_branding" (owner-only, see rbac.py) may
# change it — it's part of the org's identity, not day-to-day
# collaboration work.

async def _get_org_or_404(org_id: str, db: AsyncSession) -> Organization:
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


@router.get("/{org_id}/branding", response_model=OrganizationBrandingOut)
async def get_branding(
    org_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _assert_member(org_id, current_user.id, db)
    org = await _get_org_or_404(org_id, db)
    return OrganizationBrandingOut(
        org_id=org.id, white_label_enabled=org.white_label_enabled,
        brand_name=org.brand_name, logo_url=org.logo_url, primary_color=org.primary_color,
    )


@router.patch("/{org_id}/branding", response_model=OrganizationBrandingOut)
async def update_branding(
    org_id: str, body: OrganizationBrandingUpdate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _assert_permission(org_id, current_user.id, "manage_branding", db)
    org = await _get_org_or_404(org_id, db)
    for field in ("white_label_enabled", "brand_name", "logo_url", "primary_color"):
        value = getattr(body, field)
        if value is not None:
            setattr(org, field, value)
    await db.flush()
    await db.refresh(org)
    await log_action(db, actor_id=current_user.id, action="org.branding_updated", org_id=org_id,
                      object_type="organization", object_id=org_id)
    await db.commit()
    return OrganizationBrandingOut(
        org_id=org.id, white_label_enabled=org.white_label_enabled,
        brand_name=org.brand_name, logo_url=org.logo_url, primary_color=org.primary_color,
    )
