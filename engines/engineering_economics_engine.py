"""
Engine 27 — Administrator-Only Engineering Economics (Phase 3 §4.7)

Read-only aggregation over the ledgers Phase 3 §4.7's instrumentation pass
wrote: AIUsageRecord (COGS + revenue-charged-per-call, one row per LLM
call — see that model's docstring), AIServiceTransaction (per-service
paid/complimentary consumption, Phase 2's on-demand AI Services
Marketplace), and MarketplacePurchase (Phase 4's one-time listing
purchases). Nothing here is written by this engine — it exists purely to
answer "what does this platform actually cost to run, and is it making
money", the same "engines interoperate through shared models, read-only
where possible" discipline as portfolio_engine.py.

No permission checks live here — routers/admin.py gates every endpoint
that calls this engine behind require_superadmin, matching every other
`/admin/*` endpoint's existing convention.

Design notes:
- AIUsageRecord.cogs_cents/price_cents_charged are the single source of
  truth for revenue/COGS/margin — see that model's docstring for why this
  engine never joins back to AIServiceTransaction or CreditTransaction for
  those numbers (their `reason`/`reference` shapes differ per call site
  and would make that join fragile).
- Percentiles are computed in plain Python (nearest-rank interpolation,
  no numpy/pandas dependency) — consistent with this codebase's existing
  discipline of avoiding heavy numeric libraries for small in-memory
  aggregations (see fit_score_engine.py's plain-Python scoring for the
  same precedent). AIUsageRecord volume is not expected to be large
  enough to need SQL-side percentile functions (which also aren't
  portably available across SQLite and Postgres, this app's two
  supported databases).
- "Marketplace conversion" and "complimentary conversion" are two
  distinct metrics, both requested by the §4.7 spec's acceptance
  criteria: the former is purchase volume/revenue per published listing
  (there's no view/impression tracking yet — see MarketplaceListing's
  docstring — so this is purchases-per-listing, not a funnel rate); the
  latter is, per AI service, what fraction of orgs that have ever used
  the complimentary allowance have also paid for that same service.
"""
from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import (
    AIServiceTransaction, AIUsageRecord, MarketplaceListing, MarketplacePurchase,
    ModelPricingConfig, PlatformCostConfig,
)


