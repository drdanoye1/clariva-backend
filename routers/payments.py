"""
Payments router — Square checkout link generation.
Uses Square's Payment Links API (v2/online-checkout/payment-links).
Location ID is auto-fetched from Square's Locations API on first use.
"""
from __future__ import annotations

import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from config import settings
from models.db_models import User
from routers.auth import get_current_user

router = APIRouter()

# ── Plan catalogue ────────────────────────────────────────────────────────────
# Amount in cents (USD). Starter is free — no checkout needed.

PLANS = {
    "builder": {
        "name":        "AtiFixia Builder",
        "amount":      3900,
        "description": "AtiFixia Builder Plan — 1,000 AI credits/month for grant proposal generation.",
    },
    "innovator": {
        "name":        "AtiFixia Innovator",
        "amount":      12900,
        "description": "AtiFixia Innovator Plan — 5,000 AI credits/month including SBIR Phase I & II.",
    },
    "professional": {
        "name":        "AtiFixia Professional",
        "amount":      24900,
        "description": "AtiFixia Professional Plan — 10,000+ AI credits/month with priority support.",
    },
}

PAYGO_PACKS = {
    "paygo_starter":      {"name": "100 Credits Pack",   "amount":  1000, "description": "AtiFixia — 100 pay-as-you-go AI credits."},
    "paygo_builder":      {"name": "300 Credits Pack",   "amount":  2500, "description": "AtiFixia — 300 pay-as-you-go AI credits."},
    "paygo_innovator":    {"name": "1,000 Credits Pack", "amount":  7500, "description": "AtiFixia — 1,000 pay-as-you-go AI credits."},
    "paygo_professional": {"name": "2,500 Credits Pack", "amount": 15000, "description": "AtiFixia — 2,500 pay-as-you-go AI credits."},
}

ALL_PRODUCTS = {**PLANS, **PAYGO_PACKS}

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


# ── Request / Response models ─────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    plan_id: str
    redirect_url: Optional[str] = None


class CheckoutResponse(BaseModel):
    checkout_url: str
    plan_id: str
    plan_name: str
    amount_cents: int


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/create-checkout", response_model=CheckoutResponse)
async def create_checkout(
    body: CheckoutRequest,
    current_user: User = Depends(get_current_user),
):
    product = ALL_PRODUCTS.get(body.plan_id)
    if not product:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {body.plan_id}")

    base_url, access_token = _square_config()
    location_id = await _get_location_id(base_url, access_token)
    redirect = body.redirect_url or "https://grant.aistartupcopilot.org/dashboard"

    payload = {
        "idempotency_key": str(uuid.uuid4()),
        "quick_pay": {
            "name":        product["name"],
            "price_money": {"amount": product["amount"], "currency": "USD"},
            "location_id": location_id,
        },
        "checkout_options": {
            "redirect_url":           redirect,
            "merchant_support_email": "info@aistartupcopilot.org",
        },
        "pre_populated_data": {
            "buyer_email": current_user.email,
        },
        "description": product.get("description", ""),
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
        )

    if resp.status_code not in (200, 201):
        detail = resp.json().get("errors", [{}])[0].get("detail", "Square checkout error.")
        raise HTTPException(status_code=502, detail=detail)

    data = resp.json().get("payment_link", {})
    return CheckoutResponse(
        checkout_url=data["url"],
        plan_id=body.plan_id,
        plan_name=product["name"],
        amount_cents=product["amount"],
    )


@router.get("/plans")
async def list_plans():
    """Return available plans and pay-as-you-go packs (no auth required)."""
    return {
        "plans": [
            {"id": k, "name": v["name"], "amount_cents": v["amount"]}
            for k, v in PLANS.items()
        ],
        "paygo": [
            {"id": k, "name": v["name"], "amount_cents": v["amount"]}
            for k, v in PAYGO_PACKS.items()
        ],
    }
