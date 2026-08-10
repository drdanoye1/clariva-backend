"""
Version 3.0 architecture upgrade, Phase 3.2 — Portfolio-Level
Recommendations (Engine 24). See docs/Clariva Funding Opportunity
Intelligence product definition upgrade for monetization_1.docx §4.3, and
engines/portfolio_recommendation_engine.py's module docstring for the
design rationale.

Same convention as test_organizational_learning.py: engine methods are
async and need a real DB session, so test bodies wrap themselves in
asyncio.run() rather than pulling in pytest-asyncio for just this file.
The `client` fixture is requested purely to trigger FastAPI's lifespan
startup (database.py::create_tables()).

NOTE: this sandbox session has no network access — `pip install` fails
outright, so this file could not actually be executed here. It follows
the exact same fixture/assertion patterns as test_organizational_learning.py
and test_fit_score_engine.py, which are known-good, executed precedents in
this codebase.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from database import AsyncSessionLocal
from engines.fit_score_engine import score_opportunity
from engines.funding_intelligence_engine import FundingIntelligenceEngine
from engines.portfolio_recommendation_engine import PortfolioRecommendationEngine
from models.db_models import FOARecord, OrgContextDB, new_uuid


def _run(coro):
    return asyncio.run(coro)


def _id(prefix: str) -> str:
    return f"test-{prefix}-{uuid.uuid4().hex[:12]}"


async def _make_foa(db, **overrides) -> FOARecord:
    defaults = dict(
        id=new_uuid(), agency="NSF", program_title="Nanomaterials for Sensor Research",
        phase="phase_i", grant_type="sbir", uploaded_by=_id("user"),
        pipeline_stage="identified",
        eligibility_summary="Small businesses with prior SBIR experience are eligible nationwide.",
        estimated_award_floor=100_000, estimated_award_ceiling=250_000,
        eligibility_status="Eligible", complexity="Moderate", attractiveness="High",
        intelligence_report={
            "eligibility_assessment": {"status": "Eligible", "issues": []},
            "funding_priorities": ["nanotechnology", "sensors"],
            "requirements": ["prototype demonstration"],
            "cost_share": {"required": False},
        },
    )
    defaults.update(overrides)
    record = FOARecord(**defaults)
    db.add(record)
    await db.flush()
    await db.refresh(record)
    return record


async def _make_profile(db, user_id: str, **overrides) -> OrgContextDB:
    defaults = dict(
        id=new_uuid(), user_id=user_id,
        organization_name="NanoResearch, Inc", industry="Nanotechnology",
        mission_statement="Advancing nanomaterial sensor technology for industrial applications.",
        core_technologies=["nanomaterials", "sensors"], industries=["nanotechnology"],
        certifications=["small business"], entity_type="small business", prior_sbir_experience=True,
        past_performance=[{"title": "DoD sensor prototype"}],
        funding_preferences={
            "min_award": 50_000, "max_award": 300_000, "preferred_agencies": ["NSF"],
            "preferred_phase": "phase_i", "cost_share_tolerance": "none",
        },
        service_geography=["nationwide"], team_members=[{"name": "a"}, {"name": "b"}, {"name": "c"}],
    )
    defaults.update(overrides)
    profile = OrgContextDB(**defaults)
    db.add(profile)
    await db.flush()
    await db.refresh(profile)
    return profile


# ── get_recommendations() ────────────────────────────────────────────────────

def test_ranks_high_fit_identified_and_qualifying_opportunities(client):
    """Only identified/qualifying-stage records that score STRONG PURSUE or
    PURSUE should appear in the shortlist — pursuing/submitted/etc. are
    already committed, and a low-fit record shouldn't be recommended just
    because it's early-stage."""
    user_id = _id("user")
    engine = PortfolioRecommendationEngine()

    async def _body():
        async with AsyncSessionLocal() as db:
            await _make_profile(db, user_id)
            strong = await _make_foa(db, uploaded_by=user_id, program_title="Strong Match Opportunity")
            weak = await _make_foa(
                db, uploaded_by=user_id, program_title="Weak Match Opportunity",
                agency="DOE", eligibility_summary=None, estimated_award_floor=None,
                estimated_award_ceiling=None, eligibility_status=None, complexity=None,
                attractiveness=None, intelligence_report=None,
            )
            already_pursuing = await _make_foa(
                db, uploaded_by=user_id, program_title="Already Pursuing",
                pipeline_stage="pursuing",
            )
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_recommendations(db, uploaded_by=user_id)

    result = _run(_body())
    titles = [r["program_title"] for r in result["recommended_portfolio"]]
    assert "Strong Match Opportunity" in titles
    assert "Weak Match Opportunity" not in titles
    assert "Already Pursuing" not in titles
    assert result["disclaimer"]


