"""
Engine 26 — Funding Strategy Intelligence
Funding Opportunity Intelligence, Phase 3 §4.6 ("Institutional Funding
Intelligence & Portfolio Optimization — Funding Strategy Intelligence").

What this is: an org-level strategic synthesis, not another per-opportunity
analysis. §4.1-4.5 all score or summarize things that already sit in the
pipeline (a single FOA, a portfolio ranking, an alert). This engine instead
reads ACROSS an org's whole pipeline, historical performance, and Funding
Intelligence Profile and asks GPT-4o to produce one coherent plan: which
agencies/programs to prioritize, how much funding to target, a quarterly
pursuit calendar, capability gaps to close, a partnership strategy, and a
proposal resource plan. Nothing here estimates a probability of winning any
specific future opportunity — same "descriptive/prioritization aid, never a
guarantee" discipline as historical_performance_engine.py and
portfolio_recommendation_engine.py, whose outputs this engine consumes
directly rather than recomputing.

Reuse over rebuilding (confirmed via research pass before this was written):
- HistoricalFundingPerformanceEngine.get_performance() — win rate, cycle
  time, dollar totals, agency/program-type/funding-range breakdowns.
- PortfolioRecommendationEngine.get_recommendations() — the current
  Fit-Score-ranked shortlist, pursuit capacity/status, resource conflicts.
- engines/company_profile.py::get_org_context() — the Funding Intelligence
  Profile (organization overview, core technologies, capabilities).
No engine before this one combined "aggregate many DB records into one
prompt" (supporting_documents_engine.py's convention) with "force a fixed
JSON schema response, then setdefault()-normalize it" (foa_parser.py's
convention) — this is the first to do both.

Persistence: one FundingStrategyPlan row per org (models/db_models.py),
upserted on every generation — see that model's docstring for why this is
"current state" rather than a versioned history table.

Design notes (same discipline as the other Phase-numbered engines):
- Every method takes an already-open AsyncSession and only flushes, never
  commits — the caller's request-scoped session commits once.
- This engine does not gate on RBAC, plan tier, or pricing — that's the
  calling router's job (routers/funding_intelligence.py), matching how
  foa_parser.py's analyze_opportunity() and portfolio_recommendation_engine
  stay authorization-agnostic.
"""
from __future__ import annotations

import json
import logging
import traceback
from typing import Any, Dict, Optional

import openai
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from engines.company_profile import get_org_context
from engines.historical_performance_engine import HistoricalFundingPerformanceEngine
from engines.portfolio_recommendation_engine import PortfolioRecommendationEngine
from engines import usage_tracking
from engines.usage_tracking import usage_from_response
from models.db_models import FundingStrategyPlan

_log = logging.getLogger(__name__)

# Always present, always this exact text — never trusted to the model, same
# convention as foa_parser.py's INTELLIGENCE_REPORT_DISCLAIMER/
# HUMAN_IN_THE_LOOP_NOTE (module-level constants, appended in
# _normalize_plan() below rather than requested from GPT-4o).
STRATEGY_DISCLAIMER = (
    "This strategic plan is AI-generated decision support, synthesized from "
    "your organization's own pipeline, historical performance, and Funding "
    "Intelligence Profile. It is not a guarantee of funding, an estimate of "
    "win probability, or legal, financial, or compliance advice. Verify "
    "every figure, deadline, and recommendation before acting on it."
)
STRATEGY_HUMAN_IN_THE_LOOP_NOTE = (
    "A qualified person at your organization must review this plan and "
    "decide what to actually prioritize, target, or commit to. Clariva "
    "does not allocate budget, commit your organization, or make strategic "
    "decisions on its own — this plan is an input to your team's judgment, "
    "not a substitute for it."
)

