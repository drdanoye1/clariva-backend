"""
Funding Opportunity Fit Score (FOFS) engine — Funding Opportunity
Intelligence, Phase 2.

`score_opportunity()` is pure, deterministic Python (explicitly NOT an LLM
call — see engines/fit_score_engine.py's module docstring), so like
ScoringEngine._build_result() in test_scoring_engine.py, it's exercised
directly with SimpleNamespace stand-ins for FOARecord/OrgContextDB rather
than real ORM instances — only attribute access is needed, no DB session.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from engines.fit_score_engine import score_opportunity


def _record(**overrides) -> SimpleNamespace:
    defaults = dict(
        agency=None, program_title=None, phase=None, eligibility_summary=None,
        estimated_award_floor=None, estimated_award_ceiling=None, deadline=None,
        eligibility_status=None, complexity=None, attractiveness=None,
        intelligence_report=None, ai_summary=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _profile(**overrides) -> SimpleNamespace:
    defaults = dict(
        organization_name=None, industry=None, mission_statement=None, core_technologies=[],
        industries=[], certifications=[], entity_type=None, prior_sbir_experience=False,
        past_performance=[], funding_preferences={}, service_geography=[], team_members=[],
        company_capabilities=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_no_profile_returns_none():
    """No Funding Intelligence Profile on file -> no fit score at all, not a misleading default."""
    assert score_opportunity(_record(), None) is None


def test_fully_specified_strong_match_scores_high_and_recommends_pursuit():
    record = _record(
        agency="NSF", program_title="Nanomaterials for Sensor Research", phase="phase_i",
        eligibility_summary="Small businesses with prior SBIR experience are eligible nationwide.",
        estimated_award_floor=100_000, estimated_award_ceiling=250_000,
        deadline=datetime.now(timezone.utc) + timedelta(days=45),
        eligibility_status="Eligible", complexity="Moderate", attractiveness="High",
        intelligence_report={
            "eligibility_assessment": {"status": "Eligible", "issues": []},
            "funding_priorities": ["nanotechnology", "sensors"],
            "requirements": ["prototype demonstration"],
            "cost_share": {"required": False},
        },
    )
    profile = _profile(
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

    result = score_opportunity(record, profile)

    assert result.overall_score >= 80
    assert result.bucket == "Strong Match"
    assert result.recommendation == "STRONG PURSUE"
    assert len(result.categories) == 7
    assert {c.key for c in result.categories} == {
        "eligibility", "strategic_alignment", "capability_fit",
        "applicant_geographic_fit", "funding_fit", "readiness", "timing_feasibility",
    }
    # Weights sum to 1.0 per spec.
    assert round(sum(c.weight for c in result.categories), 4) == 1.0
    eligibility = next(c for c in result.categories if c.key == "eligibility")
    assert eligibility.score == 95
    assert "Eligible" in eligibility.explanation


def test_sparse_record_and_profile_scores_neutral_not_penalized():
    """Missing data on both sides should read as 'unscored', not 'bad fit' — every
    category should land on the neutral midpoint and say what's missing."""
    record = _record(agency="DOI", program_title="Park Service Internship", phase="n/a")
    profile = _profile(organization_name="X")

    result = score_opportunity(record, profile)

    assert result.overall_score == 50
    assert result.bucket == "Possible Match"
    for c in result.categories:
        assert c.score == 50
        assert "Add more detail to your Funding Intelligence Profile" in c.explanation


def test_unlikely_eligibility_forces_no_go_even_with_good_alignment():
    """Eligibility is a near-hard gate — a great strategic/capability fit
    should not override a solicitation the org almost certainly can't win."""
    record = _record(
        program_title="Nanomaterials for Sensor Research",
        eligibility_status="Unlikely",
        intelligence_report={"eligibility_assessment": {"status": "Unlikely", "issues": ["Foreign-owned entities excluded"]}},
    )
    profile = _profile(mission_statement="Nanomaterials for Sensor Research", industries=["nanotechnology"])

    result = score_opportunity(record, profile)

    eligibility = next(c for c in result.categories if c.key == "eligibility")
    assert eligibility.score == 10
    assert result.recommendation == "NO-GO"


def test_funding_fit_penalizes_award_below_minimum():
    record = _record(estimated_award_floor=5_000, estimated_award_ceiling=10_000)
    profile = _profile(funding_preferences={"min_award": 50_000})

    result = score_opportunity(record, profile)
    funding = next(c for c in result.categories if c.key == "funding_fit")
    assert funding.score == 30
    assert "below your stated minimum" in funding.explanation


def test_readiness_penalizes_tight_deadline():
    record = _record(deadline=datetime.now(timezone.utc) + timedelta(days=3))
    profile = _profile()

    result = score_opportunity(record, profile)
    readiness = next(c for c in result.categories if c.key == "readiness")
    assert readiness.score == 30
    assert "tight timeline" in readiness.explanation


def test_readiness_penalizes_unwanted_cost_share():
    record = _record(
        deadline=datetime.now(timezone.utc) + timedelta(days=90),
        intelligence_report={"cost_share": {"required": True}},
    )
    profile = _profile(funding_preferences={"cost_share_tolerance": "none"})

    result = score_opportunity(record, profile)
    readiness = next(c for c in result.categories if c.key == "readiness")
    # Base score for 90 days out (95) minus the 30-point cost-share penalty.
    assert readiness.score == 65
    assert "cost-share/match" in readiness.explanation


def test_timing_feasibility_penalizes_high_complexity_for_small_inexperienced_team():
    record = _record(complexity="Very High")
    profile = _profile(prior_sbir_experience=False, past_performance=[], team_members=[{"name": "solo"}])

    result = score_opportunity(record, profile)
    timing = next(c for c in result.categories if c.key == "timing_feasibility")
    assert timing.score == 15  # base 30 for "very high" (unmapped -> default) minus 15
    assert "small team" in timing.explanation


def test_overall_score_is_weighted_sum_of_categories():
    record = _record()
    profile = _profile()
    result = score_opportunity(record, profile)
    expected = round(sum(c.weighted_points for c in result.categories))
    assert result.overall_score == expected
