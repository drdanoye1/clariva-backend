"""
Engine 2 — FOA Template Builder Engine [core differentiator].
Pure transformation: FOAParserEngine.parse() output -> FOATemplate. No I/O.
"""
from __future__ import annotations

import pytest

from engines.template_builder import FOATemplateBuilderEngine
from models.schemas import Agency, Phase


def _parsed(**overrides):
    base = {
        "agency": "NSF",
        "phase": "phase_i",
        "program_title": "Test SBIR Program",
        "solicitation_number": "NSF-24-001",
        "total_page_limit": 15,
        "ordered_sections": [
            {"section_id": "project_summary", "title": "Project Summary",
             "required": True, "page_limit": 1, "guidance": "Summarize the project."},
            {"section_id": "technical_merit", "title": "Technical Merit",
             "required": True, "page_limit": 10, "guidance": "Describe the approach."},
        ],
        "weights": {"project_summary": 0.2, "technical_merit": 0.8},
        "compliance_rules": ["Font size minimum 11pt"],
        "deadline": None,
    }
    base.update(overrides)
    return base


def test_build_resolves_agency_and_phase():
    template = FOATemplateBuilderEngine().build(_parsed())
    assert template.agency == Agency.NSF
    assert template.phase == Phase.PHASE_I


def test_build_unknown_agency_falls_back_to_other():
    template = FOATemplateBuilderEngine().build(_parsed(agency="NOT_A_REAL_AGENCY"))
    assert template.agency == Agency.OTHER


def test_build_preserves_section_order_and_ids():
    template = FOATemplateBuilderEngine().build(_parsed())
    ids = [s.section_id for s in template.ordered_sections]
    assert ids == ["project_summary", "technical_merit"]


def test_build_normalizes_weights_that_already_sum_to_one():
    template = FOATemplateBuilderEngine().build(_parsed())
    total = sum(s.evaluation_weight for s in template.ordered_sections)
    assert abs(total - 1.0) < 1e-6


def test_build_normalizes_weights_that_do_not_sum_to_one():
    parsed = _parsed(weights={"project_summary": 0.3, "technical_merit": 0.3})
    template = FOATemplateBuilderEngine().build(parsed)
    total = sum(s.evaluation_weight for s in template.ordered_sections)
    assert abs(total - 1.0) < 1e-6


def test_build_clamps_out_of_range_raw_weights_instead_of_crashing():
    """
    Regression test: FOASection.evaluation_weight is constrained to [0, 1],
    but raw weights come from FOA parsing (often GPT-4o's JSON output) and
    aren't guaranteed to already be well-formed fractions. A raw weight of
    2.0 previously reached FOASection's constructor unclamped and raised a
    pydantic ValidationError, aborting proposal creation before
    _normalize_weights() ever ran. It must not crash, and the two equal
    (post-clamp) raw weights should still normalize to equal 0.5/0.5 shares.
    """
    parsed = _parsed(weights={"project_summary": 2.0, "technical_merit": 2.0})
    template = FOATemplateBuilderEngine().build(parsed)

    weights = {s.section_id: s.evaluation_weight for s in template.ordered_sections}
    assert weights["project_summary"] == pytest.approx(0.5)
    assert weights["technical_merit"] == pytest.approx(0.5)


def test_build_merges_agency_specific_compliance_rules():
    template = FOATemplateBuilderEngine().build(_parsed())
    # "Font size minimum 11pt" comes from the FOA itself; NSF-specific rules
    # (e.g. margins) come from FOATemplateBuilderEngine.AGENCY_COMPLIANCE.
    assert "Font size minimum 11pt" in template.compliance_rules
    assert any("margins" in r.lower() for r in template.compliance_rules)


def test_build_enriches_guidance_with_agency_note_when_available():
    template = FOATemplateBuilderEngine().build(_parsed())
    tech_section = next(s for s in template.ordered_sections if s.section_id == "technical_merit")
    assert "Agency Note:" in tech_section.guidance