STRATEGY_PROMPT = """You are an expert federal/institutional grant strategy advisor helping an organization plan its funding pursuit strategy for the year ahead.

Using ONLY the organization data provided below, produce a strategic funding plan as valid JSON with exactly this shape (use empty lists/strings where you genuinely have insufficient data — never invent agencies, programs, dollar figures, or partners that aren't grounded in the data given):

{{
  "executive_summary": "2-4 sentence plain-language overview of the recommended strategy",
  "priority_agencies_programs": [
    {{"agency": "...", "program_area": "...", "rationale": "why this agency/program area, grounded in the org's historical performance and/or current pipeline"}}
  ],
  "target_funding": {{
    "annual_target": "a target dollar amount or range for the year, as a string (e.g. \\"$2M-$3M\\")",
    "rationale": "why this target, grounded in historical win rate and pipeline value"
  }},
  "quarterly_pursuit_calendar": [
    {{"quarter": "Q1 2026", "focus": "what to prioritize pursuing this quarter", "notes": "deadlines, capacity, or sequencing considerations"}}
  ],
  "capability_gaps": [
    {{"gap": "a capability, credential, or capacity the org appears to be missing", "impact": "what this gap costs the org competitively", "recommended_action": "a concrete step to close it"}}
  ],
  "partnership_strategy": [
    {{"partner_type": "e.g. university research partner, subcontractor, prime contractor", "rationale": "why this kind of partnership helps", "target_profile": "what to look for in a partner"}}
  ],
  "proposal_resource_plan": [
    {{"period": "e.g. Q1 2026", "resource_need": "staffing, writing capacity, or subject-matter expertise needed", "rationale": "why, grounded in the pursuit calendar above"}}
  ]
}}

Never state or imply a numeric probability of winning any specific opportunity. Every recommendation must be traceable to the data below — if the data is too thin to recommend something specific, say so briefly in executive_summary rather than fabricating detail.

ORGANIZATION PROFILE:
{profile_block}

HISTORICAL FUNDING PERFORMANCE:
{performance_block}

CURRENT PIPELINE / PORTFOLIO RECOMMENDATIONS:
{portfolio_block}
"""


def _ai_error(exc: Exception) -> HTTPException:
    """Local copy per this codebase's convention (see the near-identical
    helper in engines/scope_of_work_engine.py and
    engines/supporting_documents_engine.py) — no provider names exposed."""
    if isinstance(exc, openai.RateLimitError):
        _log.error("AI service rate limit exceeded: %s", exc)
        return HTTPException(status_code=503, detail="The AI writing service is temporarily unavailable due to high demand. Please try again in a few minutes.")
    if isinstance(exc, openai.AuthenticationError):
        _log.critical("AI service authentication failure: %s", exc)
        return HTTPException(status_code=503, detail="The AI writing service is not configured correctly. Please contact support.")
    if isinstance(exc, openai.APIConnectionError):
        _log.error("AI service connection error: %s", exc)
        return HTTPException(status_code=503, detail="The AI writing service is unreachable. Please try again in a moment.")
    if isinstance(exc, openai.APIStatusError):
        _log.error("AI service API error %s: %s", getattr(exc, "status_code", "?"), getattr(exc, "message", str(exc)))
        return HTTPException(status_code=503, detail="The AI writing service returned an unexpected error. Please try again.")
    _log.error("Unexpected funding strategy generation error:\n%s", traceback.format_exc())
    return HTTPException(status_code=500, detail="Funding strategy generation failed. Please try again or contact support.")


