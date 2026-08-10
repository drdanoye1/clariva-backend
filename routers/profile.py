"""
Company Profile router — CRUD for OrgContextDB (PI, team, facilities,
partners, past performance, and — as of Funding Opportunity Intelligence
Phase 2 — the Funding Intelligence Profile fields the Fit Score engine
scores opportunities against: mission, industries, certifications, NAICS
codes, service geography, funding preferences, entity type).

Phase 2 migrated this from strictly per-user to optionally org-owned (see
OrgContextDB's docstring in models/db_models.py): pass `?org_id=` to read
or write the shared profile for that Organization instead of the calling
user's personal one. Viewing an org's profile only requires membership;
editing it requires the `manage_company_profile` permission (owner/editor
— see rbac.py). Omitting `org_id` behaves exactly as before this
migration existed — the caller's own personal profile, invisible to
anyone else.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import OrgContextDB, User
from routers.auth import get_current_user
from routers.organizations import _assert_member, _assert_permission
from engines.company_profile import get_org_context

router = APIRouter()

# Mirrors the String(n) column caps in models.db_models.OrgContextDB.
# Saving used to hit these limits silently — SQLAlchemy/asyncpg would raise
# a raw DataError, which (with no exception handler in main.py) surfaced to
# the frontend as a bare, non-JSON 500 that just read "Save failed." with
# no indication of which field or why. Checking here up front turns that
# into a specific, actionable 422 instead.
_MAX_LENGTHS = {
    "organization_name": 255, "industry": 255,
    "uei_number": 20, "cage_code": 10,
    "ein_tax_id": 15, "duns_number": 13,
    "firm_street": 255, "firm_apt_suite": 100, "firm_city": 100,
    "firm_state": 50, "firm_zip": 12, "firm_phone": 30,
    "pi_orcid": 25, "pi_degree": 100, "pi_phone": 30,
    "bo_title": 150, "bo_phone": 30,
    "acn_title": 150, "acn_phone": 30,
    "entity_type": 30,
}


def _check_lengths(body: Dict[str, Any]) -> None:
    violations = [
        f"{field} (max {limit} characters, got {len(body[field])})"
        for field, limit in _MAX_LENGTHS.items()
        if isinstance(body.get(field), str) and len(body[field]) > limit
    ]
    if violations:
        raise HTTPException(
            status_code=422,
            detail="These fields are too long to save: " + "; ".join(violations) + ". Please shorten them and try again.",
        )


def _ctx_to_dict(ctx: OrgContextDB) -> Dict[str, Any]:
    return {
        "org_id":                ctx.org_id,
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
        # Funding Intelligence Profile (Phase 2)
        "mission_statement":    ctx.mission_statement,
        "industries":           ctx.industries or [],
        "certifications":       ctx.certifications or [],
        "naics_codes":          ctx.naics_codes or [],
        "service_geography":    ctx.service_geography or [],
        "funding_preferences":  ctx.funding_preferences or {},
        "entity_type":          ctx.entity_type,
        "pursuit_capacity":     ctx.pursuit_capacity,
        "updated_at":           ctx.updated_at.isoformat() if ctx.updated_at else None,
    }


def _empty_profile(org_id: Optional[str], default_name: Optional[str]) -> Dict[str, Any]:
    return {
        "org_id": org_id,
        "organization_name": default_name or "", "industry": "", "core_technologies": [],
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
        "mission_statement": None, "industries": [], "certifications": [],
        "naics_codes": [], "service_geography": [], "funding_preferences": {},
        "entity_type": None,
        "pursuit_capacity": None,
        "updated_at": None,
    }


@router.get("/")
async def get_profile(
    org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get a Company/Funding Intelligence Profile. `org_id` given -> the
    org-shared profile (any member may view). Omitted -> the caller's own
    personal profile, exactly as before Phase 2.
    """
    if org_id:
        await _assert_member(org_id, current_user.id, db)
    ctx = await get_org_context(db, user_id=current_user.id, org_id=org_id)
    if not ctx:
        return _empty_profile(org_id, None if org_id else current_user.organization)
    return _ctx_to_dict(ctx)


@router.put("/")
async def save_profile(
    body: Dict[str, Any],
    org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Upsert a Company/Funding Intelligence Profile. `org_id` given ->
    requires `manage_company_profile` (owner/editor) and writes the
    org-shared profile. Omitted -> the caller's personal profile, exactly
    as before Phase 2.
    """
    if org_id:
        await _assert_permission(org_id, current_user.id, "manage_company_profile", db)

    _check_lengths(body)

    ctx = await get_org_context(db, user_id=current_user.id, org_id=org_id)
    if not ctx:
        ctx = OrgContextDB(id=str(uuid.uuid4()), user_id=current_user.id, org_id=org_id)
        db.add(ctx)
    else:
        # Track the most recent editor even on an org-shared profile —
        # informational only, org_id (the actual ownership/scope key) is
        # never reassigned here.
        ctx.user_id = current_user.id

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

    # Funding Intelligence Profile (Phase 2)
    ctx.mission_statement   = body.get("mission_statement") or None
    ctx.industries          = body.get("industries", [])
    ctx.certifications      = body.get("certifications", [])
    ctx.naics_codes         = body.get("naics_codes", [])
    ctx.service_geography   = body.get("service_geography", [])
    ctx.funding_preferences = body.get("funding_preferences") or {}
    ctx.entity_type         = body.get("entity_type") or None
    ctx.pursuit_capacity    = body.get("pursuit_capacity") or None

    await db.flush()
    await db.refresh(ctx)   # reload server-set fields (updated_at) to avoid lazy-load outside greenlet
    return _ctx_to_dict(ctx)
