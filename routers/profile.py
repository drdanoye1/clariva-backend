"""Company Profile router — CRUD for OrgContextDB (PI, team, facilities, partners, past performance)."""

from __future__ import annotations

import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models.db_models import OrgContextDB, User
from routers.auth import get_current_user

router = APIRouter()


def _ctx_to_dict(ctx: OrgContextDB) -> Dict[str, Any]:
    return {
        "organization_name":    ctx.organization_name,
        "industry":             ctx.industry,
        "core_technologies":    ctx.core_technologies or [],
        "prior_sbir_experience": ctx.prior_sbir_experience or False,
        "uei_number":           ctx.uei_number,
        "cage_code":            ctx.cage_code,
        "company_capabilities": ctx.company_capabilities,
        "pi_name":              ctx.pi_name,
        "pi_credentials":       ctx.pi_credentials,
        "pi_orcid":             ctx.pi_orcid,
        "pi_degree":            ctx.pi_degree,
        "pi_affiliation":       ctx.pi_affiliation,
        "pi_publications":      ctx.pi_publications,
        "pi_prior_sbir_awards": ctx.pi_prior_sbir_awards,
        "team_members":         ctx.team_members or [],
        "facilities":           ctx.facilities or [],
        "partners":             ctx.partners or [],
        "past_performance":     ctx.past_performance or [],
        "updated_at":           ctx.updated_at.isoformat() if ctx.updated_at else None,
    }


@router.get("/")
async def get_profile(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get the current user's company profile."""
    result = await db.execute(
        select(OrgContextDB).where(OrgContextDB.user_id == current_user.id)
    )
    ctx = result.scalar_one_or_none()
    if not ctx:
        # Return empty profile
        return {
            "organization_name": current_user.organization,
            "industry": "", "core_technologies": [],
            "prior_sbir_experience": False,
            "uei_number": None, "cage_code": None, "company_capabilities": None,
            "pi_name": None, "pi_credentials": None, "pi_orcid": None,
            "pi_degree": None, "pi_affiliation": None,
            "pi_publications": None, "pi_prior_sbir_awards": None,
            "team_members": [], "facilities": [], "partners": [], "past_performance": [],
            "updated_at": None,
        }
    return _ctx_to_dict(ctx)


@router.put("/")
async def save_profile(
    body: Dict[str, Any],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upsert the full company profile."""
    result = await db.execute(
        select(OrgContextDB).where(OrgContextDB.user_id == current_user.id)
    )
    ctx = result.scalar_one_or_none()

    if not ctx:
        ctx = OrgContextDB(id=str(uuid.uuid4()), user_id=current_user.id)
        db.add(ctx)

    # Overview
    ctx.organization_name     = body.get("organization_name", "")
    ctx.industry              = body.get("industry", "")
    ctx.core_technologies     = body.get("core_technologies", [])
    ctx.prior_sbir_experience = body.get("prior_sbir_experience", False)
    ctx.uei_number            = body.get("uei_number") or None
    ctx.cage_code             = body.get("cage_code") or None
    ctx.company_capabilities  = body.get("company_capabilities") or None

    # PI
    ctx.pi_name              = body.get("pi_name") or None
    ctx.pi_credentials       = body.get("pi_credentials") or None
    ctx.pi_orcid             = body.get("pi_orcid") or None
    ctx.pi_degree            = body.get("pi_degree") or None
    ctx.pi_affiliation       = body.get("pi_affiliation") or None
    ctx.pi_publications      = body.get("pi_publications") or None
    ctx.pi_prior_sbir_awards = body.get("pi_prior_sbir_awards") or None

    # Arrays
    ctx.team_members     = body.get("team_members", [])
    ctx.facilities       = body.get("facilities", [])
    ctx.partners         = body.get("partners", [])
    ctx.past_performance = body.get("past_performance", [])

    await db.flush()
    await db.refresh(ctx)   # reload server-set fields (updated_at) to avoid lazy-load outside greenlet
    return _ctx_to_dict(ctx)
