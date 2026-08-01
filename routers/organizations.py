"""Organizations router — create org, invite members, share proposals."""

from __future__ import annotations
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models.db_models import Organization, OrgMembership, OrgProposal, Proposal, User
from routers.auth import get_current_user

router = APIRouter()


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

class ShareProposalRequest(BaseModel):
    proposal_id: str


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
    """Invite a registered user to the organization by email."""
    await _assert_role(org_id, current_user.id, ["owner", "editor"], db)

    VALID_ROLES = {"owner", "editor", "viewer"}
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Choose: {', '.join(VALID_ROLES)}")

    # Look up the invitee
    target_result = await db.execute(select(User).where(User.email == body.email))
    target = target_result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail=f"No user found with email '{body.email}'. They must register first.")

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
    await db.flush()

    return {"message": f"{target.full_name} added as {body.role}.", "user_id": target.id}


@router.delete("/{org_id}/members/{user_id}", status_code=204)
async def remove_member(
    org_id: str,
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_role(org_id, current_user.id, ["owner"], db)
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot remove yourself. Transfer ownership first.")
    result = await db.execute(
        select(OrgMembership).where(OrgMembership.org_id == org_id, OrgMembership.user_id == user_id)
    )
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Member not found.")
    await db.delete(m)


@router.post("/{org_id}/proposals")
async def share_proposal(
    org_id: str,
    body: ShareProposalRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Share one of the user's proposals with the organization."""
    await _assert_role(org_id, current_user.id, ["owner", "editor"], db)

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
