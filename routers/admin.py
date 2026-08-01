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
from models.db_models import User, Proposal
from routers.auth import get_current_user

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