def _percentile(sorted_values: List[float], pct: float) -> float:
    """Nearest-rank-interpolated percentile over an already-sorted list.
    Returns 0.0 for an empty input rather than raising — an operation with
    no recorded transactions yet is a normal, common state (e.g. right
    after a fresh deploy), not an error."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


class EngineeringEconomicsEngine:
    """Read-only. Every method takes an already-open AsyncSession and only
    reads — no flush/commit anywhere in this engine."""

    async def get_operation_economics(self, db: AsyncSession) -> List[Dict[str, Any]]:
        """Per-operation revenue/COGS/margin + token/cost distribution,
        grouped by AIUsageRecord.operation (the stable "family:action" or
        real service_key label every call site tags its usage row with —
        see that model's docstring)."""
        result = await db.execute(
            select(
                AIUsageRecord.operation, AIUsageRecord.prompt_tokens,
                AIUsageRecord.completion_tokens, AIUsageRecord.cogs_cents,
                AIUsageRecord.price_cents_charged,
            )
        )
        rows = result.all()

        by_op: Dict[str, List[Any]] = {}
        for op, prompt_tokens, completion_tokens, cogs_cents, price_cents in rows:
            by_op.setdefault(op, []).append((prompt_tokens, completion_tokens, cogs_cents, price_cents))

        operations: List[Dict[str, Any]] = []
        for op, items in sorted(by_op.items()):
            count = len(items)
            revenue_cents = sum(i[3] for i in items)
            cogs_total = sum(i[2] for i in items)
            cogs_sorted = sorted(i[2] for i in items)
            gross_margin_cents = revenue_cents - cogs_total
            gross_margin_pct = (gross_margin_cents / revenue_cents * 100.0) if revenue_cents > 0 else None
            operations.append({
                "operation": op,
                "transaction_count": count,
                "revenue_cents": revenue_cents,
                "cogs_cents": round(cogs_total, 4),
                "gross_margin_cents": round(gross_margin_cents, 4),
                "gross_margin_pct": round(gross_margin_pct, 2) if gross_margin_pct is not None else None,
                "avg_prompt_tokens": round(sum(i[0] for i in items) / count, 1),
                "avg_completion_tokens": round(sum(i[1] for i in items) / count, 1),
                "avg_cogs_cents": round(cogs_total / count, 4),
                "p50_cogs_cents": round(_percentile(cogs_sorted, 50), 4),
                "p75_cogs_cents": round(_percentile(cogs_sorted, 75), 4),
                "p95_cogs_cents": round(_percentile(cogs_sorted, 95), 4),
            })
        return operations

    def get_totals(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Platform-wide rollup — takes the already-computed per-operation
        list rather than re-querying, so get_dashboard() below only hits
        the DB once for AIUsageRecord."""
        transaction_count = sum(o["transaction_count"] for o in operations)
        revenue_cents = sum(o["revenue_cents"] for o in operations)
        cogs_cents = sum(o["cogs_cents"] for o in operations)
        gross_margin_cents = revenue_cents - cogs_cents
        gross_margin_pct = (gross_margin_cents / revenue_cents * 100.0) if revenue_cents > 0 else None
        return {
            "transaction_count": transaction_count,
            "revenue_cents": revenue_cents,
            "cogs_cents": round(cogs_cents, 4),
            "gross_margin_cents": round(gross_margin_cents, 4),
            "gross_margin_pct": round(gross_margin_pct, 2) if gross_margin_pct is not None else None,
        }

    async def get_marketplace_conversion(self, db: AsyncSession) -> Dict[str, Any]:
        """Purchase volume + revenue per listing, and what fraction of
        published listings have ever sold at all."""
        purchases_result = await db.execute(
            select(
                MarketplacePurchase.listing_id,
                func.count(MarketplacePurchase.id),
                func.sum(MarketplacePurchase.price_cents_paid),
            ).group_by(MarketplacePurchase.listing_id)
        )
        purchase_rows = purchases_result.all()

        listing_ids = [r[0] for r in purchase_rows]
        listings: Dict[str, str] = {}
        if listing_ids:
            listing_result = await db.execute(
                select(MarketplaceListing.id, MarketplaceListing.name).where(MarketplaceListing.id.in_(listing_ids))
            )
            listings = {row[0]: row[1] for row in listing_result.all()}

        published_count_result = await db.execute(
            select(func.count(MarketplaceListing.id)).where(MarketplaceListing.status == "published")
        )
        published_count = published_count_result.scalar() or 0

        by_listing = [
            {
                "listing_id": lid,
                "listing_name": listings.get(lid, "Unknown listing"),
                "purchase_count": cnt,
                "revenue_cents": total or 0,
            }
            for lid, cnt, total in purchase_rows
        ]
        by_listing.sort(key=lambda x: x["revenue_cents"], reverse=True)

        total_purchases = sum(b["purchase_count"] for b in by_listing)
        total_revenue_cents = sum(b["revenue_cents"] for b in by_listing)
        listings_with_sale = len(by_listing)
        conversion_pct = (listings_with_sale / published_count * 100.0) if published_count > 0 else None

        return {
            "published_listing_count": published_count,
            "listings_with_at_least_one_sale": listings_with_sale,
            "listing_conversion_pct": round(conversion_pct, 1) if conversion_pct is not None else None,
            "total_purchases": total_purchases,
            "total_revenue_cents": total_revenue_cents,
            "top_listings": by_listing[:10],
        }

    async def get_complimentary_conversion(self, db: AsyncSession) -> List[Dict[str, Any]]:
        """Per-service complimentary → paid conversion: of the orgs that
        have ever consumed a service's complimentary allowance, what
        fraction have also paid for that same service from their AI
        Services balance? Free-tier usage only matters if it leads to
        paid usage — this is that signal, per service_key."""
        result = await db.execute(
            select(
                AIServiceTransaction.service_key, AIServiceTransaction.org_id,
                AIServiceTransaction.funding_source,
            )
        )
        rows = result.all()

        comp_orgs: Dict[str, set] = {}
        paid_orgs: Dict[str, set] = {}
        for service_key, org_id, funding_source in rows:
            bucket = comp_orgs if funding_source == "complimentary" else paid_orgs
            bucket.setdefault(service_key, set()).add(org_id)

        service_keys = sorted(set(comp_orgs) | set(paid_orgs))
        breakdown: List[Dict[str, Any]] = []
        for key in service_keys:
            comp_set = comp_orgs.get(key, set())
            paid_set = paid_orgs.get(key, set())
            converted = comp_set & paid_set
            conversion_rate = (len(converted) / len(comp_set) * 100.0) if comp_set else None
            breakdown.append({
                "service_key": key,
                "orgs_used_complimentary": len(comp_set),
                "orgs_used_paid": len(paid_set),
                "orgs_converted": len(converted),
                "conversion_rate_pct": round(conversion_rate, 1) if conversion_rate is not None else None,
            })
        return breakdown

    async def get_platform_cost_config(self, db: AsyncSession) -> List[Dict[str, Any]]:
        result = await db.execute(select(PlatformCostConfig))
        rows = result.scalars().all()
        return [{"key": r.key, "label": r.label, "value_cents": r.value_cents} for r in rows]

    async def get_model_pricing(self, db: AsyncSession) -> List[Dict[str, Any]]:
        result = await db.execute(select(ModelPricingConfig).order_by(ModelPricingConfig.model))
        rows = result.scalars().all()
        return [
            {
                "model": r.model,
                "input_cost_cents_per_1k": r.input_cost_cents_per_1k,
                "output_cost_cents_per_1k": r.output_cost_cents_per_1k,
            }
            for r in rows
        ]

    async def get_dashboard(self, db: AsyncSession) -> Dict[str, Any]:
        """Single entry point routers/admin.py's GET /admin/economics
        calls — bundles every metric the §4.7 dashboard needs in one
        payload."""
        operations = await self.get_operation_economics(db)
        totals = self.get_totals(operations)
        marketplace = await self.get_marketplace_conversion(db)
        complimentary_conversion = await self.get_complimentary_conversion(db)
        platform_costs = await self.get_platform_cost_config(db)
        model_pricing = await self.get_model_pricing(db)
        return {
            "operations": operations,
            "totals": totals,
            "marketplace": marketplace,
            "complimentary_conversion": complimentary_conversion,
            "platform_costs": platform_costs,
            "model_pricing": model_pricing,
        }
