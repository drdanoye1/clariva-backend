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
        # Firm identity & address
        "ein_tax_id":           ctx.ein_tax_id,
        "duns_number":          ctx.duns_number,
        "firm_street":          ctx.firm_street,
        "firm_apt_suite":       ctx.firm_apt_suite,
        "firm_city":            ctx.firm_city,
        "firm_state":           ctx.firm_state,
        "firm_zip":             ctx.firm_zip,
        "firm_phone":           ctx.firm_phone,
        # Principal Investigator
        "pi_name":              ctx.pi_name,
        "pi_credentials":       ctx.pi_credentials,
        "pi_orcid":             ctx.pi_orcid,
        "pi_degree":            ctx.pi_degree,
        "pi_affiliation":       ctx.pi_affiliation,
        "pi_publications":      ctx.pi_publications,
        "pi_prior_sbir_awards": ctx.pi_prior_sbir_awards,
        "pi_email":             ctx.pi_email,
        "pi_phone":             ctx.pi_phone,
        # Business Official
        "bo_name":              ctx.bo_name,
        "bo_title":             ctx.bo_title,
        "bo_phone":             ctx.bo_phone,
        "bo_email":             ctx.bo_email,
        # Authorized Contract Negotiator
        "acn_name":             ctx.acn_name,
        "acn_title":            ctx.acn_title,
        "acn_phone":            ctx.acn_phone,
        "acn_email":            ctx.acn_email,
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
            "ein_tax_id": None, "duns_number": None,
            "firm_street": None, "firm_apt_suite": None, "firm_city": None,
            "firm_state": None, "firm_zip": None, "firm_phone": None,
            "pi_name": None, "pi_credentials": None, "pi_orcid": None,
            "pi_degree": None, "pi_affiliation": None,
            "pi_publications": None, "pi_prior_sbir_awards": None,
            "pi_email": None, "pi_phone": None,
            "bo_name": None, "bo_title": None, "bo_phone": None, "bo_email": None,
            "acn_name": None, "acn_title": None, "acn_phone": None, "acn_email": None,
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

    # Firm identity & address
    ctx.ein_tax_id      = body.get("ein_tax_id") or None
    ctx.duns_number     = body.get("duns_number") or None
    ctx.firm_street     = body.get("firm_street") or None
    ctx.firm_apt_suite  = body.get("firm_apt_suite") or None
    ctx.firm_city       = body.get("firm_city") or None
    ctx.firm_state      = body.get("firm_state") or None
    ctx.firm_zip        = body.get("firm_zip") or None
    ctx.firm_phone      = body.get("firm_phone") or None

    # PI
    ctx.pi_name              = body.get("pi_name") or None
    ctx.pi_credentials       = body.get("pi_credentials") or None
    ctx.pi_orcid             = body.get("pi_orcid") or None
    ctx.pi_degree            = body.get("pi_degree") or None
    ctx.pi_affiliation       = body.get("pi_affiliation") or None
    ctx.pi_publications      = body.get("pi_publications") or None
    ctx.pi_prior_sbir_awards = body.get("pi_prior_sbir_awards") or None
    ctx.pi_email             = body.get("pi_email") or None
    ctx.pi_phone             = body.get("pi_phone") or None

    # Business Official
    ctx.bo_name   = body.get("bo_name") or None
    ctx.bo_title  = body.get("bo_title") or None
    ctx.bo_phone  = body.get("bo_phone") or None
    ctx.bo_email  = body.get("bo_email") or None

    # Authorized Contract Negotiator
    ctx.acn_name   = body.get("acn_name") or None
    ctx.acn_title  = body.get("acn_title") or None
    ctx.acn_phone  = body.get("acn_phone") or None
    ctx.acn_email  = body.get("acn_email") or None

    # Arrays
    ctx.team_members     = body.get("team_members", [])
    ctx.facilities       = body.get("facilities", [])
    ctx.partners         = body.get("partners", [])
    ctx.past_performance = body.get("past_performance", [])

    await db.flush()
    await db.refresh(ctx)   # reload server-set fields (updated_at) to avoid lazy-load outside greenlet
    return _ctx_to_dict(ctx)