def test_no_profile_returns_empty_shortlist_but_still_reports_by_stage(client):
    user_id = _id("user")
    engine = PortfolioRecommendationEngine()

    async def _body():
        async with AsyncSessionLocal() as db:
            await _make_foa(db, uploaded_by=user_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_recommendations(db, uploaded_by=user_id)

    result = _run(_body())
    assert result["recommended_portfolio"] == []
    assert result["by_stage"]["identified"] == 1
    assert result["pursuit_capacity"] is None
    assert result["capacity_status"] is None


def test_capacity_status_under_at_over(client):
    user_id = _id("user")
    engine = PortfolioRecommendationEngine()

    async def _make_pursuing(db, uid, n):
        for i in range(n):
            await _make_foa(db, uploaded_by=uid, program_title=f"Pursuing {i}", pipeline_stage="pursuing")

    async def _body(capacity, n_pursuing):
        uid = _id("user")
        async with AsyncSessionLocal() as db:
            await _make_profile(db, uid, pursuit_capacity=capacity)
            await _make_pursuing(db, uid, n_pursuing)
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_recommendations(db, uploaded_by=uid)

    under = _run(_body(5, 2))
    assert under["capacity_status"] == "under"
    at = _run(_body(2, 2))
    assert at["capacity_status"] == "at"
    over = _run(_body(1, 2))
    assert over["capacity_status"] == "over"


def test_resource_conflicts_cluster_close_deadlines(client):
    """Two actively-pursued opportunities with deadlines inside the 14-day
    conflict window should surface as a conflict; one far outside it (or a
    single actively-pursued record) should not."""
    user_id = _id("user")
    engine = PortfolioRecommendationEngine()
    now = datetime.now(timezone.utc)

    async def _body():
        async with AsyncSessionLocal() as db:
            await _make_profile(db, user_id)
            await _make_foa(
                db, uploaded_by=user_id, program_title="Deadline A", pipeline_stage="pursuing",
                deadline=now + timedelta(days=10),
            )
            await _make_foa(
                db, uploaded_by=user_id, program_title="Deadline B", pipeline_stage="pursuing",
                deadline=now + timedelta(days=18),
            )
            await _make_foa(
                db, uploaded_by=user_id, program_title="Deadline Far Out", pipeline_stage="pursuing",
                deadline=now + timedelta(days=120),
            )
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_recommendations(db, uploaded_by=user_id)

    result = _run(_body())
    conflicts = result["resource_conflicts"]
    assert len(conflicts) == 1
    assert set(conflicts[0]["titles"]) == {"Deadline A", "Deadline B"}


def test_personal_and_org_scopes_do_not_leak(client):
    """Same isolation rule as FundingIntelligenceEngine.list_pipeline and
    OrganizationalLearningEngine.get_timeline — a personal record must not
    appear in another org's recommendations."""
    user_id = _id("user")
    org_id = _id("org")
    engine = PortfolioRecommendationEngine()

    async def _body():
        async with AsyncSessionLocal() as db:
            await _make_foa(db, uploaded_by=user_id, program_title="Personal Opportunity")
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_recommendations(db, org_id=org_id)

    result = _run(_body())
    assert result["by_stage"] == {}
