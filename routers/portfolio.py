"""
Portfolio Dashboard router (Version 3.0 architecture upgrade, Phase 12).

A single read-only endpoint — no create/update/delete surface, unlike most
routers in this app — since PortfolioEngine only aggregates data that
already has its own CRUD elsewhere (proposals, awards, conditions,
credits). Same org_ids-resolution pattern as
routers/awards.py::list_my_awards, so "everything I can see" means the
same thing here as it does on the Awards portfolio page.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from engines.portfolio_engine import PortfolioEngine
from models.db_models import OrgMembership, User
from models.schemas import PortfolioSummaryOut
from routers.auth import get_current_user

router = APIRouter()
engine = PortfolioEngine()


@router.get("/summary", response_model=PortfolioSummaryOut)
async def get_portfolio_summary(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    memberships = await db.execute(select(OrgMembership).where(OrgMembership.user_id == current_user.id))
    org_ids = [m.org_id for m in memberships.scalars().all()]
    return await engine.get_summary(db, current_user.id, org_ids)
