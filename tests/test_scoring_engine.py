"""
Engine 6 — Scoring Engine.

`ScoringEngine.score()` calls OpenAI, so it isn't exercised directly here.
`_build_result()` is the pure, deterministic half of the engine — the actual
math (Score = Sigma(Wi * Si) - Risk Penalties, normalized to 0-100) — and is
what these tests lock down so future refactors can't silently change the
scoring formula.
"""
from __future__ import annotations

from types import SimpleNamespace

from engines.scoring_engine import ScoringEngine


def _section(section_id: str, weight: float) -> SimpleNamespace:
    return SimpleNamespace(section_id=section_id, evaluation_weight=weight)


def test_build_result_weights_and_normalizes_to_100():
    engine = ScoringEngine()
    sections = [_section("technical_merit", 0.6), _section("commercialization", 0.4)]
    raw = {
        "section_scores": [
            {"section_id": "technical_merit", "section_title": "Technical Merit", "score": 10.0},
            {"section_id": "commercialization", "section_title": "Commercialization", "score": 10.0},
        ],
        "risk_penalties": [],
        "dimension_scores": {
            "technical_merit": 10.0, "commercialization": 10.0,
            "innovation": 10.0, "team": 10.0, "compliance": 10.0,
        },
    }

    result = engine._build_result("proposal-1", raw, sections)

    # Every section scored a perfect 10 and weights sum to 1.0 -> perfect 100.
    assert result.total_score == 100.0
    assert result.proposal_id == "proposal-1"
    assert len(result.section_scores) == 2


def test_build_result_applies_risk_penalties():
    engine = ScoringEngine()
    sections = [_section("technical_merit", 1.0)]
    raw = {
        "section_scores": [
            {"section_id": "technical_merit", "section_title": "Technical Merit", "score": 10.0},
        ],
        "risk_penalties": [
            {"category": "compliance", "description": "Missing budget justification", "penalty": 15.0},
        ],
        "dimension_scores": {},
    }

    result = engine._build_result("proposal-2", raw, sections)

    assert result.total_score == 85.0  # 100 - 15 penalty
    assert len(result.risk_penalties) == 1
    assert result.risk_penalties[0].penalty == 15.0


def test_build_result_clamps_to_zero_when_penalties_exceed_score():
    engine = ScoringEngine()
    sections = [_section("technical_merit", 1.0)]
    raw = {
        "section_scores": [
            {"section_id": "technical_merit", "section_title": "Technical Merit", "score": 2.0},
        ],
        "risk_penalties": [
            {"category": "compliance", "description": "Severe non-compliance", "penalty": 50.0},
        ],
        "dimension_scores": {},
    }

    result = engine._build_result("proposal-3", raw, sections)

    assert result.total_score == 0.0  # clamped, never negative


def test_build_result_falls_back_to_dimension_defaults_when_missing():
    engine = ScoringEngine()
    sections = [_section("technical_merit", 1.0)]
    raw = {
        "section_scores": [
            {"section_id": "technical_merit", "section_title": "Technical Merit", "score": 7.0},
        ],
        "risk_penalties": [],
        "dimension_scores": {},  # AI omitted dimension scores entirely
    }

    result = engine._build_result("proposal-4", raw, sections)

    # Documented default fallback is 7.0 for every dimension when absent.
    assert result.technical_merit == 7.0
    assert result.compliance_score == 7.0
    assert result.commercialization == 7.0
    assert result.innovation == 7.0
    assert result.team_score == 7.0
