"""
Admin router — superadmin-only endpoints.
Access: /api/v1/admin/*
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models.db_models import Organization, User, Proposal
from models.schemas import OrgAdminOut, OrgPlanUpdateRequest
from routers.auth import get_current_user
from audit import log_action

router = APIRouter()


# ── Superadmin dependency ─────────────────────────────────────────────────────

async def require_superadmin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_superadmin:
        raise HTTPException(status_code=403, detail="Superadmin access required")
    return current_user


# ── Schemas ───────────────────────────────────────────────────────────────────

class AdminUserOut(BaseModel):
    id: str
    email: str
    full_name: str
    organization: str
    role: str
    subscription_plan: str
    is_superadmin: bool
    is_active: bool

    model_config = {"from_attributes": True}


class UpdateUserRequest(BaseModel):
    role: Optional[str] = None                 # user | admin | superadmin
    subscription_plan: Optional[str] = None    # free | starter | pro | enterprise
    is_active: Optional[bool] = None
    is_superadmin: Optional[bool] = None


class StatsOut(BaseModel):
    total_users: int
    total_proposals: int
    active_users: int
    superadmins: int


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=StatsOut)
async def get_stats(
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    total_users    = (await db.execute(select(func.count(User.id)))).scalar()
    active_users   = (await db.execute(select(func.count(User.id)).where(User.is_active == True))).scalar()
    superadmins    = (await db.execute(select(func.count(User.id)).where(User.is_superadmin == True))).scalar()
    total_proposals = (await db.execute(select(func.count(Proposal.id)))).scalar()
    return StatsOut(
        total_users=total_users,
        total_proposals=total_proposals,
        active_users=active_users,
        superadmins=superadmins,
    )


@router.get("/users", response_model=List[AdminUserOut])
async def list_users(
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return result.scalars().all()


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: str,
    body: UpdateUserRequest,
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if body.role is not None:
        user.role = body.role
    if body.subscription_plan is not None:
        user.subscription_plan = body.subscription_plan
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.is_superadmin is not None:
        user.is_superadmin = body.is_superadmin

    await db.flush()
    await db.refresh(user)
    return user


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await db.delete(user)
    await db.flush()


# ── Organization plan overrides (Phase 3.1 — Configurable Pricing Controls) ──
# Organization.plan drives ComplimentaryAllowance lookups (see
# engines/service_catalog_engine.py) but has no self-serve write path yet —
# Square checkout completion doesn't grant it automatically (a known,
# documented gap; see docs/ARCHITECTURE.md's pricing-overhaul section).
# Until that billing integration exists, this is the only way to set an
# org's plan, e.g. to manually reflect an offline/invoiced upgrade.

@router.get("/organizations", response_model=List[OrgAdminOut])
async def admin_list_organizations(
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Organization).order_by(Organization.name))
    return result.scalars().all()


@router.patch("/organizations/{org_id}/plan", response_model=OrgAdminOut)
async def admin_update_org_plan(
    org_id: str,
    body: OrgPlanUpdateRequest,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    old_plan = org.plan
    org.plan = body.plan
    await db.flush()
    await db.refresh(org)

    await log_action(
        db, actor_id=current_user.id, action="admin.organization.plan_updated",
        org_id=org_id, object_type="organization", object_id=org_id,
        detail={"old_plan": old_plan, "new_plan": body.plan},
    )
    return org
