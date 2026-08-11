"""
Engine 27 — Administrator-Only Engineering Economics (Phase 3 §4.7)
Shared usage/COGS instrumentation used by every live
`client.chat.completions.create()` call site across the platform.

This module is deliberately small and dependency-light — it does not
import any of the engines it instruments (they import *it*, one line
added right after their existing OpenAI call). Two entry points:

- `estimate_cost_cents(model, prompt_tokens, completion_tokens, pricing)`
  — pure function, no I/O, easy to unit test directly (same "pure
  helper, test it without mocking anything" discipline as
  foa_parser.py's `_parse_json`/`_normalize`).
- `record_usage(db, ...)` — writes one AIUsageRecord row. Best-effort and
  non-blocking, same precedent as audit.py's dispatch calls and
  credit_engine.py's `credits.balance_low` webhook: a usage-recording
  failure must never break the AI generation response that triggered it,
  so every exception here is caught and logged, never raised.

Seed data (`MODEL_PRICING_SEED`, `PLATFORM_COST_CONFIG_SEED`) mirrors
service_catalog_engine.py's SERVICE_CATALOG_SEED /
COMPLIMENTARY_ALLOWANCE_SEED pattern exactly: idempotent
`ensure_seeded()`, safe to call on every boot, wired into
main.py::_seed_service_catalog() alongside the existing catalog seed.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import AIUsageRecord, ModelPricingConfig, PlatformCostConfig, new_uuid

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Seed data — OpenAI's published per-1M-token rates as of Aug 2026 (see
# ModelPricingConfig's docstring for the exact figures/sources). Admin-
# editable from here on; this is only the starting point.
# ---------------------------------------------------------------------------
MODEL_PRICING_SEED = [
    # (model, input_cost_cents_per_1k, output_cost_cents_per_1k)
    ("gpt-4o", 0.25, 1.0),          # $2.50 / $10.00 per 1M tokens
    ("gpt-4o-mini", 0.015, 0.06),   # $0.15 / $0.60 per 1M tokens
]

# A call site whose `model=` value has no matching ModelPricingConfig row
# (e.g. settings.OPENAI_MODEL was changed to something not yet seeded)
# falls back to this rather than silently recording $0 COGS, which would
# understate cost without any visible signal that pricing is stale.
FALLBACK_PRICING = (0.25, 1.0)  # gpt-4o's rate — the most commonly used model here

PLATFORM_COST_CONFIG_SEED = [
    # (key, label, value_cents)
    ("search_api_cost_cents", "Search API cost (per call)", 0.0),
    ("rag_retrieval_cost_cents", "RAG / organizational-profile retrieval cost (per call)", 0.0),
]


def estimate_cost_cents(
    model: str, prompt_tokens: int, completion_tokens: int,
    pricing: Optional[Dict[str, tuple]] = None,
) -> float:
    """Pure function — no DB access. `pricing` maps model -> (input_cents_per_1k,
    output_cents_per_1k); callers that already have the ModelPricingConfig
    rows loaded (e.g. the Engineering Economics dashboard, recomputing
    historical COGS under a hypothetical new rate) pass it in directly.
    `record_usage()` below is the only caller that omits it and hits the DB."""
    rates = (pricing or {}).get(model) or FALLBACK_PRICING
    input_rate, output_rate = rates
    return (prompt_tokens / 1000.0) * input_rate + (completion_tokens / 1000.0) * output_rate


async def _get_pricing(db: AsyncSession, model: str) -> tuple:
    result = await db.execute(select(ModelPricingConfig).where(ModelPricingConfig.model == model))
    row = result.scalar_one_or_none()
    if row:
        return (row.input_cost_cents_per_1k, row.output_cost_cents_per_1k)
    return FALLBACK_PRICING


async def record_usage(
    db: AsyncSession, *, operation: str, model: str,
    prompt_tokens: int, completion_tokens: int,
    org_id: Optional[str] = None, user_id: Optional[str] = None,
    price_cents_charged: int = 0, reference: Optional[Dict[str, Any]] = None,
) -> Optional[AIUsageRecord]:
    """Writes one AIUsageRecord row. Flushes only (never commits — same
    convention as every other engine; the caller's request-scoped session
    commits once). Returns None (rather than raising) on any failure, so a
    usage-tracking bug can never surface as a 500 on an otherwise-successful
    AI generation — see module docstring."""
    try:
        input_rate, output_rate = await _get_pricing(db, model)
        cogs_cents = estimate_cost_cents(
            model, prompt_tokens, completion_tokens,
            pricing={model: (input_rate, output_rate)},
        )
        record = AIUsageRecord(
            id=new_uuid(), org_id=org_id, user_id=user_id, operation=operation, model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            cogs_cents=cogs_cents, price_cents_charged=price_cents_charged,
            reference=reference or {},
        )
        db.add(record)
        await db.flush()
        return record
    except Exception:
        _log.exception("Failed to record AI usage for operation=%s model=%s (non-fatal)", operation, model)
        return None


def usage_from_response(response: Any) -> tuple:
    """Extracts (prompt_tokens, completion_tokens) from an OpenAI chat
    completion response, tolerating a missing/None `.usage` (e.g. a
    monkeypatched fake response in tests) by returning (0, 0) rather than
    raising — usage tracking must never be why a call site breaks."""
    usage = getattr(response, "usage", None)
    if not usage:
        return (0, 0)
    return (getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0)


async def ensure_seeded(db: AsyncSession) -> None:
    """Idempotent: inserts any ModelPricingConfig / PlatformCostConfig row
    that doesn't exist yet. Safe to call on every app boot — same pattern
    as ServiceCatalogEngine.ensure_seeded()."""
    existing = await db.execute(select(ModelPricingConfig.model))
    existing_models = {row[0] for row in existing.all()}
    for model, input_rate, output_rate in MODEL_PRICING_SEED:
        if model in existing_models:
            continue
        db.add(ModelPricingConfig(
            id=new_uuid(), model=model,
            input_cost_cents_per_1k=input_rate, output_cost_cents_per_1k=output_rate,
        ))

    existing_cfg = await db.execute(select(PlatformCostConfig.key))
    existing_keys = {row[0] for row in existing_cfg.all()}
    for key, label, value_cents in PLATFORM_COST_CONFIG_SEED:
        if key in existing_keys:
            continue
        db.add(PlatformCostConfig(key=key, label=label, value_cents=value_cents))

    await db.flush()
