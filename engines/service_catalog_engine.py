"""
Engine 21 — Service Catalog & On-Demand AI Services Marketplace
Implements the Enterprise Public-Facing Pricing & Internal Engineering
Economics spec (v1.0, Aug 2026), §9.2 "Phase 2 — In-Platform On-Demand
Marketplace & Organization Funding Controls."

Core commercial rule this engine enforces (spec §1): subscription plans buy
platform *access*; every AI-generated output is purchased on demand, priced
centrally, and paid for from either (a) a complimentary signup allowance
consumed first, or (b) the org's paid AI Services balance. No paid
generation may occur without the caller having first fetched a `quote()`
and the requesting router presenting/confirming it — see
routers/service_catalog.py.

Design notes (same discipline as the other Phase-numbered engines):
- Every method takes an already-open AsyncSession and only flushes, never
  commits — the caller's request-scoped session commits once.
- SERVICE_CATALOG_SEED / COMPLIMENTARY_ALLOWANCE_SEED are the single source
  of truth for prices/allowances (acceptance criterion: "service prices are
  controlled centrally"). `ensure_seeded()` is idempotent — safe to call on
  every app boot (see database.py::create_tables) or from a management
  command; it only inserts rows for service_keys/plan+service_key pairs
  that don't already exist, so an operator's later manual price edit in the
  service_catalog_items table is never clobbered by a redeploy.
- Funding source: this pass reuses the existing AICreditLedger.balance
  (Engine 11) as the org's dollar-denominated "AI Services balance" ($1 of
  balance = $1 of purchasing power, spec §6.1) rather than standing up a
  second ledger table. CreditEngine.debit()/credit()/debit_or_402() are
  unchanged; they're simply called here with dollar amounts
  (price_cents / 100) instead of the old flat 1.0-per-section amount. A
  dedicated multi-source "AI Services Fund" (deposits, spending controls,
  per-project allocations, described in spec §6) is a later slice on top
  of this same balance column — nothing here forecloses that.
- Complimentary allowances are granted lazily, the same pattern
  CreditEngine.get_or_create_ledger uses for the credit ledger: the first
  time an org's entitlements are looked up (quote/consume/summary), any
  ComplimentaryAllowance row for the org's current Organization.plan that
  the org doesn't yet have an OrgServiceEntitlement for is granted then,
  with `expires_at` = now + validity_days. This means an org's allowances
  reflect its *current* plan at first-use time, not whatever plan existed
  when the org row was created — matches the spec's "signup allowance"
  framing well enough for v1 without needing a separate "plan changed"
  event hook.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engines.credit_engine import CreditEngine, InsufficientCreditsError
from models.db_models import (
    AIServiceTransaction, ComplimentaryAllowance, Organization,
    OrgServiceEntitlement, ServiceCatalogItem, new_uuid,
)

_log = logging.getLogger(__name__)

credit_engine = CreditEngine()


class ServiceNotFoundError(Exception):
    def __init__(self, service_key: str):
        self.service_key = service_key
        super().__init__(f"Unknown service_key: {service_key}")


class AllowanceNotFoundError(Exception):
    def __init__(self, allowance_id: str):
        self.allowance_id = allowance_id
        super().__init__(f"Unknown allowance id: {allowance_id}")


# ---------------------------------------------------------------------------
# Seed data — Enterprise Pricing spec §2.4, §3.2, §3.3, §4.1, §4.2
# All prices in cents. subscriber_price_cents applies to Professional/Team/
# Organization/Enterprise plans; payg_price_cents applies to the Free plan
# (no active subscription) where the spec defines a PAYG premium (roughly
# 20-25% over the subscriber price).
# ---------------------------------------------------------------------------
SERVICE_CATALOG_SEED: List[Dict] = [
    # --- §2.4 / Grant Opportunity Analysis (Grant Discovery workspace) ------
    {
        "service_key": "grant_opportunity_analysis", "category": "grant_analysis",
        "name": "Grant Opportunity Analysis",
        "description": "AI-generated funding opportunity summary, eligibility analysis, requirements extraction, evaluation-criteria analysis, and proposal-readiness recommendation for a single FOA.",
        "complexity": None, "workspace": "pre_award",
        "subscriber_price_cents": 1000, "payg_price_cents": 1000, "recurring": False,
    },
    # --- §3.2 / Proposal Development (Pre-Award workspace) ------------------
    {
        "service_key": "proposal_development_standard", "category": "proposal_development",
        "name": "Proposal Development — Standard",
        "description": "Full standard-complexity proposal draft.",
        "complexity": "standard", "workspace": "pre_award",
        "subscriber_price_cents": 10000, "payg_price_cents": 12500, "recurring": False,
    },
    {
        "service_key": "proposal_development_advanced", "category": "proposal_development",
        "name": "Proposal Development — Advanced",
        "description": "Full advanced-complexity proposal draft.",
        "complexity": "advanced", "workspace": "pre_award",
        "subscriber_price_cents": 25000, "payg_price_cents": 30000, "recurring": False,
    },
    {
        "service_key": "proposal_development_complex", "category": "proposal_development",
        "name": "Proposal Development — Complex",
        "description": "Full complex proposal draft (e.g. multi-component, multi-PI, or center-scale submissions).",
        "complexity": "complex", "workspace": "pre_award",
        "subscriber_price_cents": 60000, "payg_price_cents": 75000, "recurring": False,
    },
    # --- §3.3 / Supporting documents (Pre-Award workspace) ------------------
    # PAYG = subscriber price + ~20-25% premium, rounded to the nearest 100 cents.
    {"service_key": "doc_cover_letter", "category": "supporting_document", "name": "Cover Letter",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 500, "payg_price_cents": 600, "recurring": False},
    {"service_key": "doc_letter_of_support", "category": "supporting_document", "name": "Letter of Support",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 500, "payg_price_cents": 600, "recurring": False},
    {"service_key": "doc_letter_of_commitment", "category": "supporting_document", "name": "Letter of Commitment",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 500, "payg_price_cents": 600, "recurring": False},
    {"service_key": "doc_letter_of_inquiry", "category": "supporting_document", "name": "Letter of Inquiry",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 1000, "payg_price_cents": 1200, "recurring": False},
    {"service_key": "doc_capability_statement", "category": "supporting_document", "name": "Capability Statement",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 2000, "payg_price_cents": 2500, "recurring": False},
    {"service_key": "doc_concept_paper", "category": "supporting_document", "name": "Concept Paper",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 2500, "payg_price_cents": 3100, "recurring": False},
    {"service_key": "doc_mou", "category": "supporting_document", "name": "MOU",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 2500, "payg_price_cents": 3100, "recurring": False},
    {"service_key": "doc_logic_model", "category": "supporting_document", "name": "Logic Model",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 3500, "payg_price_cents": 4400, "recurring": False},
    {"service_key": "doc_sustainability_plan", "category": "supporting_document", "name": "Sustainability Plan",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 3500, "payg_price_cents": 4400, "recurring": False},
    {"service_key": "doc_risk_management_plan", "category": "supporting_document", "name": "Risk Management Plan",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 3500, "payg_price_cents": 4400, "recurring": False},
    {"service_key": "doc_project_management_plan", "category": "supporting_document", "name": "Project Management Plan",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 4000, "payg_price_cents": 5000, "recurring": False},
    {"service_key": "doc_me_plan", "category": "supporting_document", "name": "M&E Plan",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 5000, "payg_price_cents": 6300, "recurring": False},
    {"service_key": "doc_data_management_plan", "category": "supporting_document", "name": "Data Management Plan",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 5000, "payg_price_cents": 6300, "recurring": False},
    {"service_key": "doc_budget", "category": "supporting_document", "name": "Budget",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 5000, "payg_price_cents": 6300, "recurring": False},
    {"service_key": "doc_budget_justification", "category": "supporting_document", "name": "Budget Justification",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 5000, "payg_price_cents": 6300, "recurring": False},
    {"service_key": "doc_budget_and_justification", "category": "supporting_document", "name": "Budget + Justification (bundle)",
     "description": None, "complexity": None, "workspace": "pre_award",
     "subscriber_price_cents": 8500, "payg_price_cents": 10600, "recurring": False},
    # --- §4.1 / Award Setup & Activation (Award workspace) ------------------
    {
        "service_key": "award_setup_activation", "category": "award_setup",
        "name": "Award Setup & Activation",
        "description": "Full award setup & activation, including AI processing of up to 150 pages of award/intake documents.",
        "complexity": None, "workspace": "award",
        "subscriber_price_cents": 8000, "payg_price_cents": 10000, "recurring": False,
    },
    {
        "service_key": "award_setup_additional_pages", "category": "award_setup",
        "name": "Additional AI Document Processing (per 100 pages)",
        "description": "Extra AI document processing beyond the 150 pages included with Award Setup & Activation, billed per additional 100-page block.",
        "complexity": None, "workspace": "award",
        "subscriber_price_cents": 1200, "payg_price_cents": 1500, "recurring": False,
    },
    # --- §4.2 / Active Award Management & Compliance (Post-Award workspace) -
    # Recurring: charged monthly per active award.
    {
        "service_key": "post_award_management_standard", "category": "post_award_management",
        "name": "Active Award Management & Compliance — Standard",
        "description": "Monthly Post-Award management & compliance, standard complexity, per active award.",
        "complexity": "standard", "workspace": "post_award",
        "subscriber_price_cents": 7500, "payg_price_cents": None, "recurring": True,
    },
    {
        "service_key": "post_award_management_advanced", "category": "post_award_management",
        "name": "Active Award Management & Compliance — Advanced",
        "description": "Monthly Post-Award management & compliance, advanced complexity, per active award.",
        "complexity": "advanced", "workspace": "post_award",
        "subscriber_price_cents": 15000, "payg_price_cents": None, "recurring": True,
    },
    {
        "service_key": "post_award_management_complex", "category": "post_award_management",
        "name": "Active Award Management & Compliance — Complex",
        "description": "Monthly Post-Award management & compliance, complex, per active award.",
        "complexity": "complex", "workspace": "post_award",
        "subscriber_price_cents": 25000, "payg_price_cents": None, "recurring": True,
    },
    # --- Funding Opportunity Intelligence, Phase 3 §4.6 / Funding Strategy --
    # Intelligence (org-level strategic synthesis) --------------------------
    # Priced in the same band as the heavier planning documents ($35-$50)
    # rather than the single-opportunity Grant Opportunity Analysis ($10):
    # this reads across the org's whole pipeline, historical performance,
    # and profile in one call, not a single FOA.
    {
        "service_key": "funding_strategy_intelligence", "category": "grant_analysis",
        "name": "Funding Strategy Intelligence",
        "description": "AI-synthesized organizational funding strategy: priority agencies/programs, target funding, quarterly pursuit calendar, capability gaps, partnership strategy, and proposal resource plan — generated from the org's pipeline, historical performance, and Funding Intelligence Profile.",
        "complexity": None, "workspace": "pre_award",
        "subscriber_price_cents": 3500, "payg_price_cents": 4000, "recurring": False,
    },
]

# --- §2.3 / Complimentary signup allowances (90-day validity baseline) -----
# quantity=None is reserved for a future "unlimited within window" allowance
# type; every current row has an explicit quantity.
COMPLIMENTARY_ALLOWANCE_SEED: List[Dict] = [
    # plan, service_key, quantity, validity_days
    ("professional", "grant_opportunity_analysis", 2, 90),
    ("team",         "grant_opportunity_analysis", 5, 90),
    ("organization", "grant_opportunity_analysis", 10, 90),
    ("large",        "grant_opportunity_analysis", 20, 90),

    # "Supporting Documents" allowance applies to any one doc_* service —
    # seeded against the most commonly used one (Cover Letter); consume()
    # treats any service in category "supporting_document" as drawing from
    # whichever doc_* entitlement rows the org has, see _pick_entitlement().
    ("professional", "doc_cover_letter", 2, 90),
    ("team",         "doc_cover_letter", 3, 90),
    ("organization", "doc_cover_letter", 5, 90),
    ("large",        "doc_cover_letter", 8, 90),

    ("professional", "proposal_development_standard", None, 90),  # "Limited trial" — see NOTE below
    ("team",         "proposal_development_standard", 1, 90),
    ("organization", "proposal_development_standard", 1, 90),
    ("large",        "proposal_development_standard", 2, 90),

    ("professional", "award_setup_activation", None, 90),  # "Limited trial"
    ("team",         "award_setup_activation", 1, 90),
    ("organization", "award_setup_activation", 1, 90),
    ("large",        "award_setup_activation", 2, 90),
]
# "large" tier quantities above (20/8/2/2) are not from a specific SOP table
# cell — the source spec's allowance table stops at Organization — and were
# extrapolated by continuing that table's growth curve one step further.
# Flagged for product review same as the "Limited trial" interpretation note
# below; adjust the tuples above if/when a real Large-tier allowance number
# is specified.
# NOTE: spec §2.3 describes Professional's Standard-Proposal and Award-Setup
# rows as "Limited trial" rather than a fixed count. Modeled here as
# quantity=1 (one trial use) rather than quantity=None/"unlimited", since
# OrgServiceEntitlement.granted_quantity is an integer and "limited trial"
# unambiguously means "try it once," not "unlimited." Flagged for product
# review if a different interpretation is intended.
COMPLIMENTARY_ALLOWANCE_SEED = [
    (plan, key, (1 if qty is None else qty), days)
    for (plan, key, qty, days) in COMPLIMENTARY_ALLOWANCE_SEED
]


class ServiceCatalogEngine:
    # -- Seeding ------------------------------------------------------------
    async def ensure_seeded(self, db: AsyncSession) -> None:
        """Idempotent: inserts any catalog item / allowance row that doesn't
        exist yet, by service_key / (plan, service_key). Safe to call on
        every app boot."""
        existing = await db.execute(select(ServiceCatalogItem.service_key))
        existing_keys = {row[0] for row in existing.all()}
        for item in SERVICE_CATALOG_SEED:
            if item["service_key"] in existing_keys:
                continue
            db.add(ServiceCatalogItem(id=new_uuid(), **item))

        # Flush the catalog items before adding anything that foreign-keys to
        # service_key (ComplimentaryAllowance below). This session is
        # configured with autoflush=False (see database.py — a deliberate
        # choice to avoid an earlier async/greenlet issue), so without this
        # explicit flush both batches of db.add() calls would only hit the
        # database at the single flush() at the end of this method, with no
        # guarantee the ServiceCatalogItem INSERTs run before the
        # ComplimentaryAllowance ones that reference them — which is exactly
        # what caused a production ForeignKeyViolationError on first deploy.
        await db.flush()

        existing_allow = await db.execute(
            select(ComplimentaryAllowance.plan, ComplimentaryAllowance.service_key)
        )
        existing_pairs = {(row[0], row[1]) for row in existing_allow.all()}
        for plan, service_key, quantity, validity_days in COMPLIMENTARY_ALLOWANCE_SEED:
            if (plan, service_key) in existing_pairs:
                continue
            db.add(ComplimentaryAllowance(
                id=new_uuid(), plan=plan, service_key=service_key,
                quantity=quantity, validity_days=validity_days,
            ))
        await db.flush()

    # -- Catalog reads --------------------------------------------------------
    async def list_catalog(self, db: AsyncSession, *, active_only: bool = True) -> List[ServiceCatalogItem]:
        query = select(ServiceCatalogItem)
        if active_only:
            query = query.where(ServiceCatalogItem.active.is_(True))
        result = await db.execute(query.order_by(ServiceCatalogItem.category, ServiceCatalogItem.name))
        return list(result.scalars().all())

    async def get_service(self, db: AsyncSession, service_key: str) -> ServiceCatalogItem:
        result = await db.execute(
            select(ServiceCatalogItem).where(ServiceCatalogItem.service_key == service_key)
        )
        item = result.scalar_one_or_none()
        if not item:
            raise ServiceNotFoundError(service_key)
        return item

    # -- Entitlements ---------------------------------------------------------
    async def _get_org_plan(self, db: AsyncSession, org_id: str) -> str:
        result = await db.execute(select(Organization.plan).where(Organization.id == org_id))
        plan = result.scalar_one_or_none()
        return plan or "free"

    async def ensure_entitlements_granted(self, db: AsyncSession, org_id: str) -> None:
        """Lazily grant any ComplimentaryAllowance for the org's current plan
        that the org doesn't yet have an OrgServiceEntitlement row for."""
        plan = await self._get_org_plan(db, org_id)
        if plan in (None, "free"):
            return  # Free plan has no complimentary AI-services allowance.

        allowances = await db.execute(
            select(ComplimentaryAllowance).where(ComplimentaryAllowance.plan == plan)
        )
        allowances = list(allowances.scalars().all())
        if not allowances:
            return

        existing = await db.execute(
            select(OrgServiceEntitlement.service_key).where(OrgServiceEntitlement.org_id == org_id)
        )
        existing_keys = {row[0] for row in existing.all()}

        now = datetime.utcnow()
        for allowance in allowances:
            if allowance.service_key in existing_keys:
                continue
            db.add(OrgServiceEntitlement(
                id=new_uuid(), org_id=org_id, service_key=allowance.service_key,
                granted_quantity=allowance.quantity or 0, used_quantity=0,
                expires_at=now + timedelta(days=allowance.validity_days),
            ))
        await db.flush()

    async def list_entitlements(self, db: AsyncSession, org_id: str) -> List[OrgServiceEntitlement]:
        await self.ensure_entitlements_granted(db, org_id)
        result = await db.execute(
            select(OrgServiceEntitlement).where(OrgServiceEntitlement.org_id == org_id)
        )
        return list(result.scalars().all())

    async def _find_available_entitlement(
        self, db: AsyncSession, org_id: str, service_key: str, category: str,
    ) -> Optional[OrgServiceEntitlement]:
        """An unexpired entitlement row with remaining quantity for this
        exact service_key, or — for supporting_document services, which
        share one allowance bucket per spec §2.3 — any unexpired doc_*
        entitlement row with remaining quantity."""
        await self.ensure_entitlements_granted(db, org_id)
        now = datetime.utcnow()

        result = await db.execute(
            select(OrgServiceEntitlement).where(
                OrgServiceEntitlement.org_id == org_id,
                OrgServiceEntitlement.service_key == service_key,
            )
        )
        row = result.scalar_one_or_none()
        if row and row.used_quantity < row.granted_quantity and (row.expires_at is None or row.expires_at > now):
            return row

        if category == "supporting_document":
            result = await db.execute(
                select(OrgServiceEntitlement).where(
                    OrgServiceEntitlement.org_id == org_id,
                    OrgServiceEntitlement.service_key.like("doc_%"),
                )
            )
            for candidate in result.scalars().all():
                if candidate.used_quantity < candidate.granted_quantity and (
                    candidate.expires_at is None or candidate.expires_at > now
                ):
                    return candidate
        return None

    # -- Pricing / quoting ------------------------------------------------------
    async def quote(
        self, db: AsyncSession, org_id: str, service_key: str,
    ) -> Dict:
        """Price + funding-source preview for a service, WITHOUT charging
        anything — routers call this to show the user a confirmation before
        triggering generation (spec §9.2: "price-before-generation
        confirmation")."""
        service = await self.get_service(db, service_key)
        plan = await self._get_org_plan(db, org_id)
        is_subscriber = plan != "free"

        entitlement = await self._find_available_entitlement(db, org_id, service_key, service.category)
        if entitlement:
            funding_source = "complimentary"
            price_cents = 0
        else:
            funding_source = "ai_services_balance"
            price_cents = (
                service.subscriber_price_cents if is_subscriber
                else (service.payg_price_cents or service.subscriber_price_cents)
            )

        balance = await credit_engine.get_balance(db, org_id)
        return {
            "service_key": service.service_key,
            "name": service.name,
            "category": service.category,
            "complexity": service.complexity,
            "recurring": service.recurring,
            "funding_source": funding_source,
            "price_cents": price_cents,
            "list_price_cents": service.subscriber_price_cents if is_subscriber else (service.payg_price_cents or service.subscriber_price_cents),
            "ai_services_balance_cents": round(balance * 100),
            "sufficient_balance": funding_source == "complimentary" or (balance * 100) >= price_cents,
        }

    # -- Consumption ------------------------------------------------------------
    async def consume(
        self, db: AsyncSession, org_id: str, user_id: Optional[str], service_key: str,
        reference: Optional[Dict] = None,
    ) -> AIServiceTransaction:
        """Charges for one unit of `service_key` — complimentary allowance
        first, else the org's paid AI Services balance (via the existing
        CreditEngine, hard 402 at insufficient balance, exactly as today's
        proposal-generation metering already behaves). Records an
        AIServiceTransaction either way, so complimentary vs paid usage is
        always distinguishable (acceptance criterion) and every paid
        transaction's funding source + price is on the record."""
        service = await self.get_service(db, service_key)
        entitlement = await self._find_available_entitlement(db, org_id, service_key, service.category)

        if entitlement:
            entitlement.used_quantity += 1
            transaction = AIServiceTransaction(
                id=new_uuid(), org_id=org_id, user_id=user_id, service_key=service_key,
                funding_source="complimentary", price_cents=0, reference=reference or {},
            )
            db.add(transaction)
            await db.flush()
            return transaction

        plan = await self._get_org_plan(db, org_id)
        is_subscriber = plan != "free"
        price_cents = (
            service.subscriber_price_cents if is_subscriber
            else (service.payg_price_cents or service.subscriber_price_cents)
        )

        # Reuses Engine 11 as-is: dollar amount = price_cents / 100. Raises
        # InsufficientCreditsError (translated to HTTP 402 by the caller via
        # debit_or_402, or caught directly here) — no partial-apply.
        try:
            await credit_engine.debit(
                db, org_id, user_id, price_cents / 100.0,
                reason=f"ai_service:{service_key}",
            )
        except InsufficientCreditsError:
            raise

        transaction = AIServiceTransaction(
            id=new_uuid(), org_id=org_id, user_id=user_id, service_key=service_key,
            funding_source="ai_services_balance", price_cents=price_cents, reference=reference or {},
        )
        db.add(transaction)
        await db.flush()
        return transaction

    # -- Reporting ------------------------------------------------------------
    async def get_org_summary(self, db: AsyncSession, org_id: str) -> Dict:
        """Everything an org's usage/billing view needs in one call: current
        AI Services balance, live entitlements, and recent transactions."""
        balance = await credit_engine.get_balance(db, org_id)
        entitlements = await self.list_entitlements(db, org_id)
        result = await db.execute(
            select(AIServiceTransaction)
            .where(AIServiceTransaction.org_id == org_id)
            .order_by(AIServiceTransaction.created_at.desc())
            .limit(100)
        )
        transactions = list(result.scalars().all())
        return {
            "ai_services_balance_cents": round(balance * 100),
            "entitlements": entitlements,
            "transactions": transactions,
        }

    # -- Admin — Configurable Pricing Controls (Phase 3.1) -------------------
    # Lets a superadmin edit prices/allowances that were previously only
    # settable by editing SERVICE_CATALOG_SEED / COMPLIMENTARY_ALLOWANCE_SEED
    # and redeploying. Gated by routers.admin.require_superadmin at the
    # router layer — these methods do no permission checking themselves.

    async def admin_update_service(
        self, db: AsyncSession, service_key: str, updates: Dict,
    ) -> ServiceCatalogItem:
        service = await self.get_service(db, service_key)
        for field in ("name", "description", "subscriber_price_cents", "payg_price_cents", "active"):
            value = updates.get(field, None)
            if value is not None:
                setattr(service, field, value)
        await db.flush()
        await db.refresh(service)
        return service

    async def admin_create_service(self, db: AsyncSession, data: Dict) -> ServiceCatalogItem:
        existing = await db.execute(
            select(ServiceCatalogItem).where(ServiceCatalogItem.service_key == data["service_key"])
        )
        if existing.scalar_one_or_none():
            raise ValueError(f"service_key '{data['service_key']}' already exists")
        service = ServiceCatalogItem(id=new_uuid(), **data)
        db.add(service)
        await db.flush()
        await db.refresh(service)
        return service

    async def admin_list_allowances(
        self, db: AsyncSession, plan: Optional[str] = None,
    ) -> List[ComplimentaryAllowance]:
        query = select(ComplimentaryAllowance)
        if plan:
            query = query.where(ComplimentaryAllowance.plan == plan)
        result = await db.execute(query.order_by(ComplimentaryAllowance.plan, ComplimentaryAllowance.service_key))
        return list(result.scalars().all())

    async def admin_create_allowance(self, db: AsyncSession, data: Dict) -> ComplimentaryAllowance:
        # service_key must reference a real catalog item — surface a clean
        # 404-able error rather than letting the FK violation bubble up as
        # a raw IntegrityError (the exact class of bug that crashed startup
        # before ensure_seeded() was fixed to flush in the right order).
        await self.get_service(db, data["service_key"])
        existing = await db.execute(
            select(ComplimentaryAllowance).where(
                ComplimentaryAllowance.plan == data["plan"],
                ComplimentaryAllowance.service_key == data["service_key"],
            )
        )
        if existing.scalar_one_or_none():
            raise ValueError(f"An allowance for plan '{data['plan']}' + service '{data['service_key']}' already exists — edit it instead.")
        allowance = ComplimentaryAllowance(id=new_uuid(), **data)
        db.add(allowance)
        await db.flush()
        await db.refresh(allowance)
        return allowance

    async def admin_update_allowance(
        self, db: AsyncSession, allowance_id: str, updates: Dict,
    ) -> ComplimentaryAllowance:
        result = await db.execute(select(ComplimentaryAllowance).where(ComplimentaryAllowance.id == allowance_id))
        allowance = result.scalar_one_or_none()
        if not allowance:
            raise AllowanceNotFoundError(allowance_id)
        for field in ("quantity", "validity_days"):
            value = updates.get(field, None)
            if value is not None:
                setattr(allowance, field, value)
        await db.flush()
        await db.refresh(allowance)
        return allowance
