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
from models.db_models import (
    AICreditLedger, ModelPricingConfig, Organization, PlatformCostConfig, User, Proposal,
)
from models.schemas import (
    AgencyProfileResolvedOut, AgencyProfileVersionCreate, AgencyProfileVersionOut,
    CreditBalanceOut, CreditTopupRequest, EngineeringEconomicsDashboardOut,
    ModelPricingConfigOut, ModelPricingConfigUpdate, OrgAdminOut, OrgPlanUpdateRequest,
    PlatformCostConfigOut, PlatformCostConfigUpdate,
)
from routers.auth import get_current_user
from routers.credits import _balance_out
from engines.credit_engine import CreditEngine, DEFAULT_STARTING_BALANCE
from engines.engineering_economics_engine import EngineeringEconomicsEngine
from engines.agency_profile_engine import AgencyProfileEngine
from audit import log_action

router = APIRouter()
credit_engine = CreditEngine()
economics_engine = EngineeringEconomicsEngine()
agency_profile_engine = AgencyProfileEngine()


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
    orgs = result.scalars().all()

    # Bulk-fetch existing ledgers rather than calling
    # credit_engine.get_or_create_ledger() per org — that method creates a
    # row on first call, and a read-only list endpoint must never have that
    # side effect. An org with no ledger yet just shows the same
    # DEFAULT_STARTING_BALANCE it would actually get the first time
    # anything touches its ledger for real.
    ledger_result = await db.execute(
        select(AICreditLedger).where(AICreditLedger.org_id.in_([o.id for o in orgs]))
    )
    balances = {l.org_id: l.balance for l in ledger_result.scalars().all()}

    # Bulk-fetch creators too, same pattern as the ledger fetch above —
    # this is what disambiguates two orgs that happen to share a display
    # name (a real, expected case: nothing stops two different signups
    # both being called "Acme Inc"). Surfaced in every UI that lists orgs
    # by name alone, e.g. the Commission Ledger's manual-entry org picker.
    creator_result = await db.execute(select(User).where(User.id.in_([o.created_by for o in orgs])))
    creator_emails = {u.id: u.email for u in creator_result.scalars().all()}

    return [
        OrgAdminOut(
            id=o.id, name=o.name, plan=o.plan, created_at=o.created_at,
            ai_credit_balance=balances.get(o.id, DEFAULT_STARTING_BALANCE),
            created_by_email=creator_emails.get(o.created_by),
        )
        for o in orgs
    ]


# ── AI Services credit grants (superadmin) ────────────────────────────────────
# routers/credits.py's topup_credits is owner-only (manage_credits) — a
# superadmin with no membership in a given org can't use it. This is the
# platform-admin lever for the same underlying CreditEngine.credit() call:
# comping a customer's balance for support, or funding an internal testing
# org (e.g. one used to exercise paid AI features like Analyze Opportunity,
# Bulk Opportunity Intelligence, or Funding Strategy Intelligence without
# hitting the $100 default starting balance).

