"""
Payments router — Square checkout link generation + webhook.

Uses Square's Payment Links API (v2/online-checkout/payment-links), built
from an `order` (not `quick_pay`) so a `metadata` map can ride along on the
Order and come back to us on the webhook — that's how the webhook knows
which org and which product a given completed payment was for. Location ID
is auto-fetched from Square's Locations API on first use.

Post-launch addendum (real-money payment wiring): before this addendum,
`create_checkout` built a Square Payment Link and nothing else — a customer
could pay and nothing in the app would ever change (Organization.plan
never got set). This file now also owns:
  - POST /payments/create-fund-checkout — self-serve AI Services Fund
    top-up (pay real money, get it credited straight to the org's
    AICreditLedger balance). Was a "Coming Soon" teaser on pricing.tsx;
    now live.
  - POST /payments/webhook — Square calls this when a payment completes.
    Verifies the HMAC signature, dedupes via SquareWebhookEvent (Square
    redelivers on anything but a 2xx response), fetches the Order for its
    metadata, and applies the effect: either activates an org's plan
    (Organization.plan + plan_expires_at) or credits the AI Services Fund
    top-up amount (CreditEngine.credit()).

See docs/ARCHITECTURE.md for the full design, the Square dashboard
configuration steps, and the paired scripts/downgrade_expired_plans.py
daily job that expires a plan once plan_expires_at passes (Square Payment
Links are one-time charges, not real recurring subscriptions — this app is
the sole source of truth for whether a plan is still current).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from audit import log_action
from config import settings
from database import get_db
from engines.credit_engine import CreditEngine
from models.db_models import Organization, SquareWebhookEvent, User, new_uuid
from routers.auth import get_current_user
from routers.organizations import _assert_permission

_log = logging.getLogger(__name__)

router = APIRouter()
credit_engine = CreditEngine()

# ── Plan catalogue ────────────────────────────────────────────────────────────
# Amount in cents (USD). Free tier has no checkout needed (5 grant-opportunity
# searches/month, no other paid features — enforced client-side/product-side
# for now; see ARCHITECTURE.md "Phase 16 (cont'd) — Pricing & packaging
# overhaul" for the enforcement caveat).
#
# Version 3.0 architecture upgrade, Phase 16 (cont'd) — pricing/packaging
# overhaul per the "Enterprise Public-Facing Pricing & Internal Engineering
# Economics" spec (v1.0, Aug 2026), Phase 1 (public pricing update) of that
# doc's three-phase roadmap. Replaces the old $29/$79/$199 AI-credit-bundle
# plans (keys "builder"/"innovator"/"professional", which had ALREADY drifted
# out of sync with the live pricing page — the page sent "professional"/
# "organization"/"enterprise" while only "builder"/"innovator"/"professional"
# existed here, so Organization/Enterprise checkout 400'd and Professional
# checked out at $249 while the page advertised $29). Reconciled to one
# consistent set of planId strings shared by pricing.tsx and this dict:
# professional/team/organization/enterprise, each with a separate `_annual`
# key so the pricing page's monthly/annual toggle actually charges the
# amount it displays instead of always charging monthly regardless of
# toggle state (the previous behavior — a latent bug, not a deliberate
# design). Removed PAYGO_PACKS/ALL_PRODUCTS entirely: the spec replaces
# "buy a bundle of AI credits" with "pay per on-demand service, at a PAYG
# premium over the subscriber price" (Phase 2 marketplace scope) — the
# separate AI Services Fund top-up (create-fund-checkout, below) is that
# marketplace's funding mechanism, distinct from these subscription plans.
PLANS = {
    "professional": {
        "name":        "Clariva Professional",
        "amount":      3900,
        "description": "Clariva Professional — monthly platform access, all four grant lifecycle workspaces, complimentary AI services on signup.",
    },
    "professional_annual": {
        "name":        "Clariva Professional (Annual)",
        "amount":      39000,
        "description": "Clariva Professional — annual platform access, all four grant lifecycle workspaces, complimentary AI services on signup.",
    },
    "team": {
        "name":        "Clariva Team",
        "amount":      9900,
        "description": "Clariva Team — monthly platform access for up to 5 users, collaboration, organizational knowledge.",
    },
    "team_annual": {
        "name":        "Clariva Team (Annual)",
        "amount":      99000,
        "description": "Clariva Team — annual platform access for up to 5 users, collaboration, organizational knowledge.",
    },
    "organization": {
        "name":        "Clariva Organization",
        "amount":      24900,
        "description": "Clariva Organization — monthly platform access for up to 15 users, advanced administration, API access.",
    },
    "organization_annual": {
        "name":        "Clariva Organization (Annual)",
        "amount":      249000,
        "description": "Clariva Organization — annual platform access for up to 15 users, advanced administration, API access.",
    },
    "large": {
        "name":        "Clariva Large",
        "amount":      499900,
        "description": "Clariva Large — monthly platform access for large teams, expanded Managed Data, priority administration and support.",
    },
    "large_annual": {
        "name":        "Clariva Large (Annual)",
        "amount":      4999000,
        "description": "Clariva Large — annual platform access for large teams, expanded Managed Data, priority administration and support.",
    },
    "enterprise": {
        "name":        "Clariva Enterprise",
        # Commercial Architecture Phase 1: raised from 59900 ($599) to keep
        # Enterprise priced above the new Large tier ($4,999/mo,
        # PLANS["large"] above) — Large a Payment Link never existed against
        # a self-serve Enterprise checkout path anyway (planId is null on
        # the Enterprise card; it routes to /demo, never here), so this
        # figure is a marketing anchor only, not a live charge amount.
        "amount":      999900,
        "description": "Clariva Enterprise — starting monthly platform access; custom users, capacity, and support. Contact sales for a tailored quote.",
    },
}

# ── Public-facing tier display names ────────────────────────────────────────
# Commercial Architecture Phase 1 ("Standardized Subscription & Pricing
# Labeling" SOP, Aug 2026): the public plan *names* change, but the stored
# Organization.plan value and every tier-string comparison across the
# codebase (BULK_ALLOWED_PLANS, ComplimentaryAllowance.plan,
# service_catalog_engine._get_org_plan, admin plan-override endpoints, PLANS
# keys above, etc.) deliberately keep their existing internal values — no
# data migration, no rename of stored rows. This dict is the single lookup
# any UI surface uses to translate an internal tier into the new label.
# Existing customers keep their current billed price/tier; only the label
# they see changes (per explicit user decision, Aug 2026).
TIER_DISPLAY_NAMES = {
    "free":         "Starter",
    "professional": "Professional",
    "team":         "Small",
    "organization": "Medium",
    "large":        "Large",
    "enterprise":   "Enterprise",
}

# Every plan_id in PLANS maps to exactly one underlying tier stored in
# Organization.plan — the "_annual" suffix only changes amount/billing
# interval, never the tier itself, so it's stripped before writing to
# Organization.plan (service_catalog_engine.py's ComplimentaryAllowance
# lookups key on tier alone: "professional"/"team"/"organization", not on
# whether it was paid monthly or annually).
_ANNUAL_SUFFIX = "_annual"
PLAN_INTERVAL_DAYS = {False: 30, True: 365}  # keyed by "is_annual"

# Minimum self-serve AI Services Fund top-up — small enough to be usable,
# large enough that Square's flat per-transaction fee doesn't eat an
# unreasonable share of it.
MIN_FUND_TOPUP_CENTS = 1000  # $10.00

# ── Additional Seats ─────────────────────────────────────────────────────────
# Commercial Architecture Phase 1 — per-seat pricing for the three tiers the
# source spec's seat table covers (Small/Medium/Large = internal team/
# organization/large). Professional and Enterprise are deliberately absent:
# Professional is a single-user plan by design (no seat add-on), and
# Enterprise seats are negotiated as part of a custom quote (contact sales),
# not self-serve checkout — same reasoning pricing.tsx already uses for
# keeping Enterprise off the self-serve plan-checkout path.
SEAT_PRICES_CENTS = {
    "team":         2500,  # Small — $25/seat/month
    "organization": 2000,  # Medium — $20/seat/month
    "large":        1500,  # Large — $15/seat/month
}
MAX_SEATS_PER_PURCHASE = 100  # guards against a fat-fingered order-of-magnitude typo


def _plan_tier_and_interval(plan_id: str) -> tuple[str, int]:
    """('professional_annual', ...) -> ('professional', 365) etc."""
    if plan_id.endswith(_ANNUAL_SUFFIX):
        return plan_id[: -len(_ANNUAL_SUFFIX)], PLAN_INTERVAL_DAYS[True]
    return plan_id, PLAN_INTERVAL_DAYS[False]


# Module-level cache so we only call Square's Locations API once per process.
_cached_location_id: str | None = None


def _square_config() -> tuple[str, str]:
    """Return (base_url, access_token) based on SQUARE_ENVIRONMENT."""
    env = settings.SQUARE_ENVIRONMENT.lower()
    if env == "production":
        base_url = "https://connect.squareup.com"
        token    = settings.SQUARE_PRODUCTION_ACCESS_TOKEN
    else:
        base_url = "https://connect.squareupsandbox.com"
        token    = settings.SQUARE_SANDBOX_ACCESS_TOKEN
    if not token:
        raise HTTPException(
            status_code=503,
            detail="Square payment credentials not configured. Contact support.",
        )
    return base_url, token


async def _get_location_id(base_url: str, token: str) -> str:
    """
    Return a Square location ID.
    Uses SQUARE_LOCATION_ID from .env if set; otherwise auto-fetches the
    first active location from Square's Locations API and caches it.
    Online products still need a location internally for Square's ledger.
    """
    global _cached_location_id
    if settings.SQUARE_LOCATION_ID:
        return settings.SQUARE_LOCATION_ID
    if _cached_location_id:
        return _cached_location_id
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{base_url}/v2/locations",
            headers={"Authorization": f"Bearer {token}", "Square-Version": "2024-01-18"},
            timeout=10,
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=503, detail="Could not retrieve Square location.")
    locations = resp.json().get("locations", [])
    active = [loc for loc in locations if loc.get("status") == "ACTIVE"]
    if not active:
        raise HTTPException(status_code=503, detail="No active Square location found.")
    _cached_location_id = active[0]["id"]
    return _cached_location_id


async def _create_order_payment_link(
    *, base_url: str, access_token: str, location_id: str,
    item_name: str, amount_cents: int, description: str,
    metadata: dict, buyer_email: str, redirect_url: str,
) -> dict:
    """
    Shared builder for both the plan-subscription and Fund top-up checkout
    endpoints. Uses the `order` shape (not `quick_pay`) specifically so
    `order.metadata` can carry our own identifiers through to the webhook —
    Square's quick_pay shortcut has no metadata field.
    """
    payload = {
        "idempotency_key": str(uuid.uuid4()),
        "order": {
            "location_id": location_id,
            "line_items": [{
                "name":            item_name,
                "quantity":        "1",
                "base_price_money": {"amount": amount_cents, "currency": "USD"},
            }],
            # String keys/values only, per Square's Order.metadata contract.
            "metadata": {k: str(v) for k, v in metadata.items()},
        },
        "checkout_options": {
            "redirect_url":           redirect_url,
            "merchant_support_email": "info@aistartupcopilot.org",
        },
        "pre_populated_data": {
            "buyer_email": buyer_email,
        },
        "description": description,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/v2/online-checkout/payment-links",
            json=payload,
            headers={
                "Authorization":  f"Bearer {access_token}",
                "Square-Version": "2024-01-18",
                "Content-Type":   "application/json",
            },
            timeout=15,
        )

    if resp.status_code not in (200, 201):
        detail = resp.json().get("errors", [{}])[0].get("detail", "Square checkout error.")
        raise HTTPException(status_code=502, detail=detail)

    return resp.json().get("payment_link", {})


# ── Request / Response models ─────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    plan_id: str
    org_id: str
    redirect_url: Optional[str] = None


class CheckoutResponse(BaseModel):
    checkout_url: str
    plan_id: str
    plan_name: str
    amount_cents: int


class FundCheckoutRequest(BaseModel):
    org_id: str
    amount_cents: int = Field(ge=MIN_FUND_TOPUP_CENTS)
    redirect_url: Optional[str] = None


class FundCheckoutResponse(BaseModel):
    checkout_url: str
    org_id: str
    amount_cents: int


class SeatsCheckoutRequest(BaseModel):
    org_id: str
    seat_count: int = Field(ge=1, le=MAX_SEATS_PER_PURCHASE)
    redirect_url: Optional[str] = None


class SeatsCheckoutResponse(BaseModel):
    checkout_url: str
    org_id: str
    seat_count: int
    amount_cents: int


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/create-checkout", response_model=CheckoutResponse)
async def create_checkout(
    body: CheckoutRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Build a Square Payment Link for a subscription plan. Requires org_id
    (Organization.plan is org-level, not user-level) and manage_credits
    permission on that org — same gate as the manual credit top-up, since
    both are "spend the org's money" actions and there's no dedicated
    billing permission yet (see rbac.py).
    """
    product = PLANS.get(body.plan_id)
    if not product:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {body.plan_id}")

    await _assert_permission(body.org_id, current_user.id, "manage_credits", db)

    base_url, access_token = _square_config()
    location_id = await _get_location_id(base_url, access_token)
    redirect = body.redirect_url or "https://grant.aistartupcopilot.org/dashboard"

    payment_link = await _create_order_payment_link(
        base_url=base_url, access_token=access_token, location_id=location_id,
        item_name=product["name"], amount_cents=product["amount"], description=product.get("description", ""),
        metadata={
            "kind": "plan_subscription",
            "org_id": body.org_id,
            "plan_id": body.plan_id,
            "initiated_by_user_id": current_user.id,
        },
        buyer_email=current_user.email, redirect_url=redirect,
    )

    return CheckoutResponse(
        checkout_url=payment_link["url"],
        plan_id=body.plan_id,
        plan_name=product["name"],
        amount_cents=product["amount"],
    )


