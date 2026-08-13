"""
Partner Center router — Channel Partner Program, Phase 1 MVP + Partner
Portal (read-only partner-facing dashboard).
Access: /api/v1/partners/*

Three audiences now, sharply split:
  - POST /partners/apply is the only fully-unauthenticated endpoint — it's
    the real backend for frontend/src/pages/partners.tsx's "Apply to
    Become a Partner" form.
  - `/partners/admin/*` is gated behind `require_superadmin` (imported from
    routers.admin, same dependency every other admin-only endpoint in this
    codebase uses) — the Admin Console half of Version 2's spec.
  - `/partners/portal/*` is the new Partner Portal: two public endpoints
    (`GET /portal/invite/{token}`, `POST /portal/invite/{token}/accept`) for
    the invite-claim flow — a prospective portal user has no Clariva
    account yet, same reasoning as routers/invitations.py being
    unauthenticated — plus authenticated `GET /portal/me`,
    `/portal/overview`, `/portal/customers`, `/portal/commission-entries`,
    `/portal/payouts`, all gated behind the new `require_partner_portal_
    access` dependency below (reuses the existing Clariva User/JWT system
    via `get_current_user` + `Partner.user_id`, rather than a separate
    partner-auth system). Every portal query is scoped server-side to the
    calling partner's own `partner_id` — a partner can never pass another
    partner's id and see their data.

See engines/partner_engine.py for all business logic; this file is routing
+ schemas + permission gates only, matching this codebase's engine/router
split everywhere else.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_db
from models.db_models import Partner, User
from models.schemas import LoginResponse
from routers.admin import require_superadmin
from routers.auth import _issue_tokens, get_current_user, hash_password
from email_service import render_partner_portal_invite_email, send_email
from engines.partner_engine import (
    AttributionNotFoundError, CommissionEntryNotFoundError, DealRegistrationNotFoundError,
    DuplicateClaimError, PartnerAccountExistsError, PartnerEngine, PartnerNotFoundError,
    PortalInviteNotFoundError,
)
from audit import log_action

router = APIRouter()
partner_engine = PartnerEngine()


async def require_partner_portal_access(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> Partner:
    """Portal-equivalent of require_superadmin — resolves the calling
    User to their linked Partner row (via Partner.user_id) and 403s if
    there isn't one, rather than gating on a role flag. A partner whose
    account was later suspended still resolves here (they can see their
    own history) but the frontend surfaces the suspended status; nothing
    in this MVP hides ledger data from a suspended partner."""
    partner = await partner_engine.get_partner_by_user_id(db, current_user.id)
    if partner is None:
        raise HTTPException(status_code=403, detail="This account has no linked Partner Portal access.")
    return partner


# ── Schemas ────────────────────────────────────────────────────────────────

class PartnerApplyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)          # organization/firm name
    contact_name: str = Field(..., min_length=1, max_length=255)
    contact_email: EmailStr
    partner_type: Optional[str] = None
    message: Optional[str] = None

class PartnerOut(BaseModel):
    id: str
    name: str
    contact_name: str
    contact_email: str
    partner_type: Optional[str] = None
    notes: Optional[str] = None
    status: str
    referral_code: Optional[str] = None
    program_status: str
    reviewed_at: Optional[str] = None
    created_at: Optional[str] = None
    user_id: Optional[str] = None  # set once linked to a Partner Portal login — see Partner.user_id's docstring

class PartnerRejectRequest(BaseModel):
    reason: Optional[str] = None

class ProgramStatusUpdateRequest(BaseModel):
    program_status: str  # registered | silver | gold | platinum

class DealRegistrationCreateRequest(BaseModel):
    partner_id: str
    organization_name: str = Field(..., min_length=1, max_length=255)
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    domain: Optional[str] = None
    proposed_plan: Optional[str] = None
    estimated_value_cents: Optional[int] = Field(default=None, ge=0)
    notes: Optional[str] = None

class DealRegistrationOut(BaseModel):
    id: str
    partner_id: str
    organization_name: str
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    domain: Optional[str] = None
    proposed_plan: Optional[str] = None
    estimated_value_cents: Optional[int] = None
    approved_commissionable_value_cents: Optional[int] = None
    stage: str
    status: str
    protection_expires_at: Optional[str] = None
    created_at: Optional[str] = None

class DealApproveRequest(BaseModel):
    protection_days: Optional[int] = Field(default=None, ge=1, le=730)
    approved_commissionable_value_cents: Optional[int] = Field(default=None, ge=0)

class DealRejectRequest(BaseModel):
    reason: Optional[str] = None

class CommissionRuleOut(BaseModel):
    id: str
    months_1_12_rate: float
    months_13_24_rate: float
    months_25_36_rate: float
    month_37_plus_rate: float

class CommissionRuleUpdateRequest(BaseModel):
    months_1_12_rate: float = Field(..., ge=0, le=1)
    months_13_24_rate: float = Field(..., ge=0, le=1)
    months_25_36_rate: float = Field(..., ge=0, le=1)
    month_37_plus_rate: float = Field(..., ge=0, le=1)

class CommissionEntryOut(BaseModel):
    id: str
    partner_id: str
    organization_id: str
    payment_reference: Optional[str] = None
    plan_id: Optional[str] = None
    qualifying_revenue_cents: int
    months_since_attribution: int
    commission_rate: float
    commission_amount_cents: int
    status: str
    is_adjustment: bool
    created_at: Optional[str] = None

class CommissionStatusUpdateRequest(BaseModel):
    status: str  # pending | approved | available | paid | reversed

class ManualCommissionRequest(BaseModel):
    organization_id: str
    qualifying_revenue_cents: int = Field(..., ge=0)
    payment_reference: Optional[str] = None
    plan_id: Optional[str] = None

class PayoutCreateRequest(BaseModel):
    partner_id: str
    commission_entry_ids: List[str] = Field(..., min_length=1)
    notes: Optional[str] = None

class PayoutOut(BaseModel):
    id: str
    partner_id: str
    amount_cents: int
    commission_entry_ids: List[str]
    status: str
    notes: Optional[str] = None
    created_at: Optional[str] = None

class ChannelOverviewOut(BaseModel):
    active_partners: int
    pending_applications: int
    suspended_partners: int
    attributed_customers: int
    pending_deal_registrations: int
    outstanding_commission_liability_cents: int
    total_commission_paid_cents: int


# ── Partner Portal schemas ───────────────────────────────────────────────────

class PortalInviteCreateOut(BaseModel):
    linked_existing_user: bool
    email_sent: bool
    message: str

class PortalInvitePreviewOut(BaseModel):
    partner_name: str
    email: str
    valid: bool
    reason: Optional[str] = None  # "expired" | "revoked" | "accepted" when valid=False

class PortalInviteAcceptRequest(BaseModel):
    full_name: str
    password: str

class PartnerPortalMeOut(BaseModel):
    id: str
    name: str
    contact_name: str
    contact_email: str
    status: str
    referral_code: Optional[str] = None
    program_status: str

class PartnerPortalOverviewOut(BaseModel):
    active_customers: int
    attributed_subscription_revenue_cents: int
    commission_this_month_cents: int
    available_for_payout_cents: int
    lifetime_earnings_cents: int

class PartnerPortalCustomerOut(BaseModel):
    organization_id: str
    organization_name: str
    attribution_type: str
    attributed_at: Optional[str] = None


def _partner_out(p) -> PartnerOut:
    return PartnerOut(
        id=p.id, name=p.name, contact_name=p.contact_name, contact_email=p.contact_email,
        partner_type=p.partner_type, notes=p.notes, status=p.status, referral_code=p.referral_code,
        program_status=p.program_status,
        reviewed_at=p.reviewed_at.isoformat() if p.reviewed_at else None,
        created_at=p.created_at.isoformat() if p.created_at else None,
        user_id=p.user_id,
    )

def _deal_out(d) -> DealRegistrationOut:
    return DealRegistrationOut(
        id=d.id, partner_id=d.partner_id, organization_name=d.organization_name,
        contact_name=d.contact_name, contact_email=d.contact_email, domain=d.domain,
        proposed_plan=d.proposed_plan, estimated_value_cents=d.estimated_value_cents,
        approved_commissionable_value_cents=d.approved_commissionable_value_cents,
        stage=d.stage, status=d.status,
        protection_expires_at=d.protection_expires_at.isoformat() if d.protection_expires_at else None,
        created_at=d.created_at.isoformat() if d.created_at else None,
    )

def _rule_out(r) -> CommissionRuleOut:
    return CommissionRuleOut(
        id=r.id, months_1_12_rate=r.months_1_12_rate, months_13_24_rate=r.months_13_24_rate,
        months_25_36_rate=r.months_25_36_rate, month_37_plus_rate=r.month_37_plus_rate,
    )

def _entry_out(e) -> CommissionEntryOut:
    return CommissionEntryOut(
        id=e.id, partner_id=e.partner_id, organization_id=e.organization_id,
        payment_reference=e.payment_reference, plan_id=e.plan_id,
        qualifying_revenue_cents=e.qualifying_revenue_cents, months_since_attribution=e.months_since_attribution,
        commission_rate=e.commission_rate, commission_amount_cents=e.commission_amount_cents,
        status=e.status, is_adjustment=e.is_adjustment,
        created_at=e.created_at.isoformat() if e.created_at else None,
    )

def _payout_out(p) -> PayoutOut:
    return PayoutOut(
        id=p.id, partner_id=p.partner_id, amount_cents=p.amount_cents,
        commission_entry_ids=p.commission_entry_ids or [], status=p.status, notes=p.notes,
        created_at=p.created_at.isoformat() if p.created_at else None,
    )


# ── Public: apply ──────────────────────────────────────────────────────────

@router.post("/apply", response_model=PartnerOut, status_code=201)
async def apply_to_become_partner(body: PartnerApplyRequest, db: AsyncSession = Depends(get_db)):
    partner = await partner_engine.apply(
        db, name=body.name, contact_name=body.contact_name, contact_email=body.contact_email,
        partner_type=body.partner_type, notes=body.message,
    )
    # No actor_id — this is an unauthenticated public form, same reasoning
    # as the Square webhook's actor_id being optional (audit.log_action
    # requires a real actor_id, and there isn't one here).
    return _partner_out(partner)


# ── Admin: partner applications ────────────────────────────────────────────

@router.get("/admin/applications", response_model=List[PartnerOut])
async def admin_list_applications(
    status: Optional[str] = None,
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    partners = await partner_engine.list_partners(db, status=status)
    return [_partner_out(p) for p in partners]


@router.post("/admin/applications/{partner_id}/approve", response_model=PartnerOut)
async def admin_approve_application(
    partner_id: str,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    try:
        partner = await partner_engine.approve_partner(db, partner_id, current_user.id)
    except PartnerNotFoundError:
        raise HTTPException(status_code=404, detail="Partner not found.")
    await log_action(
        db, actor_id=current_user.id, action="partner.application.approved",
        object_type="partner", object_id=partner.id, detail={"referral_code": partner.referral_code},
    )
    return _partner_out(partner)


@router.post("/admin/applications/{partner_id}/reject", response_model=PartnerOut)
async def admin_reject_application(
    partner_id: str, body: PartnerRejectRequest,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    try:
        partner = await partner_engine.reject_partner(db, partner_id, current_user.id, reason=body.reason)
    except PartnerNotFoundError:
        raise HTTPException(status_code=404, detail="Partner not found.")
    await log_action(
        db, actor_id=current_user.id, action="partner.application.rejected",
        object_type="partner", object_id=partner.id, detail={"reason": body.reason},
    )
    return _partner_out(partner)


@router.post("/admin/applications/{partner_id}/suspend", response_model=PartnerOut)
async def admin_suspend_partner(
    partner_id: str, body: PartnerRejectRequest,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    try:
        partner = await partner_engine.suspend_partner(db, partner_id, current_user.id, reason=body.reason)
    except PartnerNotFoundError:
        raise HTTPException(status_code=404, detail="Partner not found.")
    await log_action(
        db, actor_id=current_user.id, action="partner.suspended",
        object_type="partner", object_id=partner.id, detail={"reason": body.reason},
    )
    return _partner_out(partner)


@router.put("/admin/applications/{partner_id}/program-status", response_model=PartnerOut)
async def admin_set_program_status(
    partner_id: str, body: ProgramStatusUpdateRequest,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    try:
        partner = await partner_engine.set_program_status(db, partner_id, body.program_status)
    except PartnerNotFoundError:
        raise HTTPException(status_code=404, detail="Partner not found.")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await log_action(
        db, actor_id=current_user.id, action="partner.program_status_changed",
        object_type="partner", object_id=partner.id, detail={"program_status": body.program_status},
    )
    return _partner_out(partner)


@router.get("/admin/applications/{partner_id}/customers", response_model=List[PartnerPortalCustomerOut])
async def admin_list_partner_customers(
    partner_id: str,
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """Surfaces a partner's already-attributed customers (real signups tied
    to their referral code — see PartnerEngine.list_attributed_customers)
    directly in the Admin Console, so an admin creating a manual commission
    entry doesn't have to separately look up an Organization's id via
    GET /admin/organizations. Reuses the same engine method and response
    shape the Partner Portal's own GET /portal/customers uses."""
    try:
        await partner_engine.get_partner(db, partner_id)
    except PartnerNotFoundError:
        raise HTTPException(status_code=404, detail="Partner not found.")
    customers = await partner_engine.list_attributed_customers(db, partner_id)
    return [
        PartnerPortalCustomerOut(
            organization_id=c["organization_id"], organization_name=c["organization_name"],
            attribution_type=c["attribution_type"],
            attributed_at=c["attributed_at"].isoformat() if c["attributed_at"] else None,
        )
        for c in customers
    ]