class FundingStrategyEngine:
    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        self.performance_engine = HistoricalFundingPerformanceEngine()
        self.portfolio_engine = PortfolioRecommendationEngine()

    # ── JSON parsing (multi-strategy, same convention as foa_parser.py) ────

    def _parse_json(self, text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        print("[funding_strategy_engine] Could not parse JSON from LLM response, using defaults.")
        return {}

    # ── Prompt assembly ─────────────────────────────────────────────────────

    def _profile_block(self, profile: Any) -> str:
        """Same opt-in-line discipline as
        supporting_documents_engine.py::_profile_block, but reads directly
        off the OrgContextDB row (get_org_context()'s return type) rather
        than a pre-serialized dict."""
        if profile is None:
            return "No Funding Intelligence Profile has been filled out for this organization yet."
        lines = []
        if profile.organization_name:
            lines.append(f"Organization: {profile.organization_name}")
        if profile.industry:
            lines.append(f"Industry: {profile.industry}")
        if profile.core_technologies:
            lines.append(f"Core technologies: {', '.join(profile.core_technologies)}")
        if profile.company_capabilities:
            lines.append(f"Capabilities: {profile.company_capabilities}")
        if profile.prior_sbir_experience:
            lines.append("Has prior SBIR/STTR experience.")
        if profile.pursuit_capacity is not None:
            lines.append(f"Stated pursuit capacity: {profile.pursuit_capacity} active pursuits at a time.")
        return "\n".join(lines) if lines else "No Funding Intelligence Profile has been filled out for this organization yet."

    def _performance_block(self, performance: Dict[str, Any]) -> str:
        return json.dumps({
            "total_opportunities": performance.get("total_opportunities"),
            "win_rate": performance.get("win_rate"),
            "avg_cycle_time_days": performance.get("avg_cycle_time_days"),
            "pipeline_value": performance.get("pipeline_value"),
            "total_awarded_funding": performance.get("total_awarded_funding"),
            "by_agency": performance.get("by_agency", [])[:10],
            "by_program_type": performance.get("by_program_type", [])[:10],
            "by_funding_range": performance.get("by_funding_range", []),
        }, default=str)

    def _portfolio_block(self, recommendations: Dict[str, Any]) -> str:
        return json.dumps({
            "recommended_portfolio": recommendations.get("recommended_portfolio", [])[:15],
            "by_stage": recommendations.get("by_stage", {}),
            "pursuit_capacity": recommendations.get("pursuit_capacity"),
            "currently_pursuing": recommendations.get("currently_pursuing"),
            "capacity_status": recommendations.get("capacity_status"),
            "resource_conflicts": recommendations.get("resource_conflicts", []),
        }, default=str)

    def _normalize_plan(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Fill in safe defaults for any field the model omitted, and always
        attach the fixed disclaimer/human-in-the-loop text (never trusted to
        the model) — mirrors foa_parser.py::_normalize_intelligence_report."""
        data.setdefault("executive_summary", None)
        data.setdefault("priority_agencies_programs", [])
        data.setdefault("target_funding", {"annual_target": None, "rationale": None})
        data.setdefault("quarterly_pursuit_calendar", [])
        data.setdefault("capability_gaps", [])
        data.setdefault("partnership_strategy", [])
        data.setdefault("proposal_resource_plan", [])
        data["disclaimer"] = STRATEGY_DISCLAIMER
        data["human_in_the_loop_note"] = STRATEGY_HUMAN_IN_THE_LOOP_NOTE
        return data

    # ── Public API ───────────────────────────────────────────────────────

    async def generate_plan(
        self, db: AsyncSession, org_id: str, user_id: Optional[str] = None,
        price_cents_charged: int = 0,
    ) -> Dict[str, Any]:
        """Synthesizes a fresh strategic plan from the org's current
        pipeline, historical performance, and profile, then upserts it as
        this org's FundingStrategyPlan row. Raises the shared _ai_error()
        HTTPException on any OpenAI failure, same pattern as
        scope_of_work_engine.py / supporting_documents_engine.py — the
        caller (router) doesn't need its own try/except for that.

        `price_cents_charged` — Phase 3 §4.7 — is whatever the router
        already charged via ServiceCatalogEngine.consume() before calling
        this, passed through purely so the AIUsageRecord written below
        carries real revenue alongside real COGS without this engine
        needing to know anything about pricing itself."""
        performance = await self.performance_engine.get_performance(db, org_id=org_id)
        recommendations = await self.portfolio_engine.get_recommendations(db, org_id=org_id, limit=15)
        profile = await get_org_context(db, org_id=org_id)

        prompt = STRATEGY_PROMPT.format(
            profile_block=self._profile_block(profile),
            performance_block=self._performance_block(performance),
            portfolio_block=self._portfolio_block(recommendations),
        )

        try:
            response = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an expert federal grant strategist advising an "
                            "organization on its multi-quarter funding pursuit strategy. "
                            "Always return valid JSON only. Never state or imply a "
                            "numeric win probability."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
            )
        except Exception as exc:
            raise _ai_error(exc)

        raw_json = response.choices[0].message.content or ""
        parsed = self._parse_json(raw_json)
        plan = self._normalize_plan(parsed)

        # Phase 3 §4.7 — Administrator-Only Engineering Economics.
        prompt_tokens, completion_tokens = usage_from_response(response)
        await usage_tracking.record_usage(
            db, operation="funding_strategy_intelligence", model=settings.OPENAI_MODEL,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            org_id=org_id, user_id=user_id, price_cents_charged=price_cents_charged,
        )

        row = await self._upsert_plan(db, org_id, user_id, plan)
        return self._to_out(row)

    async def get_plan(self, db: AsyncSession, org_id: str) -> Optional[Dict[str, Any]]:
        """Read-only fetch of the org's last-generated plan (or None if one
        has never been generated) — no AI call, no charge."""
        result = await db.execute(select(FundingStrategyPlan).where(FundingStrategyPlan.org_id == org_id))
        row = result.scalars().first()
        return self._to_out(row) if row else None

    async def _upsert_plan(
        self, db: AsyncSession, org_id: str, user_id: Optional[str], plan: Dict[str, Any],
    ) -> FundingStrategyPlan:
        result = await db.execute(select(FundingStrategyPlan).where(FundingStrategyPlan.org_id == org_id))
        row = result.scalars().first()
        if row:
            row.plan = plan
            row.generated_by = user_id
        else:
            row = FundingStrategyPlan(org_id=org_id, plan=plan, generated_by=user_id)
            db.add(row)
        await db.flush()
        await db.refresh(row)
        return row

    def _to_out(self, row: FundingStrategyPlan) -> Dict[str, Any]:
        return {
            "org_id": row.org_id,
            "generated_by": row.generated_by,
            "generated_at": row.generated_at,
            **row.plan,
        }