@router.get("/organizations/{org_id}/credits", response_model=CreditBalanceOut)
async def admin_get_org_credits(
    org_id: str,
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Organization not found")
    ledger = await credit_engine.get_or_create_ledger(db, org_id)
    return _balance_out(org_id, ledger)


@router.post("/organizations/{org_id}/credits/grant", response_model=CreditBalanceOut)
async def admin_grant_org_credits(
    org_id: str,
    body: CreditTopupRequest,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """Adds `body.amount` to the org's AI Services balance — same
    CreditEngine.credit() call topup_credits uses, just reachable without
    org membership. `body.reason` defaults to "manual_topup"; pass
    something identifying (e.g. "admin testing allocation") so the
    transaction log stays legible."""
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Organization not found")

    ledger = await credit_engine.credit(db, org_id, body.amount, body.reason, user_id=current_user.id)

    await log_action(
        db, actor_id=current_user.id, action="admin.organization.credits_granted",
        org_id=org_id, object_type="ai_credit_ledger", object_id=ledger.id,
        detail={"amount": body.amount, "reason": body.reason},
    )
    return _balance_out(org_id, ledger)


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


# ── Engineering Economics (Phase 3 §4.7) ─────────────────────────────────────
# Read-only dashboard over engines/engineering_economics_engine.py, plus
# admin-editable per-model token pricing and platform cost-config — the
# same "configurable, not hardcoded" discipline as Phase 3.1's service
# catalog pricing controls above.

@router.get("/economics", response_model=EngineeringEconomicsDashboardOut)
async def admin_get_economics(
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    return await economics_engine.get_dashboard(db)


@router.get("/model-pricing", response_model=List[ModelPricingConfigOut])
async def admin_list_model_pricing(
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    return await economics_engine.get_model_pricing(db)


@router.patch("/model-pricing/{model}", response_model=ModelPricingConfigOut)
async def admin_update_model_pricing(
    model: str,
    body: ModelPricingConfigUpdate,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(ModelPricingConfig).where(ModelPricingConfig.model == model))
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"No pricing configured for model '{model}'.")

    changes: dict = {}
    if body.input_cost_cents_per_1k is not None:
        changes["input_cost_cents_per_1k"] = (row.input_cost_cents_per_1k, body.input_cost_cents_per_1k)
        row.input_cost_cents_per_1k = body.input_cost_cents_per_1k
    if body.output_cost_cents_per_1k is not None:
        changes["output_cost_cents_per_1k"] = (row.output_cost_cents_per_1k, body.output_cost_cents_per_1k)
        row.output_cost_cents_per_1k = body.output_cost_cents_per_1k

    await db.flush()
    await db.refresh(row)

    if changes:
        await log_action(
            db, actor_id=current_user.id, action="admin.model_pricing.updated",
            object_type="model_pricing_config", object_id=row.id,
            detail={k: {"old": v[0], "new": v[1]} for k, v in changes.items()},
        )
    return row


@router.get("/cost-config", response_model=List[PlatformCostConfigOut])
async def admin_list_cost_config(
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    return await economics_engine.get_platform_cost_config(db)


@router.patch("/cost-config/{key}", response_model=PlatformCostConfigOut)
async def admin_update_cost_config(
    key: str,
    body: PlatformCostConfigUpdate,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(PlatformCostConfig).where(PlatformCostConfig.key == key))
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"No cost config entry '{key}'.")

    old_value = row.value_cents
    row.value_cents = body.value_cents
    await db.flush()
    await db.refresh(row)

    await log_action(
        db, actor_id=current_user.id, action="admin.cost_config.updated",
        object_type="platform_cost_config", object_id=row.key,
        detail={"old_value_cents": old_value, "new_value_cents": body.value_cents},
    )
    return row


# ── Agency-Profile Resolver (CLARIVA-DOCGEN-SPEC-001, Phase 7) ─────────────────
# Admin-editable, versioned per-agency guidance text and page limits — see
# engines/agency_profile_engine.py's module docstring for the full design
# rationale. Superadmin-only, same require_superadmin/log_action convention
# as the pricing endpoints above.

@router.get("/agency-profiles", response_model=List[AgencyProfileResolvedOut])
async def admin_list_agency_profiles(
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """One resolved (merged with hardcoded defaults) row per known agency —
    the admin console's agency picker/overview."""
    return await agency_profile_engine.list_agencies_summary(db)


@router.get("/agency-profiles/{agency_code}", response_model=AgencyProfileResolvedOut)
async def admin_get_agency_profile(
    agency_code: str,
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    return await agency_profile_engine.resolve(db, agency_code)


@router.get("/agency-profiles/{agency_code}/versions", response_model=List[AgencyProfileVersionOut])
async def admin_list_agency_profile_versions(
    agency_code: str,
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """Full version history for one agency, newest first — audit/rollback view."""
    return await agency_profile_engine.list_versions(db, agency_code)


@router.post("/agency-profiles/{agency_code}/versions", response_model=AgencyProfileVersionOut)
async def admin_create_agency_profile_version(
    agency_code: str,
    body: AgencyProfileVersionCreate,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """Append-only — never mutates an existing version. Pass activate=true
    to make this the live version immediately (one request instead of two
    for the common case)."""
    row = await agency_profile_engine.create_version(
        db, agency_code=agency_code,
        guidance_text=body.guidance_text, section_limits=body.section_limits,
        total_page_limit=body.total_page_limit, notes=body.notes,
        created_by_user_id=current_user.id, activate=body.activate,
    )
    await log_action(
        db, actor_id=current_user.id, action="admin.agency_profile.version_created",
        object_type="agency_profile", object_id=row.id,
        detail={"agency_code": row.agency_code, "version": row.version, "activated": body.activate},
    )
    return row


@router.post("/agency-profiles/{agency_code}/versions/{version}/activate", response_model=AgencyProfileVersionOut)
async def admin_activate_agency_profile_version(
    agency_code: str,
    version: int,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    row = await agency_profile_engine.activate_version(db, agency_code, version)
    await log_action(
        db, actor_id=current_user.id, action="admin.agency_profile.version_activated",
        object_type="agency_profile", object_id=row.id,
        detail={"agency_code": row.agency_code, "version": row.version},
    )
    return row


@router.post("/agency-profiles/seed-defaults", response_model=List[AgencyProfileVersionOut])
async def admin_seed_agency_profile_defaults(
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """Idempotent — creates and activates version 1 (seeded verbatim from the
    hardcoded AGENCY_GUIDANCE/AGENCY_SECTION_LIMITS/AGENCY_TOTAL_LIMITS
    dicts) for every agency that has no AgencyProfile row yet. Never
    overwrites an agency that already has one. Lets an admin move an agency
    from "silent code fallback" to "editable DB row" without hand-copying
    the hardcoded guidance text."""
    created = await agency_profile_engine.seed_defaults(db, created_by_user_id=current_user.id)
    if created:
        await log_action(
            db, actor_id=current_user.id, action="admin.agency_profile.defaults_seeded",
            object_type="agency_profile", object_id=None,
            detail={"agency_codes": [row.agency_code for row in created]},
        )
    return created
