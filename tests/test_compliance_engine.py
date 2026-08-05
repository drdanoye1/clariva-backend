"""
Engine 5 — Formatting & Compliance Engine.

Fully deterministic (no OpenAI calls), so it's tested end-to-end through its
public `validate()` method rather than just its private helpers.
"""
from __future__ import annotations

from types import SimpleNamespace

from engines.compliance_engine import ComplianceEngine
from models.schemas import Agency, SectionContent

WORDS_PER_PAGE = 250


def _proposal(agency: str = Agency.NSF, proposal_id: str = "p1") -> SimpleNamespace:
    return SimpleNamespace(agency=agency, id=proposal_id)


def _section(section_id: str, title: str, words: int, content: str | None = None) -> SectionContent:
    return SectionContent(
        section_id=section_id,
        title=title,
        content=content if content is not None else ("word " * words).strip(),
        word_count=words,
        page_estimate=words / WORDS_PER_PAGE,
    )


def test_passes_when_within_all_limits():
    engine = ComplianceEngine()
    sections = [
        _section("project_summary", "Project Summary", words=200),  # ~0.8 pages, limit 1
        _section("team", "Team Qualifications", words=400,
                  content="Our team has 10 years of experience and clear milestones and a timeline."),
    ]
    report = engine.validate(_proposal(Agency.NSF), sections)

    assert report.passed is True
    assert report.proposal_id == "p1"
    assert report.total_pages > 0


def test_flags_section_over_its_page_limit():
    engine = ComplianceEngine()
    # NSF project_summary limit is 1 page = 250 words; give it 1000 words (~4 pages).
    sections = [_section("project_summary", "Project Summary", words=1000)]
    report = engine.validate(_proposal(Agency.NSF), sections)

    assert report.passed is False
    errors = [v for v in report.violations if v.severity == "error"]
    assert any(v.section_id == "project_summary" for v in errors)


def test_flags_empty_section_as_warning_not_error():
    engine = ComplianceEngine()
    sections = [_section("team", "Team Qualifications", words=0, content="")]
    report = engine.validate(_proposal(Agency.NSF), sections)

    warnings = [v for v in report.violations if v.severity == "warning"]
    assert any(v.rule == "Empty section" for v in warnings)
    # An empty section alone (no error-level violations) should still pass.
    assert report.passed is True


def test_total_page_limit_enforced_across_all_sections():
    engine = ComplianceEngine()
    # DOD total limit is 20 pages; make three sections that individually
    # fit their own limits but blow the total.
    sections = [
        _section("technical_merit", "Technical Approach", words=12 * WORDS_PER_PAGE,
                  content="Includes milestones and a timeline for each phase."),
        _section("commercialization", "Commercialization Plan", words=5 * WORDS_PER_PAGE,
                  content="Market size is estimated at $50 million."),
        _section("team", "Key Personnel", words=6 * WORDS_PER_PAGE),
    ]
    report = engine.validate(_proposal(Agency.DOD), sections)

    assert report.passed is False
    assert any(v.rule == "Total page limit" for v in report.violations)


def test_commercialization_quality_warning_when_no_market_size():
    engine = ComplianceEngine()
    sections = [_section("commercialization", "Commercialization Plan", words=100,
                          content="We plan to sell our product to customers.")]
    report = engine.validate(_proposal(Agency.NSF), sections)

    assert any(v.rule == "Commercialization quality" for v in report.violations)


def test_custom_rule_flags_section_exceeding_stated_limit():
    engine = ComplianceEngine()
    sections = [_section("budget_narrative", "Budget Narrative", words=3 * WORDS_PER_PAGE)]
    report = engine.validate(
        _proposal(Agency.OTHER),
        sections,
        compliance_rules=["Budget narrative must not exceed 2 pages"],
    )

    assert any("Budget narrative" in v.rule for v in report.violations)