@router.post("/create-fund-checkout", response_model=FundCheckoutResponse)
async def create_fund_checkout(
    body: FundCheckoutRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Self-serve AI Services Fund top-up (Enterprise Pricing spec §6:
    "$1 deposited will equal $1 of purchasing power"). Was a "Coming Soon"
    teaser on pricing.tsx; this endpoint makes it real. Gated the same as
    the manual admin top-up (manage_credits) — a member who can already add
    free/manual credits can also pay real money to add them.
    """
    await _assert_permission(body.org_id, current_user.id, "manage_credits", db)

    base_url, access_token = _square_config()
    location_id = await _get_location_id(base_url, access_token)
    redirect = body.redirect_url or "https://grant.aistartupcopilot.org/dashboard"
    dollars = body.amount_cents / 100

    payment_link = await _create_order_payment_link(
        base_url=base_url, access_token=access_token, location_id=location_id,
        item_name="Clariva AI Services Fund Deposit",
        amount_cents=body.amount_cents,
        description=f"AI Services Fund deposit — ${dollars:,.2f} of purchasing power for on-demand AI services.",
        metadata={
            "kind": "fund_topup",
            "org_id": body.org_id,
            "amount_cents": body.amount_cents,
            "initiated_by_user_id": current_user.id,
        },
        buyer_email=current_user.email, redirect_url=redirect,
    )

    return FundCheckoutResponse(
        checkout_url=payment_link["url"], org_id=body.org_id, amount_cents=body.amount_cents,
    )


@router.post("/create-seats-checkout", response_model=SeatsCheckoutResponse)
async def create_seats_checkout(
    body: SeatsCheckoutRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Build a Square Payment Link for Additional Seats (Commercial
    Architecture Phase 1). Priced per-seat/month against the org's
    *current* plan tier (SEAT_PRICES_CENTS) — read fresh at checkout time
    rather than trusting a client-supplied tier, same "don't trust the
    client for anything billing touches" posture as create_checkout.
    Requires the org to already be on a tier with a seat price (Small/
    Medium/Large); Professional has no seat add-on and Enterprise seats are
    a contact-sales conversation, not self-serve checkout.
    """
    await _assert_permission(body.org_id, current_user.id, "manage_credits", db)

    org = (await db.execute(select(Organization).where(Organization.id == body.org_id))).scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found.")

    seat_price = SEAT_PRICES_CENTS.get(org.plan)
    if seat_price is None:
        raise HTTPException(
            status_code=400,
            detail="Additional seats are available on the Small, Medium, and Large plans. "
                   "Professional is single-user; Enterprise seats are part of your custom quote — contact sales.",
        )

    amount_cents = seat_price * body.seat_count

    base_url, access_token = _square_config()
    location_id = await _get_location_id(base_url, access_token)
    redirect = body.redirect_url or "https://grant.aistartupcopilot.org/org?seats=success"

    payment_link = await _create_order_payment_link(
        base_url=base_url, access_token=access_token, location_id=location_id,
        item_name=f"Clariva Additional Seats ({body.seat_count} × ${seat_price / 100:,.2f}/mo)",
        amount_cents=amount_cents,
        description=f"{body.seat_count} additional seat(s) for one month on the {TIER_DISPLAY_NAMES.get(org.plan, org.plan)} plan.",
        metadata={
            "kind": "seat_purchase",
            "org_id": body.org_id,
            "seat_count": body.seat_count,
            "plan_at_purchase": org.plan,
            "initiated_by_user_id": current_user.id,
        },
        buyer_email=current_user.email, redirect_url=redirect,
    )

    return SeatsCheckoutResponse(
        checkout_url=payment_link["url"], org_id=body.org_id,
        seat_count=body.seat_count, amount_cents=amount_cents,
    )


@router.get("/plans")
async def list_plans():
    """Return available plans (no auth required)."""
    return {
        "plans": [
            {"id": k, "name": v["name"], "amount_cents": v["amount"]}
            for k, v in PLANS.items()
        ],
    }


# ── Webhook ───────────────────────────────────────────────────────────────────

def _verify_square_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """
    Square's documented webhook signature algorithm: HMAC-SHA256 over
    (notification_url + raw request body), base64-encoded, compared against
    the `x-square-hmacsha256-signature` header. Uses the exact raw bytes of
    the body as received — re-serializing parsed JSON can reorder keys or
    change whitespace and would break the signature.
    """
    if not signature_header or not settings.SQUARE_WEBHOOK_SIGNATURE_KEY:
        return False
    if not settings.SQUARE_WEBHOOK_NOTIFICATION_URL:
        _log.error("SQUARE_WEBHOOK_NOTIFICATION_URL is not configured — cannot verify webhook signatures.")
        return False

    string_to_sign = settings.SQUARE_WEBHOOK_NOTIFICATION_URL + raw_body.decode("utf-8")
    digest = hmac.new(
        settings.SQUARE_WEBHOOK_SIGNATURE_KEY.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, signature_header)


async def _fetch_order_metadata(base_url: str, access_token: str, order_id: str) -> dict:
    """Fetch an Order via Square's Orders API and return its metadata map
    (empty dict if the order has none, or the fetch fails — a webhook that
    can't resolve its own metadata should no-op, not crash)."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{base_url}/v2/orders/{order_id}",
                headers={"Authorization": f"Bearer {access_token}", "Square-Version": "2024-01-18"},
                timeout=15,
            )
        if resp.status_code != 200:
            _log.error("Square Orders API returned %s for order %s.", resp.status_code, order_id)
            return {}
        return resp.json().get("order", {}).get("metadata", {}) or {}
    except Exception:
        _log.exception("Failed to fetch order %s from Square Orders API.", order_id)
        return {}


@router.post("/webhook")
async def square_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Square calls this on payment lifecycle events. No auth dependency —
    Square can't send a bearer token for our app; the HMAC signature (see
    _verify_square_signature) is the only trust boundary here, so it is
    NOT optional in production. Always returns 200 once the signature
    checks out and the event has been durably recorded (even if we then
    no-op on it), because Square retries on any non-2xx response — a 4xx/5xx
    here means "please send this again," not "reject."
    """
    raw_body = await request.body()
    signature = request.headers.get("x-square-hmacsha256-signature")

    if not _verify_square_signature(raw_body, signature):
        _log.warning("Rejected Square webhook — signature verification failed.")
        raise HTTPException(status_code=401, detail="Invalid signature.")

    try:
        event = json.loads(raw_body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed webhook payload.")

    event_id = event.get("event_id")
    event_type = event.get("type")
    if not event_id:
        # Square always sends one; if it's ever missing there's nothing safe
        # to dedupe on, so just no-op rather than risk double-processing.
        return {"status": "ignored", "reason": "missing event_id"}

    # Idempotency: try to claim this event_id before doing anything else.
    # A unique-constraint violation means we've already processed it
    # (Square redelivers on retries/timeouts) — return 200 and do nothing.
    db.add(SquareWebhookEvent(id=new_uuid(), square_event_id=event_id, event_type=event_type))
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return {"status": "duplicate", "event_id": event_id}

    if event_type != "payment.updated":
        await db.commit()
        return {"status": "ignored", "reason": f"unhandled event type {event_type}"}

    payment = (event.get("data") or {}).get("object", {}).get("payment") or {}
    if payment.get("status") != "COMPLETED":
        await db.commit()
        return {"status": "ignored", "reason": f"payment status {payment.get('status')}"}

    order_id = payment.get("order_id")
    if not order_id:
        await db.commit()
        return {"status": "ignored", "reason": "no order_id on payment"}

    base_url, access_token = _square_config()
    metadata = await _fetch_order_metadata(base_url, access_token, order_id)
    kind = metadata.get("kind")
    org_id = metadata.get("org_id")
    actor_id = metadata.get("initiated_by_user_id")

    if not kind or not org_id:
        _log.warning("Square webhook for order %s has no usable metadata (kind=%s org_id=%s).", order_id, kind, org_id)
        await db.commit()
        return {"status": "ignored", "reason": "no usable order metadata"}

    org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one_or_none()
    if org is None:
        _log.error("Square webhook for order %s references unknown org_id %s.", order_id, org_id)
        await db.commit()
        return {"status": "ignored", "reason": "unknown org_id"}

    if kind == "plan_subscription":
        plan_id = metadata.get("plan_id", "")
        if plan_id not in PLANS:
            # plan_id was validated against PLANS at checkout time (see
            # create_checkout), so this only fires if PLANS was edited
            # between checkout and webhook delivery — log and skip rather
            # than guess at a tier.
            _log.error("Square webhook plan_subscription for order %s has unknown plan_id %s.", order_id, plan_id)
            await db.commit()
            return {"status": "ignored", "reason": "unknown plan_id"}

        tier, interval_days = _plan_tier_and_interval(plan_id)
        org.plan = tier
        org.plan_expires_at = datetime.now(timezone.utc) + timedelta(days=interval_days)
        await db.flush()

        if actor_id:
            await log_action(
                db, actor_id=actor_id, action="billing.plan_activated", org_id=org_id,
                object_type="organization", object_id=org_id,
                detail={"plan_id": plan_id, "tier": tier, "expires_at": org.plan_expires_at.isoformat(), "square_order_id": order_id},
            )
        await db.commit()
        _log.info("Activated plan %s for org %s (expires %s) via Square order %s.", tier, org_id, org.plan_expires_at, order_id)
        return {"status": "applied", "kind": kind, "org_id": org_id, "plan": tier}

    if kind == "fund_topup":
        try:
            amount_cents = int(metadata.get("amount_cents", "0"))
        except ValueError:
            amount_cents = 0
        if amount_cents <= 0:
            await db.commit()
            return {"status": "ignored", "reason": "invalid amount_cents"}

        dollars = amount_cents / 100
        await credit_engine.credit(db, org_id, dollars, "square_fund_topup", user_id=actor_id)

        if actor_id:
            await log_action(
                db, actor_id=actor_id, action="billing.fund_topup", org_id=org_id,
                object_type="ai_credit_ledger", object_id=org_id,
                detail={"amount_cents": amount_cents, "square_order_id": order_id},
            )
        await db.commit()
        _log.info("Credited $%.2f AI Services Fund top-up to org %s via Square order %s.", dollars, org_id, order_id)
        return {"status": "applied", "kind": kind, "org_id": org_id, "amount_cents": amount_cents}

    if kind == "seat_purchase":
        try:
            seat_count = int(metadata.get("seat_count", "0"))
        except ValueError:
            seat_count = 0
        if seat_count <= 0:
            await db.commit()
            return {"status": "ignored", "reason": "invalid seat_count"}

        org.purchased_seats = (org.purchased_seats or 0) + seat_count
        await db.flush()

        if actor_id:
            await log_action(
                db, actor_id=actor_id, action="billing.seats_purchased", org_id=org_id,
                object_type="organization", object_id=org_id,
                detail={"seat_count": seat_count, "new_total": org.purchased_seats, "square_order_id": order_id},
            )
        await db.commit()
        _log.info("Added %d purchased seat(s) to org %s (total now %d) via Square order %s.", seat_count, org_id, org.purchased_seats, order_id)
        return {"status": "applied", "kind": kind, "org_id": org_id, "seat_count": seat_count, "total_purchased_seats": org.purchased_seats}

    _log.warning("Square webhook for order %s has unknown metadata kind %s.", order_id, kind)
    await db.commit()
    return {"status": "ignored", "reason": f"unknown kind {kind}"}
