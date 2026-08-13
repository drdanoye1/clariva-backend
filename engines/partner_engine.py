"""
Engine 28 — Partner Center (Channel Partner Program, Phase 1 MVP)

Backend for the Channel Partner Program's Admin Console — see
docs/ARCHITECTURE.md's Partner Center MVP addendum for the full scope
decision (Admin Console + data model first, Partner Portal frontend
deferred). Implements Version 2's canonical chain (Customer -> Attribution
-> Subscription -> Payment -> Commission -> Payout) end to end on the
backend, minus the authenticated partner-facing UI.

No permission checks live here — routers/partners.py gates every
`/partners/admin/*` endpoint behind require_superadmin (imported from
routers.admin), matching every other admin-only endpoint in this codebase.
`POST /partners/apply` is intentionally unauthenticated (a prospective
partner has no Clariva account yet).

Design notes:
- Commission basis: "qualifying net base subscription revenue" per Version
  2 §2 maps naturally onto this app's existing Square webhook `kind`
  dispatch (routers/payments.py) — only `kind == "plan_subscription"`
  events ever reach record_commission(); `fund_topup` (AI Credits) and
  `seat_purchase` (Additional Seats) never do, which is exactly Version 2's
  exclusion list (AI usage, seats) without any extra filtering logic here.
- Cohort math uses 30-day buckets (`elapsed_days // 30 + 1`), not calendar
  months — consistent with this app's existing 30-day monthly billing
  interval (payments.py's `_plan_tier_and_interval`), and avoids ambiguity
  around variable month lengths. Documented approximation, not
  calendar-exact.
- `attributed_at` (and every other "now" comparison here) goes through the
  same `_naive()` guard already used in scripts/downgrade_expired_plans.py,
  routers/invitations.py, and engines/alerts_engine.py — SQLite returns
  naive datetimes on read, Postgres returns aware ones, and comparing the
  two raises `TypeError: can't compare offset-naive and offset-aware
  datetimes`. This is the fourth call site with this exact guard; consider
  extracting a shared helper if a fifth ever comes up.
- Reversal without a second CommissionAdjustment table: a reversed entry
  gets `status="reversed"` and a paired new row (`is_adjustment=True`,
  `adjusts_entry_id` pointing back) carrying the negated amount, so the sum
  of `commission_amount_cents` across a partner's ledger is always correct
  without ever mutating a historical entry's original numbers.
- Enterprise commission is a manual admin action (`record_manual_commission`),
  not something the Square webhook can trigger automatically — Enterprise
  has no self-serve checkout (see the Commercial Architecture Phase 1
  addendum), so `routers/payments.py`'s webhook never fires with
  `plan_id == "enterprise"` today. `DealRegistration.approved_
  commissionable_value_cents` exists so that when Clariva staff manually
  invoice an Enterprise deal, they can still create a correctly-computed
  commission entry against it.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import (
    CommissionEntry, CommissionRule, CustomerAttribution, DealRegistration, Organization, Partner,
    PartnerPayout, PartnerPortalInvite, User,
)

# Version 2 doesn't specify an exact deal-registration protection window
# (unlike the superseded v1 doc's 90-180 days) — a single named constant so
# this is a one-line change, not a migration, if Program Settings needs to
# make it admin-configurable later.
DEAL_PROTECTION_DAYS = 180

VALID_COMMISSION_STATUSES = ("pending", "approved", "available", "paid", "reversed")
VALID_PROGRAM_STATUSES = ("registered", "silver", "gold", "platinum")

# Longer than routers/organizations.py's INVITATION_EXPIRY_DAYS (7) —
# a partner-side decision maker's approval cycle for "should I set up our
# portal login" runs slower than an internal teammate accepting a team
# invite, and re-issuing is an extra admin click that's easy to avoid.
PORTAL_INVITE_EXPIRY_DAYS = 14

# Fields an admin may correct on an existing application/deal registration
# after the fact (typo fixes, a wrong contact email, etc.) — deliberately
# excludes anything a status-transition method above already owns
# (status, referral_code, program_status, protection_expires_at, reviewed_*)
# so update_partner()/update_deal() below can never be used as a backdoor
# around approve/reject/suspend.
EDITABLE_PARTNER_FIELDS = ("name", "contact_name", "contact_email", "partner_type", "notes")
EDITABLE_DEAL_FIELDS = (
    "organization_name", "contact_name", "contact_email", "domain",
    "proposed_plan", "estimated_value_cents", "approved_commissionable_value_cents", "notes",
)


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Strip tzinfo so naive (SQLite) and aware (Postgres) datetimes can be
    compared — see this module's docstring; same fix applied three times
    already elsewhere in this codebase."""
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo is not None else dt


