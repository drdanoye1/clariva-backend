"""
Engine 24 — Portfolio-Level Recommendations
Funding Opportunity Intelligence, Phase 3 §4.3 ("Institutional Funding
Intelligence & Portfolio Optimization — Portfolio-Level Recommendations").

Reuse over rebuilding, same discipline as Phase 3.1 (Organizational
Learning + Historical Funding Performance): this engine computes nothing
that scores an opportunity's *fit* — that's engines/fit_score_engine.py's
job, unchanged — it only ranks/aggregates what that engine and
FundingIntelligenceEngine.list_pipeline already produce, plus the two
genuinely new pieces the spec asks for that had no existing equivalent:
pursuit-capacity analysis (OrgContextDB.pursuit_capacity, added alongside
this engine) and deadline/resource-conflict detection.

Deliberately descriptive, never predictive: every recommended opportunity
carries its Fit Score and the engine's plain-language reasoning
(fit_score_engine.py's existing explainable-scoring discipline), and the
response always includes DISCLAIMER, which the frontend renders next to
the list unconditionally. Per the spec's explicit instruction,
"strategically weighted pipeline indicators should be clearly labeled and
should not imply guaranteed revenue" — nothing here is a probability or a
promise, only a prioritization aid over the org's own data.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from engines.company_profile import get_org_context
from engines.fit_score_engine import score_opportunity
from engines.funding_intelligence_engine import FundingIntelligenceEngine
from models.db_models import FOARecord

DISCLAIMER = (
    "This is a prioritization aid built from your own pipeline data and Fit "
    "Score — not a guarantee of funding or an estimate of win probability. "
    "Always apply human judgment for the final Bid/No-Go call."
)

# A recommendation only ever draws from opportunities still open to a
# decision — nothing already submitted, awarded, declined, or no-go'd
# (there is nothing left to "recommend pursuing" about those).
RECOMMENDABLE_STAGES = ("identified", "qualifying")
# "Actively pursued" for capacity/conflict purposes — broader than
# RECOMMENDABLE_STAGES on purpose: qualifying is still being decided,
# pursuing is already committed, and both consume real team bandwidth.
ACTIVE_PURSUIT_STAGES = ("qualifying", "pursuing")
# Fit Score recommendations worth surfacing as "recommended to pursue" —
# CONDITIONAL PURSUE/LOW PRIORITY/NO-GO opportunities are left in the
# pipeline board but not promoted into this list.
PURSUE_RECOMMENDATIONS = ("STRONG PURSUE", "PURSUE")
# Two actively-pursued opportunities whose deadlines fall within this many
# days of each other are flagged as a potential resource conflict.
CONFLICT_WINDOW_DAYS = 14
DEFAULT_LIMIT = 10


class PortfolioRecommendationEngine:
    def __init__(self):
        self.fi_engine = FundingIntelligenceEngine()

    async def get_recommendations(
        self, db: AsyncSession, org_id: Optional[str] = None, uploaded_by: Optional[str] = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Dict[str, Any]:
        records = await self.fi_engine.list_pipeline(db, org_id=org_id, uploaded_by=uploaded_by)
        profile = await get_org_context(db, user_id=uploaded_by, org_id=org_id)

        by_stage: Dict[str, int] = {}
        ranked: List[tuple] = []
        for r in records:
            by_stage[r.pipeline_stage] = by_stage.get(r.pipeline_stage, 0) + 1
            if r.pipeline_stage not in RECOMMENDABLE_STAGES:
                continue
            fit = score_opportunity(r, profile)
            if not fit or fit.recommendation not in PURSUE_RECOMMENDATIONS:
                continue
            ranked.append((r, fit))

        ranked.sort(key=lambda pair: pair[1].overall_score, reverse=True)
        ranked = ranked[:limit]

        recommended_portfolio = [
            {
                "foa_id": r.id,
                "program_title": r.program_title,
                "agency": r.agency,
                "deadline": r.deadline.isoformat() if r.deadline else None,
                "fit_score": fit.overall_score,
                "recommendation": fit.recommendation,
                "rationale": fit.recommendation_reason,
            }
            for r, fit in ranked
        ]

        pursuing_count = sum(by_stage.get(s, 0) for s in ACTIVE_PURSUIT_STAGES)
        pursuit_capacity = profile.pursuit_capacity if profile else None
        if pursuit_capacity is None:
            capacity_status = None
        elif pursuing_count > pursuit_capacity:
            capacity_status = "over"
        elif pursuing_count == pursuit_capacity:
            capacity_status = "at"
        else:
            capacity_status = "under"

        active = [r for r in records if r.pipeline_stage in ACTIVE_PURSUIT_STAGES]
        resource_conflicts = self._find_conflicts(active)

        return {
            "recommended_portfolio": recommended_portfolio,
            "by_stage": by_stage,
            "pursuit_capacity": pursuit_capacity,
            "currently_pursuing": pursuing_count,
            "capacity_status": capacity_status,
            "resource_conflicts": resource_conflicts,
            "disclaimer": DISCLAIMER,
        }

    def _find_conflicts(self, records: List[FOARecord]) -> List[Dict[str, Any]]:
        """Greedily clusters actively-pursued opportunities whose deadlines
        fall within CONFLICT_WINDOW_DAYS of each other, earliest-deadline
        first. Each opportunity is consumed into at most one cluster, so
        overlapping windows don't produce duplicate/overlapping conflict
        entries — deliberately simple (no severity weighting, no per-day
        effort estimate) for this first pass; see docs/ARCHITECTURE.md's
        Phase 3.2 section for what's explicitly deferred."""
        dated = sorted((r for r in records if r.deadline), key=lambda r: r.deadline)
        conflicts: List[Dict[str, Any]] = []
        consumed: set = set()
        for i, a in enumerate(dated):
            if a.id in consumed:
                continue
            cluster = [a]
            for b in dated[i + 1:]:
                if b.id in consumed:
                    continue
                if (b.deadline - a.deadline).days <= CONFLICT_WINDOW_DAYS:
                    cluster.append(b)
                else:
                    break
            if len(cluster) > 1:
                for r in cluster:
                    consumed.add(r.id)
                conflicts.append({
                    "foa_ids": [r.id for r in cluster],
                    "titles": [r.program_title for r in cluster],
                    "deadline_window_days": CONFLICT_WINDOW_DAYS,
                    "message": (
                        f"{len(cluster)} actively-pursued opportunities have deadlines "
                        f"within {CONFLICT_WINDOW_DAYS} days of each other — review team "
                        f"capacity before committing to all of them."
                    ),
                })
        return conflicts
