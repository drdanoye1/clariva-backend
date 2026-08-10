"""
Engine 23 — Historical Funding Performance (Version 3.0 architecture
upgrade, Phase 3.1 — "Institutional Funding Intelligence & Portfolio
Optimization", docs/Clariva Funding Opportunity Intelligence product
definition upgrade for monetization_1.docx §4.2).

FundingIntelligenceEngine.get_pipeline_report (Phase 4) already computes
the single-number aggregate this spec calls for — total opportunities,
win rate, average cycle time, open pipeline value — and already backs the
tiles on the Funding Pipeline board. This engine does NOT recompute any of
that; it calls get_pipeline_report and layers on the one thing the spec
explicitly asks for that didn't exist yet: breakdowns ("patterns by
agency, funding range, program type") plus the actual dollar total of
funding won (Award.total_award_value — ground truth once an Award exists,
not FOARecord's pre-award estimated ceiling).

Deliberately descriptive, not predictive: every number here is a count or
rate over what has already happened. Nothing here estimates the
probability that a *future*, not-yet-decided opportunity will be won —
see the monetization spec's explicit instruction not to present
"unsupported probabilities of winning."
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engines.funding_intelligence_engine import FundingIntelligenceEngine
from models.db_models import Award, FOARecord

# (inclusive lower bound, exclusive upper bound, label)
_FUNDING_RANGE_BUCKETS = [
    (0, 100_000, "Under $100K"),
    (100_000, 500_000, "$100K–$500K"),
    (500_000, 1_000_000, "$500K–$1M"),
    (1_000_000, float("inf"), "$1M+"),
]


def _funding_range_label(ceiling: Optional[float]) -> str:
    if ceiling is None:
        return "Unknown"
    for lo, hi, label in _FUNDING_RANGE_BUCKETS:
        if lo <= ceiling < hi:
            return label
    return "Unknown"


class HistoricalFundingPerformanceEngine:
    def __init__(self):
        self.fi_engine = FundingIntelligenceEngine()

    def _group_stats(self, records: List[FOARecord], key_fn: Callable[[FOARecord], Optional[str]]) -> List[Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}
        for r in records:
            key = key_fn(r) or "Unspecified"
            g = groups.setdefault(key, {"label": key, "total": 0, "awarded": 0, "lost": 0})
            g["total"] += 1
            if r.pipeline_stage == "awarded":
                g["awarded"] += 1
            elif r.pipeline_stage in ("declined", "no_go"):
                g["lost"] += 1
        out = []
        for g in groups.values():
            terminal = g["awarded"] + g["lost"]
            g["win_rate"] = (g["awarded"] / terminal) if terminal > 0 else None
            out.append(g)
        out.sort(key=lambda g: g["total"], reverse=True)
        return out

    async def get_performance(
        self, db: AsyncSession, org_id: Optional[str] = None, uploaded_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        records = await self.fi_engine.list_pipeline(db, org_id=org_id, uploaded_by=uploaded_by)
        base_report = await self.fi_engine.get_pipeline_report(db, org_id=org_id, uploaded_by=uploaded_by)

        total_awarded_funding = 0.0
        foa_ids = [r.id for r in records]
        if foa_ids:
            result = await db.execute(select(Award.total_award_value).where(Award.foa_id.in_(foa_ids)))
            total_awarded_funding = sum(v for (v,) in result.all() if v)

        return {
            **base_report,
            "total_awarded_funding": total_awarded_funding,
            "by_agency": self._group_stats(records, lambda r: r.agency),
            "by_program_type": self._group_stats(records, lambda r: r.grant_type),
            "by_funding_range": self._group_stats(records, lambda r: _funding_range_label(r.estimated_award_ceiling)),
        }