class PartnerNotFoundError(Exception):
    pass


class DealRegistrationNotFoundError(Exception):
    pass


class CommissionEntryNotFoundError(Exception):
    pass


class AttributionNotFoundError(Exception):
    pass


class DuplicateClaimError(Exception):
    pass


class PortalInviteNotFoundError(Exception):
    pass


class PartnerAccountExistsError(Exception):
    pass


class PartnerEngine:

    # -- Applications -----------------------------------------------------

    async def apply(
        self, db: AsyncSession, *, name: str, contact_name: str, contact_email: str,
        partner_type: Optional[str] = None, notes: Optional[str] = None,
    ) -> Partner:
        partner = Partner(
            name=name.strip(), contact_name=contact_name.strip(), contact_email=contact_email.strip().lower(),
            partner_type=partner_type, notes=notes, status="pending",
        )
        db.add(partner)
        await db.flush()
        await db.refresh(partner)
        return partner

    async def get_partner(self, db: AsyncSession, partner_id: str) -> Partner:
        result = await db.execute(select(Partner).where(Partner.id == partner_id))
        partner = result.scalar_one_or_none()
        if partner is None:
            raise PartnerNotFoundError(partner_id)
        return partner

    async def list_partners(self, db: AsyncSession, *, status: Optional[str] = None) -> List[Partner]:
        stmt = select(Partner).order_by(Partner.created_at.desc())
        if status:
            stmt = stmt.where(Partner.status == status)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    def _generate_referral_code(self) -> str:
        # 8-char URL-safe token — short enough for a ?ref= query param,
        # long enough that guessing a live code isn't practical.
        return secrets.token_urlsafe(6).replace("_", "").replace("-", "")[:8].upper()

    async def approve_partner(self, db: AsyncSession, partner_id: str, actor_id: str) -> Partner:
        partner = await self.get_partner(db, partner_id)
        partner.status = "approved"
        if not partner.referral_code:
            partner.referral_code = self._generate_referral_code()
        partner.reviewed_by_user_id = actor_id
        partner.reviewed_at = datetime.now(timezone.utc)
        partner.rejection_reason = None
        await db.flush()
        await db.refresh(partner)
        return partner

    async def reject_partner(self, db: AsyncSession, partner_id: str, actor_id: str, reason: Optional[str] = None) -> Partner:
        partner = await self.get_partner(db, partner_id)
        partner.status = "rejected"
        partner.reviewed_by_user_id = actor_id
        partner.reviewed_at = datetime.now(timezone.utc)
        partner.rejection_reason = reason
        await db.flush()
        await db.refresh(partner)
        return partner

    async def suspend_partner(self, db: AsyncSession, partner_id: str, actor_id: str, reason: Optional[str] = None) -> Partner:
        partner = await self.get_partner(db, partner_id)
        partner.status = "suspended"
        partner.reviewed_by_user_id = actor_id
        partner.reviewed_at = datetime.now(timezone.utc)
        partner.rejection_reason = reason
        await db.flush()
        await db.refresh(partner)
        return partner

    async def set_program_status(self, db: AsyncSession, partner_id: str, program_status: str) -> Partner:
        if program_status not in VALID_PROGRAM_STATUSES:
            raise ValueError(f"Invalid program_status '{program_status}' — must be one of {VALID_PROGRAM_STATUSES}")
        partner = await self.get_partner(db, partner_id)
        partner.program_status = program_status
        await db.flush()
        await db.refresh(partner)
        return partner

    async def update_partner(self, db: AsyncSession, partner_id: str, changes: Dict[str, Any]) -> Tuple[Partner, Dict[str, Any]]:
        """Admin correction of an application's own fields — a typo in the
        firm name, a wrong contact email, etc. Distinct from every status-
        transition method above, which never touches these fields (see
        EDITABLE_PARTNER_FIELDS). `changes` may contain any subset of those
        fields; anything else is silently ignored so a caller can pass a
        Pydantic model's `.dict(exclude_unset=True)` straight through.
        Returns (partner, diff) where diff contains only fields whose value
        actually changed, each as {"old": ..., "new": ...} — this is what
        routers/partners.py logs to the audit trail, so "who changed what"
        is always a precise before/after, never a vague "was edited"."""
        partner = await self.get_partner(db, partner_id)
        diff: Dict[str, Any] = {}
        for field in EDITABLE_PARTNER_FIELDS:
            if field not in changes:
                continue
            new_value = changes[field]
            if isinstance(new_value, str):
                new_value = new_value.strip()
                if field == "contact_email":
                    new_value = new_value.lower()
            old_value = getattr(partner, field)
            if new_value != old_value:
                diff[field] = {"old": old_value, "new": new_value}
                setattr(partner, field, new_value)
        if diff:
            await db.flush()
            await db.refresh(partner)
        return partner, diff

    # -- Deal registrations -------------------------------------------------

    async def register_deal(
        self, db: AsyncSession, *, partner_id: str, organization_name: str,
        contact_name: Optional[str] = None, contact_email: Optional[str] = None,
        domain: Optional[str] = None, proposed_plan: Optional[str] = None,
        estimated_value_cents: Optional[int] = None, notes: Optional[str] = None,
    ) -> DealRegistration:
        # Confirm the partner exists (and is approved — an unapproved
        # partner has no live referral relationship yet, so registering a
        # deal on their behalf doesn't make sense).
        partner = await self.get_partner(db, partner_id)
        if partner.status != "approved":
            raise ValueError(f"Partner '{partner_id}' is not an approved partner (status: {partner.status})")

        normalized_domain = domain.strip().lower() if domain else None
        if normalized_domain:
            existing = await db.execute(
                select(DealRegistration).where(
                    func.lower(DealRegistration.domain) == normalized_domain,
                    DealRegistration.status.in_(["pending_review", "approved"]),
                    DealRegistration.partner_id != partner_id,
                )
            )
            conflict = existing.scalars().first()
            if conflict is not None:
                raise DuplicateClaimError(
                    f"Domain '{normalized_domain}' is already claimed by another partner's deal registration."
                )

        deal = DealRegistration(
            partner_id=partner_id, organization_name=organization_name.strip(),
            contact_name=contact_name, contact_email=contact_email, domain=normalized_domain,
            proposed_plan=proposed_plan, estimated_value_cents=estimated_value_cents,
            notes=notes, stage="lead", status="pending_review",
        )
        db.add(deal)
        await db.flush()
        await db.refresh(deal)
        return deal

    async def get_deal(self, db: AsyncSession, deal_id: str) -> DealRegistration:
        result = await db.execute(select(DealRegistration).where(DealRegistration.id == deal_id))
        deal = result.scalar_one_or_none()
        if deal is None:
            raise DealRegistrationNotFoundError(deal_id)
        return deal

    async def list_deals(
        self, db: AsyncSession, *, partner_id: Optional[str] = None, status: Optional[str] = None,
    ) -> List[DealRegistration]:
        stmt = select(DealRegistration).order_by(DealRegistration.created_at.desc())
        if partner_id:
            stmt = stmt.where(DealRegistration.partner_id == partner_id)
        if status:
            stmt = stmt.where(DealRegistration.status == status)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def approve_deal(
        self, db: AsyncSession, deal_id: str, actor_id: str, *,
        protection_days: Optional[int] = None, approved_commissionable_value_cents: Optional[int] = None,
    ) -> DealRegistration:
        deal = await self.get_deal(db, deal_id)
        deal.status = "approved"
        deal.protection_expires_at = datetime.now(timezone.utc) + timedelta(days=protection_days or DEAL_PROTECTION_DAYS)
        if approved_commissionable_value_cents is not None:
            deal.approved_commissionable_value_cents = approved_commissionable_value_cents
        deal.reviewed_by_user_id = actor_id
        deal.reviewed_at = datetime.now(timezone.utc)
        deal.rejection_reason = None
        await db.flush()
        await db.refresh(deal)
        return deal

    async def reject_deal(self, db: AsyncSession, deal_id: str, actor_id: str, reason: Optional[str] = None) -> DealRegistration:
        deal = await self.get_deal(db, deal_id)
        deal.status = "rejected"
        deal.reviewed_by_user_id = actor_id
        deal.reviewed_at = datetime.now(timezone.utc)
        deal.rejection_reason = reason
        await db.flush()
        await db.refresh(deal)
        return deal

    async def update_deal(self, db: AsyncSession, deal_id: str, changes: Dict[str, Any]) -> Tuple[DealRegistration, Dict[str, Any]]:
        """Admin correction of a deal registration's own fields — see
        update_partner()'s docstring for the same reasoning (excludes
        status/protection/review fields, only touches EDITABLE_DEAL_FIELDS,
        returns a precise before/after diff for the audit log). Editable
        even after approval: `approved_commissionable_value_cents` in
        particular is legitimately correctable if the negotiated Enterprise
        figure changes before a manual commission entry is created against
        it (see record_manual_commission's docstring)."""
        deal = await self.get_deal(db, deal_id)
        diff: Dict[str, Any] = {}
        for field in EDITABLE_DEAL_FIELDS:
            if field not in changes:
                continue
            new_value = changes[field]
            if isinstance(new_value, str):
                new_value = new_value.strip()
                if field == "domain":
                    new_value = new_value.lower() or None
            old_value = getattr(deal, field)
            if new_value != old_value:
                diff[field] = {"old": old_value, "new": new_value}
                setattr(deal, field, new_value)
        if diff:
            await db.flush()
            await db.refresh(deal)
        return deal, diff

    # -- Attribution ----------------------------------------------------------

    async def capture_attribution(
        self, db: AsyncSession, *, organization_id: str, referral_code: str,
        deal_registration_id: Optional[str] = None,
    ) -> Optional[CustomerAttribution]:
        """Called from routers/organizations.py::create_organization right
        after a new Organization is flushed. Returns None (never raises) on
        any invalid/unknown referral_code or an org that's already
        attributed — a bad or missing ref code must never block org
        creation itself."""
        existing = await db.execute(
            select(CustomerAttribution).where(CustomerAttribution.organization_id == organization_id)
        )
        if existing.scalar_one_or_none() is not None:
            return None  # first attribution wins — see model docstring

        result = await db.execute(
            select(Partner).where(Partner.referral_code == referral_code.strip().upper(), Partner.status == "approved")
        )
        partner = result.scalar_one_or_none()
        if partner is None:
            return None

        attribution = CustomerAttribution(
            organization_id=organization_id, partner_id=partner.id,
            deal_registration_id=deal_registration_id, attribution_type="partner_sourced",
        )
        db.add(attribution)
        await db.flush()
        await db.refresh(attribution)
        return attribution

    async def get_attribution_for_org(self, db: AsyncSession, organization_id: str) -> Optional[CustomerAttribution]:
        result = await db.execute(
            select(CustomerAttribution).where(CustomerAttribution.organization_id == organization_id)
        )
        return result.scalar_one_or_none()

    # -- Commission rule --------------------------------------------------------

    async def get_active_commission_rule(self, db: AsyncSession) -> CommissionRule:
        """Idempotent — seeds Version 2 §2's default 20/15/10/0 schedule on
        first call if no active rule exists yet. Safe to call on every
        request that needs the rate schedule (mirrors service_catalog_
        engine.py's ensure_seeded pattern)."""
        result = await db.execute(select(CommissionRule).where(CommissionRule.is_active == True))  # noqa: E712
        rule = result.scalars().first()
        if rule is not None:
            return rule
        rule = CommissionRule(is_active=True)  # column defaults: 0.20 / 0.15 / 0.10 / 0.0
        db.add(rule)
        await db.flush()
        await db.refresh(rule)
        return rule

    async def update_commission_rule(
        self, db: AsyncSession, *, months_1_12_rate: float, months_13_24_rate: float,
        months_25_36_rate: float, month_37_plus_rate: float, actor_id: str,
    ) -> CommissionRule:
        """Append-only versioning — deactivates the current rule and inserts
        a new active one, so every historical CommissionEntry stays
        traceable to the rule that actually produced it (entries snapshot
        their own rate at creation time and never get rewritten)."""
        current = await self.get_active_commission_rule(db)
        current.is_active = False
        new_rule = CommissionRule(
            months_1_12_rate=months_1_12_rate, months_13_24_rate=months_13_24_rate,
            months_25_36_rate=months_25_36_rate, month_37_plus_rate=month_37_plus_rate,
            is_active=True, created_by_user_id=actor_id,
        )
        db.add(new_rule)
        await db.flush()
        await db.refresh(new_rule)
        return new_rule

    def _rate_for_months_elapsed(self, rule: CommissionRule, months_elapsed: int) -> float:
        if months_elapsed <= 12:
            return rule.months_1_12_rate
        if months_elapsed <= 24:
            return rule.months_13_24_rate
        if months_elapsed <= 36:
            return rule.months_25_36_rate
        return rule.month_37_plus_rate

    # -- Commission entries -------------------------------------------------

    async def _create_commission_entry(
        self, db: AsyncSession, *, attribution: CustomerAttribution, qualifying_revenue_cents: int,
        payment_reference: Optional[str], plan_id: Optional[str],
    ) -> Optional[CommissionEntry]:
        rule = await self.get_active_commission_rule(db)
        now_naive = _naive(datetime.now(timezone.utc))
        elapsed_days = (now_naive - _naive(attribution.attributed_at)).days
        months_elapsed = max(elapsed_days // 30 + 1, 1)  # 1-indexed cohort bucket
        rate = self._rate_for_months_elapsed(rule, months_elapsed)
        if rate <= 0:
            # Month 37+ (standard 0%) — no entry created; an entry with a
            # $0 amount would just be ledger noise. See module docstring.
            return None

        entry = CommissionEntry(
            partner_id=attribution.partner_id, organization_id=attribution.organization_id,
            attribution_id=attribution.id, payment_reference=payment_reference, plan_id=plan_id,
            qualifying_revenue_cents=qualifying_revenue_cents, months_since_attribution=months_elapsed,
            commission_rate=rate, commission_amount_cents=round(qualifying_revenue_cents * rate),
            status="pending",
        )
        db.add(entry)
        await db.flush()
        await db.refresh(entry)
        return entry

    async def record_commission(
        self, db: AsyncSession, *, organization_id: str, plan_id: str,
        qualifying_revenue_cents: int, payment_reference: Optional[str] = None,
    ) -> Optional[CommissionEntry]:
        """Called from routers/payments.py's Square webhook handler, inside
        the `kind == "plan_subscription"` branch, after `org.plan`/`plan_
        expires_at` are set and flushed but before the webhook's final
        commit — so the commission entry commits atomically with the plan
        activation. Returns None (never raises) if the org has no partner
        attribution — most orgs don't, and that's the normal case, not an
        error."""
        attribution = await self.get_attribution_for_org(db, organization_id)
        if attribution is None:
            return None
        return await self._create_commission_entry(
            db, attribution=attribution, qualifying_revenue_cents=qualifying_revenue_cents,
            payment_reference=payment_reference, plan_id=plan_id,
        )

    async def record_manual_commission(
        self, db: AsyncSession, *, organization_id: str, qualifying_revenue_cents: int,
        payment_reference: Optional[str] = None, plan_id: Optional[str] = None,
    ) -> CommissionEntry:
        """Admin-triggered — e.g. an Enterprise deal invoiced manually
        outside self-serve checkout (see module docstring). Unlike
        record_commission(), this raises rather than silently returning
        None, since a superadmin explicitly invoking this on a specific org
        should know that org has an attribution before calling it."""
        attribution = await self.get_attribution_for_org(db, organization_id)
        if attribution is None:
            raise AttributionNotFoundError(organization_id)
        entry = await self._create_commission_entry(
            db, attribution=attribution, qualifying_revenue_cents=qualifying_revenue_cents,
            payment_reference=payment_reference, plan_id=plan_id,
        )
        if entry is None:
            raise ValueError("Computed commission rate is 0% for this org's current cohort (month 37+) — no entry created.")
        return entry

    async def get_commission_entry(self, db: AsyncSession, entry_id: str) -> CommissionEntry:
        result = await db.execute(select(CommissionEntry).where(CommissionEntry.id == entry_id))
        entry = result.scalar_one_or_none()
        if entry is None:
            raise CommissionEntryNotFoundError(entry_id)
        return entry

    async def list_commission_entries(
        self, db: AsyncSession, *, partner_id: Optional[str] = None, status: Optional[str] = None,
    ) -> List[CommissionEntry]:
        stmt = select(CommissionEntry).order_by(CommissionEntry.created_at.desc())
        if partner_id:
            stmt = stmt.where(CommissionEntry.partner_id == partner_id)
        if status:
            stmt = stmt.where(CommissionEntry.status == status)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def update_commission_entry_status(self, db: AsyncSession, entry_id: str, new_status: str) -> CommissionEntry:
        if new_status not in VALID_COMMISSION_STATUSES:
            raise ValueError(f"Invalid status '{new_status}' — must be one of {VALID_COMMISSION_STATUSES}")
        entry = await self.get_commission_entry(db, entry_id)
        if entry.is_adjustment:
            raise ValueError("Adjustment entries are immutable — reverse the original entry instead.")

        entry.status = new_status
        if new_status == "reversed":
            reversal = CommissionEntry(
                partner_id=entry.partner_id, organization_id=entry.organization_id,
                attribution_id=entry.attribution_id, payment_reference=entry.payment_reference,
                plan_id=entry.plan_id, qualifying_revenue_cents=entry.qualifying_revenue_cents,
                months_since_attribution=entry.months_since_attribution, commission_rate=entry.commission_rate,
                commission_amount_cents=-entry.commission_amount_cents, status="reversed",
                is_adjustment=True, adjusts_entry_id=entry.id,
            )
            db.add(reversal)
        await db.flush()
        await db.refresh(entry)
        return entry

    # -- Payouts ------------------------------------------------------------

    async def create_payout(
        self, db: AsyncSession, *, partner_id: str, commission_entry_ids: List[str],
        actor_id: str, notes: Optional[str] = None,
    ) -> PartnerPayout:
        """Bookkeeping only — see PartnerPayout's docstring. Marks every
        listed entry `paid` and sums them into a new payout row; does not
        call any payment processor or move money."""
        if not commission_entry_ids:
            raise ValueError("commission_entry_ids must be non-empty.")

        total_cents = 0
        entries: List[CommissionEntry] = []
        for entry_id in commission_entry_ids:
            entry = await self.get_commission_entry(db, entry_id)
            if entry.partner_id != partner_id:
                raise ValueError(f"Commission entry '{entry_id}' does not belong to partner '{partner_id}'.")
            if entry.status != "available":
                raise ValueError(f"Commission entry '{entry_id}' is not 'available' (status: {entry.status}) — approve it first.")
            entries.append(entry)
            total_cents += entry.commission_amount_cents

        payout = PartnerPayout(
            partner_id=partner_id, amount_cents=total_cents, commission_entry_ids=commission_entry_ids,
            status="paid", notes=notes, created_by_user_id=actor_id,
        )
        db.add(payout)
        for entry in entries:
            entry.status = "paid"
        await db.flush()
        await db.refresh(payout)
        return payout

    async def list_payouts(self, db: AsyncSession, *, partner_id: Optional[str] = None) -> List[PartnerPayout]:
        stmt = select(PartnerPayout).order_by(PartnerPayout.created_at.desc())
        if partner_id:
            stmt = stmt.where(PartnerPayout.partner_id == partner_id)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    # -- Channel overview (admin dashboard KPIs) -----------------------------

    async def get_channel_overview(self, db: AsyncSession) -> Dict[str, Any]:
        partner_counts = await db.execute(
            select(Partner.status, func.count(Partner.id)).group_by(Partner.status)
        )
        counts_by_status: Dict[str, int] = {status: count for status, count in partner_counts.all()}

        liability = await db.execute(
            select(func.coalesce(func.sum(CommissionEntry.commission_amount_cents), 0)).where(
                CommissionEntry.status.in_(["pending", "approved", "available"])
            )
        )
        paid = await db.execute(
            select(func.coalesce(func.sum(CommissionEntry.commission_amount_cents), 0)).where(
                CommissionEntry.status == "paid"
            )
        )
        attributed_orgs = await db.execute(select(func.count(CustomerAttribution.id)))
        pending_deals = await db.execute(
            select(func.count(DealRegistration.id)).where(DealRegistration.status == "pending_review")
        )

        return {
            "active_partners": counts_by_status.get("approved", 0),
            "pending_applications": counts_by_status.get("pending", 0),
            "suspended_partners": counts_by_status.get("suspended", 0),
            "attributed_customers": attributed_orgs.scalar_one(),
            "pending_deal_registrations": pending_deals.scalar_one(),
            "outstanding_commission_liability_cents": liability.scalar_one(),
            "total_commission_paid_cents": paid.scalar_one(),
        }

    # -- Partner Portal (read-only, Phase 1 of the roadmap's "Channel
    # Operations" plan) ------------------------------------------------------
    #
    # A partner reaches the Portal by logging into the same Clariva
    # User/JWT system every other authenticated area of this app uses —
    # `Partner.user_id` is the link, and `PartnerPortalInvite` (token +
    # Resend email + inline registration) is how that link gets created,
    # mirroring routers/invitations.py's org-member invite flow almost
    # exactly. See this file's module docstring update / docs/ARCHITECTURE.md
    # for the full design rationale.

    async def create_portal_invite(
        self, db: AsyncSession, *, partner_id: str, invited_by_user_id: str,
    ) -> Dict[str, Any]:
        """Links `partner` to a Clariva User account. If a User already
        exists with the partner's contact_email, skips PartnerPortalInvite
        entirely and links Partner.user_id directly — same shortcut
        routers/organizations.py's invite-creation endpoint takes for org
        invites. Otherwise issues a token-based PartnerPortalInvite (mirrors
        Invitation) for the router to email.

        Returns {"linked_existing_user": bool, "invite": PartnerPortalInvite | None, "partner": Partner}
        so the router can decide whether to send an email or just report
        the direct link."""
        partner = await self.get_partner(db, partner_id)
        if partner.status != "approved":
            raise ValueError(f"Partner '{partner_id}' is not an approved partner (status: {partner.status})")
        if partner.user_id:
            raise PartnerAccountExistsError(f"Partner '{partner_id}' is already linked to a portal account.")

        existing_user = await db.execute(select(User).where(User.email == partner.contact_email))
        user = existing_user.scalar_one_or_none()
        if user is not None:
            partner.user_id = user.id
            await db.flush()
            await db.refresh(partner)
            return {"linked_existing_user": True, "invite": None, "partner": partner}

        # Revoke any still-pending invite for this partner before issuing a
        # new one — one live invite per partner at a time keeps a token
        # lookup unambiguous (mirrors organizations.py's "refresh the
        # existing pending Invitation" resend behavior).
        pending = await db.execute(
            select(PartnerPortalInvite).where(
                PartnerPortalInvite.partner_id == partner_id, PartnerPortalInvite.status == "pending",
            )
        )
        for row in pending.scalars().all():
            row.status = "revoked"

        invite = PartnerPortalInvite(
            partner_id=partner_id, email=partner.contact_email, token=secrets.token_urlsafe(32),
            invited_by=invited_by_user_id, status="pending",
            expires_at=datetime.now(timezone.utc) + timedelta(days=PORTAL_INVITE_EXPIRY_DAYS),
        )
        db.add(invite)
        await db.flush()
        await db.refresh(invite)
        return {"linked_existing_user": False, "invite": invite, "partner": partner}

    async def get_portal_invite_by_token(self, db: AsyncSession, token: str) -> PartnerPortalInvite:
        result = await db.execute(select(PartnerPortalInvite).where(PartnerPortalInvite.token == token))
        invite = result.scalar_one_or_none()
        if invite is None:
            raise PortalInviteNotFoundError(token)
        return invite

    def portal_invite_validity(self, invite: PartnerPortalInvite) -> Optional[str]:
        """None = valid; otherwise a short reason code — mirrors
        routers/invitations.py's _invitation_validity() exactly, including
        the naive/aware stripping (see this module's _naive() docstring)."""
        if invite.status == "revoked":
            return "revoked"
        if invite.status == "accepted":
            return "accepted"
        if invite.expires_at and _naive(invite.expires_at) < _naive(datetime.now(timezone.utc)):
            return "expired"
        return None

    async def accept_portal_invite(
        self, db: AsyncSession, *, token: str, full_name: str, hashed_password: str,
    ) -> User:
        """Mirrors routers/invitations.py::accept_invitation step-by-step,
        minus the org-membership creation (a partner isn't an org member) —
        links the new User onto Partner.user_id instead. Takes an
        already-hashed password rather than hashing here, since this engine
        (like every other engine in this codebase) doesn't import from
        routers/auth.py — the router hashes it via the same hash_password()
        every other auth path uses, then passes the result in. Raises
        ValueError/PartnerAccountExistsError for caller-facing conditions;
        the router translates those to HTTPExceptions."""
        invite = await self.get_portal_invite_by_token(db, token)
        reason = self.portal_invite_validity(invite)
        if reason:
            raise ValueError(f"This invitation has been {reason}.")

        if not full_name.strip():
            raise ValueError("Full name is required.")

        existing = await db.execute(select(User).where(User.email == invite.email))
        if existing.scalar_one_or_none():
            raise PartnerAccountExistsError(
                "An account with this email already exists. Please log in instead."
            )

        partner = await self.get_partner(db, invite.partner_id)

        user = User(
            email=invite.email, hashed_password=hashed_password, full_name=full_name.strip(),
            organization=f"{partner.name} (Partner)",
        )
        db.add(user)
        await db.flush()

        partner.user_id = user.id
        invite.status = "accepted"
        invite.accepted_at = datetime.now(timezone.utc)
        await db.flush()
        await db.refresh(user)
        return user

    async def get_partner_by_user_id(self, db: AsyncSession, user_id: str) -> Optional[Partner]:
        result = await db.execute(select(Partner).where(Partner.user_id == user_id))
        return result.scalar_one_or_none()

    async def get_partner_overview(self, db: AsyncSession, partner_id: str) -> Dict[str, Any]:
        """Partner-scoped KPIs for the Portal's landing screen — Appendix
        B's first acceptance criterion: "A partner can identify active
        customers, attributed subscription revenue, current commission,
        available payout and lifetime earnings from the first screen."
        Every query here is scoped to `partner_id`; see
        list_attributed_customers()'s docstring for the matching rule
        about never joining into customer content tables."""
        await self.get_partner(db, partner_id)  # raises PartnerNotFoundError if unknown

        customers = await db.execute(
            select(func.count(CustomerAttribution.id)).where(CustomerAttribution.partner_id == partner_id)
        )

        # Plain naive UTC, passed as a bind parameter (not compared directly
        # against a Python datetime) — same convention as credit_engine.py's
        # _spent_in_period(), which sidesteps the naive/aware comparison
        # issue entirely by letting the DB driver handle the comparison.
        month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        revenue = await db.execute(
            select(func.coalesce(func.sum(CommissionEntry.qualifying_revenue_cents), 0)).where(
                CommissionEntry.partner_id == partner_id, CommissionEntry.is_adjustment == False,  # noqa: E712
                CommissionEntry.status != "reversed",
            )
        )
        this_month = await db.execute(
            select(func.coalesce(func.sum(CommissionEntry.commission_amount_cents), 0)).where(
                CommissionEntry.partner_id == partner_id, CommissionEntry.is_adjustment == False,  # noqa: E712
                CommissionEntry.status != "reversed", CommissionEntry.created_at >= month_start,
            )
        )
        available = await db.execute(
            select(func.coalesce(func.sum(CommissionEntry.commission_amount_cents), 0)).where(
                CommissionEntry.partner_id == partner_id, CommissionEntry.status == "available",
            )
        )
        lifetime = await db.execute(
            select(func.coalesce(func.sum(CommissionEntry.commission_amount_cents), 0)).where(
                CommissionEntry.partner_id == partner_id, CommissionEntry.is_adjustment == False,  # noqa: E712
                CommissionEntry.status != "reversed",
            )
        )

        return {
            "active_customers": customers.scalar_one(),
            "attributed_subscription_revenue_cents": revenue.scalar_one(),
            "commission_this_month_cents": this_month.scalar_one(),
            "available_for_payout_cents": available.scalar_one(),
            "lifetime_earnings_cents": lifetime.scalar_one(),
        }

    async def list_attributed_customers(self, db: AsyncSession, partner_id: str) -> List[Dict[str, Any]]:
        """Partner Portal customer list — Appendix B: "Partner users cannot
        access customer grant content without an explicit separate
        authorization mechanism." This deliberately joins only
        Organization.name onto CustomerAttribution's own columns; it must
        never join into Proposal/FOARecord/ProjectKnowledge or any other
        table that holds a customer's actual grant content."""
        result = await db.execute(
            select(CustomerAttribution, Organization.name)
            .join(Organization, Organization.id == CustomerAttribution.organization_id)
            .where(CustomerAttribution.partner_id == partner_id)
            .order_by(CustomerAttribution.attributed_at.desc())
        )
        return [
            {
                "organization_id": attribution.organization_id,
                "organization_name": org_name,
                "attribution_type": attribution.attribution_type,
                "attributed_at": attribution.attributed_at,
            }
            for attribution, org_name in result.all()
        ]