# ── Admin: deal registrations ───────────────────────────────────────────────

@router.post("/admin/deal-registrations", response_model=DealRegistrationOut, status_code=201)
async def admin_create_deal_registration(
    body: DealRegistrationCreateRequest,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """Deal registrations are created here, by admin, on a partner's
    behalf — there is no authenticated Partner Portal yet for a partner to
    self-register a deal (see module docstring)."""
    try:
        deal = await partner_engine.register_deal(
            db, partner_id=body.partner_id, organization_name=body.organization_name,
            contact_name=body.contact_name, contact_email=body.contact_email, domain=body.domain,
            proposed_plan=body.proposed_plan, estimated_value_cents=body.estimated_value_cents,
            notes=body.notes,
        )
    except PartnerNotFoundError:
        raise HTTPException(status_code=404, detail="Partner not found.")
    except DuplicateClaimError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await log_action(
        db, actor_id=current_user.id, action="partner.deal_registration.created",
        object_type="deal_registration", object_id=deal.id,
        detail={"partner_id": deal.partner_id, "organization_name": deal.organization_name},
    )
    return _deal_out(deal)


@router.get("/admin/deal-registrations", response_model=List[DealRegistrationOut])
async def admin_list_deal_registrations(
    partner_id: Optional[str] = None, status: Optional[str] = None,
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    deals = await partner_engine.list_deals(db, partner_id=partner_id, status=status)
    return [_deal_out(d) for d in deals]


@router.post("/admin/deal-registrations/{deal_id}/approve", response_model=DealRegistrationOut)
async def admin_approve_deal_registration(
    deal_id: str, body: DealApproveRequest,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    try:
        deal = await partner_engine.approve_deal(
            db, deal_id, current_user.id, protection_days=body.protection_days,
            approved_commissionable_value_cents=body.approved_commissionable_value_cents,
        )
    except DealRegistrationNotFoundError:
        raise HTTPException(status_code=404, detail="Deal registration not found.")
    await log_action(
        db, actor_id=current_user.id, action="partner.deal_registration.approved",
        object_type="deal_registration", object_id=deal.id,
        detail={"protection_expires_at": deal.protection_expires_at.isoformat() if deal.protection_expires_at else None},
    )
    return _deal_out(deal)


@router.post("/admin/deal-registrations/{deal_id}/reject", response_model=DealRegistrationOut)
async def admin_reject_deal_registration(
    deal_id: str, body: DealRejectRequest,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    try:
        deal = await partner_engine.reject_deal(db, deal_id, current_user.id, reason=body.reason)
    except DealRegistrationNotFoundError:
        raise HTTPException(status_code=404, detail="Deal registration not found.")
    await log_action(
        db, actor_id=current_user.id, action="partner.deal_registration.rejected",
        object_type="deal_registration", object_id=deal.id, detail={"reason": body.reason},
    )
    return _deal_out(deal)


# ── Admin: commission rule (Program Settings) ───────────────────────────────

@router.get("/admin/commission-rule", response_model=CommissionRuleOut)
async def admin_get_commission_rule(_: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    rule = await partner_engine.get_active_commission_rule(db)
    return _rule_out(rule)


@router.put("/admin/commission-rule", response_model=CommissionRuleOut)
async def admin_update_commission_rule(
    body: CommissionRuleUpdateRequest,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    rule = await partner_engine.update_commission_rule(
        db, months_1_12_rate=body.months_1_12_rate, months_13_24_rate=body.months_13_24_rate,
        months_25_36_rate=body.months_25_36_rate, month_37_plus_rate=body.month_37_plus_rate,
        actor_id=current_user.id,
    )
    await log_action(
        db, actor_id=current_user.id, action="partner.commission_rule.updated",
        object_type="commission_rule", object_id=rule.id,
        detail={
            "months_1_12_rate": rule.months_1_12_rate, "months_13_24_rate": rule.months_13_24_rate,
            "months_25_36_rate": rule.months_25_36_rate, "month_37_plus_rate": rule.month_37_plus_rate,
        },
    )
    return _rule_out(rule)


# ── Admin: commission ledger ─────────────────────────────────────────────────

@router.get("/admin/commission-entries", response_model=List[CommissionEntryOut])
async def admin_list_commission_entries(
    partner_id: Optional[str] = None, status: Optional[str] = None,
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    entries = await partner_engine.list_commission_entries(db, partner_id=partner_id, status=status)
    return [_entry_out(e) for e in entries]


@router.put("/admin/commission-entries/{entry_id}/status", response_model=CommissionEntryOut)
async def admin_update_commission_entry_status(
    entry_id: str, body: CommissionStatusUpdateRequest,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    try:
        entry = await partner_engine.update_commission_entry_status(db, entry_id, body.status)
    except CommissionEntryNotFoundError:
        raise HTTPException(status_code=404, detail="Commission entry not found.")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await log_action(
        db, actor_id=current_user.id, action="partner.commission_entry.status_changed",
        object_type="commission_entry", object_id=entry.id, detail={"status": body.status},
    )
    return _entry_out(entry)


@router.post("/admin/commission-entries/manual", response_model=CommissionEntryOut, status_code=201)
async def admin_create_manual_commission_entry(
    body: ManualCommissionRequest,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """For Enterprise deals (or any other manually-invoiced case) not
    reachable through the automatic Square-webhook path — see
    engines/partner_engine.py::record_manual_commission's docstring."""
    try:
        entry = await partner_engine.record_manual_commission(
            db, organization_id=body.organization_id, qualifying_revenue_cents=body.qualifying_revenue_cents,
            payment_reference=body.payment_reference, plan_id=body.plan_id,
        )
    except AttributionNotFoundError:
        raise HTTPException(status_code=404, detail="This organization has no partner attribution.")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await log_action(
        db, actor_id=current_user.id, action="partner.commission.manual_entry_created",
        object_type="commission_entry", object_id=entry.id,
        detail={"organization_id": body.organization_id, "amount_cents": entry.commission_amount_cents},
    )
    return _entry_out(entry)


# ── Admin: payouts ──────────────────────────────────────────────────────────

@router.post("/admin/payouts", response_model=PayoutOut, status_code=201)
async def admin_create_payout(
    body: PayoutCreateRequest,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    try:
        payout = await partner_engine.create_payout(
            db, partner_id=body.partner_id, commission_entry_ids=body.commission_entry_ids,
            actor_id=current_user.id, notes=body.notes,
        )
    except CommissionEntryNotFoundError:
        raise HTTPException(status_code=404, detail="One or more commission entries not found.")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await log_action(
        db, actor_id=current_user.id, action="partner.payout.created",
        object_type="partner_payout", object_id=payout.id,
        detail={"partner_id": payout.partner_id, "amount_cents": payout.amount_cents},
    )
    return _payout_out(payout)


@router.get("/admin/payouts", response_model=List[PayoutOut])
async def admin_list_payouts(
    partner_id: Optional[str] = None,
    _: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    payouts = await partner_engine.list_payouts(db, partner_id=partner_id)
    return [_payout_out(p) for p in payouts]


# ── Admin: channel overview (dashboard KPIs) ────────────────────────────────

@router.get("/admin/overview", response_model=ChannelOverviewOut)
async def admin_channel_overview(_: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    overview = await partner_engine.get_channel_overview(db)
    return ChannelOverviewOut(**overview)


# ── Admin: Partner Portal invites ───────────────────────────────────────────

@router.post("/admin/applications/{partner_id}/portal-invite", response_model=PortalInviteCreateOut)
async def admin_send_portal_invite(
    partner_id: str,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """Superadmin action from the Admin Console's Applications section —
    grants an approved partner login access to the (read-only) Partner
    Portal. See engines/partner_engine.py::create_portal_invite for the
    "link an existing User directly" shortcut when one already exists with
    the partner's contact_email."""
    try:
        result = await partner_engine.create_portal_invite(db, partner_id=partner_id, invited_by_user_id=current_user.id)
    except PartnerNotFoundError:
        raise HTTPException(status_code=404, detail="Partner not found.")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except PartnerAccountExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    email_sent = False
    if result["linked_existing_user"]:
        message = "An existing Clariva account with this partner's contact email was linked directly — no email sent."
    else:
        invite = result["invite"]
        claim_url = f"{settings.FRONTEND_URL.rstrip('/')}/partner-portal-invite?token={invite.token}"
        email_sent = await send_email(
            to=invite.email, subject="Set up your Clariva Partner Portal login",
            html=render_partner_portal_invite_email(partner_name=result["partner"].name, claim_url=claim_url),
        )
        message = "Portal invite email sent." if email_sent else "Portal invite created, but the email failed to send — share the link manually."

    await log_action(
        db, actor_id=current_user.id, action="partner.portal_invite.sent",
        object_type="partner", object_id=partner_id,
        detail={"linked_existing_user": result["linked_existing_user"], "email_sent": email_sent},
    )
    return PortalInviteCreateOut(linked_existing_user=result["linked_existing_user"], email_sent=email_sent, message=message)


# ── Portal: public invite-claim flow ────────────────────────────────────────

@router.get("/portal/invite/{token}", response_model=PortalInvitePreviewOut)
async def portal_invite_preview(token: str, db: AsyncSession = Depends(get_db)):
    """Lets the claim page show who's inviting the person before they
    commit to setting a password — mirrors GET /invitations/by-token/{token}."""
    try:
        invite = await partner_engine.get_portal_invite_by_token(db, token)
    except PortalInviteNotFoundError:
        raise HTTPException(status_code=404, detail="Invitation not found.")
    partner = await partner_engine.get_partner(db, invite.partner_id)
    reason = partner_engine.portal_invite_validity(invite)
    return PortalInvitePreviewOut(
        partner_name=partner.name, email=invite.email, valid=reason is None, reason=reason,
    )


@router.post("/portal/invite/{token}/accept", response_model=LoginResponse)
async def portal_invite_accept(token: str, body: PortalInviteAcceptRequest, db: AsyncSession = Depends(get_db)):
    """Creates the partner contact's Clariva account, links Partner.user_id,
    and logs them in — one submit, mirrors POST /invitations/by-token/{token}/accept."""
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    try:
        user = await partner_engine.accept_portal_invite(
            db, token=token, full_name=body.full_name, hashed_password=hash_password(body.password),
        )
    except PortalInviteNotFoundError:
        raise HTTPException(status_code=404, detail="Invitation not found.")
    except PartnerAccountExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await log_action(db, actor_id=user.id, action="partner.portal_invite_accepted",
                      object_type="user", object_id=user.id)
    return _issue_tokens(user)


# ── Portal: authenticated partner-facing dashboard ──────────────────────────

@router.get("/portal/me", response_model=PartnerPortalMeOut)
async def portal_me(partner: Partner = Depends(require_partner_portal_access)):
    return PartnerPortalMeOut(
        id=partner.id, name=partner.name, contact_name=partner.contact_name, contact_email=partner.contact_email,
        status=partner.status, referral_code=partner.referral_code, program_status=partner.program_status,
    )


@router.get("/portal/overview", response_model=PartnerPortalOverviewOut)
async def portal_overview(
    partner: Partner = Depends(require_partner_portal_access), db: AsyncSession = Depends(get_db),
):
    overview = await partner_engine.get_partner_overview(db, partner.id)
    return PartnerPortalOverviewOut(**overview)


@router.get("/portal/customers", response_model=List[PartnerPortalCustomerOut])
async def portal_customers(
    partner: Partner = Depends(require_partner_portal_access), db: AsyncSession = Depends(get_db),
):
    customers = await partner_engine.list_attributed_customers(db, partner.id)
    return [
        PartnerPortalCustomerOut(
            organization_id=c["organization_id"], organization_name=c["organization_name"],
            attribution_type=c["attribution_type"],
            attributed_at=c["attributed_at"].isoformat() if c["attributed_at"] else None,
        )
        for c in customers
    ]


@router.get("/portal/commission-entries", response_model=List[CommissionEntryOut])
async def portal_commission_entries(
    status: Optional[str] = None,
    partner: Partner = Depends(require_partner_portal_access), db: AsyncSession = Depends(get_db),
):
    """Scoped to the calling partner only — partner_id comes from the
    resolved Partner, never from a request parameter, so there's no way
    for a partner to pass another partner's id and see their ledger."""
    entries = await partner_engine.list_commission_entries(db, partner_id=partner.id, status=status)
    return [_entry_out(e) for e in entries]


@router.get("/portal/payouts", response_model=List[PayoutOut])
async def portal_payouts(
    partner: Partner = Depends(require_partner_portal_access), db: AsyncSession = Depends(get_db),
):
    payouts = await partner_engine.list_payouts(db, partner_id=partner.id)
    return [_payout_out(p) for p in payouts]
