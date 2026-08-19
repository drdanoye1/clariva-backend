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


def _structured_section(section_id: str, title: str, blocks: list, words: int = 100) -> SectionContent:
    """Same as _section() but with structured_content populated — SectionContent
    accepts arbitrary dicts there (models.schemas doesn't validate it eagerly;
    ComplianceEngine._structured_content_checks() is what validates it)."""
    return SectionContent(
        section_id=section_id,
        title=title,
        content=("word " * words).strip(),
        word_count=words,
        page_estimate=words / WORDS_PER_PAGE,
        structured_content={"schemaVersion": "1.0", "blocks": blocks},
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


# ── Phase 5 — Export QA gate (CLARIVA-DOCGEN-SPEC-001) ──────────────────────
# These exercise _structured_content_checks() through the same public
# validate() entry point, using real block-JSON dicts shaped exactly like
# proposal_generator.py's generate_section() (Phase 4) produces.

def test_invalid_structured_content_schema_is_a_blocking_error():
    engine = ComplianceEngine()
    # Missing the required "text" alternative and "type" discriminator that
    # every block needs — StructuredSectionContent.model_validate() rejects this.
    sections = [_structured_section("technical_approach", "Technical Approach",
                                     blocks=[{"not_a_real_block": True}])]
    report = engine.validate(_proposal(Agency.OTHER), sections)

    assert report.passed is False
    assert any(v.rule == "Invalid section data" and v.severity == "error" for v in report.violations)


def test_blocking_missing_placeholder_is_an_error_warning_missing_is_a_warning():
    engine = ComplianceEngine()
    sections = [_structured_section("team", "Team Qualifications", blocks=[
        {"type": "paragraph", "text": "We propose to hire additional staff.",
         "missing": {"field": "staff_count", "label": "Number of new hires", "severity": "blocking"}},
        {"type": "paragraph", "text": "Nice to have but optional.",
         "missing": {"field": "extra", "label": "Optional detail", "severity": "warning"}},
    ])]
    report = engine.validate(_proposal(Agency.OTHER), sections)

    placeholder_viols = [v for v in report.violations if v.rule == "Missing placeholder"]
    assert {v.severity for v in placeholder_viols} == {"error", "warning"}
    assert report.passed is False  # the blocking one alone should fail the gate


def test_missing_callout_is_always_a_blocking_error():
    engine = ComplianceEngine()
    sections = [_structured_section("facilities", "Facilities", blocks=[
        {"type": "callout", "kind": "missing", "title": "Facilities gap", "text": "No lab data on file."},
        {"type": "callout", "kind": "note", "text": "Just an FYI, not a gap."},
    ])]
    report = engine.validate(_proposal(Agency.OTHER), sections)

    placeholder_viols = [v for v in report.violations if v.rule == "Missing placeholder"]
    assert len(placeholder_viols) == 1
    assert placeholder_viols[0].severity == "error"


def test_malformed_table_row_length_mismatch_is_a_blocking_error():
    engine = ComplianceEngine()
    sections = [_structured_section("budget_justification", "Budget Justification", blocks=[
        {"type": "table", "caption": "Personnel Costs", "columns": ["Item", "Cost", "Notes"],
         "rows": [["Labor", "$50,000"], ["Travel", "$5,000", "Conference", "extra cell"]]},
    ])]
    report = engine.validate(_proposal(Agency.OTHER), sections)

    malformed = [v for v in report.violations if v.rule == "Malformed table"]
    assert len(malformed) == 2  # one row too short, one row too long
    assert all(v.severity == "error" for v in malformed)
    assert report.passed is False


def test_raw_pipe_in_table_cell_is_a_warning_not_an_error():
    engine = ComplianceEngine()
    sections = [_structured_section("budget_justification", "Budget Justification", blocks=[
        {"type": "table", "columns": ["A", "B"], "rows": [["x | y", "z"]]},
    ])]
    report = engine.validate(_proposal(Agency.OTHER), sections)

    leak = [v for v in report.violations if v.rule == "Possible unparsed table"]
    assert len(leak) == 1
    assert leak[0].severity == "warning"


def test_malformed_reference_urls_flagged_as_warnings():
    engine = ComplianceEngine()
    sections = [_structured_section("references", "References", blocks=[
        {"type": "references", "entries": [
            {"text": "Valid", "url": "https://www.grants.gov/some-page"},
            {"text": "No scheme", "url": "www.grants.gov/page"},
            {"text": "Embedded space", "url": "https://www.grants.gov/some page"},
        ]},
    ])]
    report = engine.validate(_proposal(Agency.OTHER), sections)

    bad_urls = [v for v in report.violations if v.rule == "Malformed reference URL"]
    assert len(bad_urls) == 2
    assert all(v.severity == "warning" for v in bad_urls)


def test_markdown_leak_in_structured_paragraph_is_a_warning():
    engine = ComplianceEngine()
    sections = [_structured_section("approach", "Approach", blocks=[
        {"type": "paragraph", "text": "This is **bold** text that should not be here."},
    ])]
    report = engine.validate(_proposal(Agency.OTHER), sections)

    assert any(v.rule == "Markdown formatting leaked into content" and v.severity == "warning"
               for v in report.violations)


def test_clean_structured_section_produces_no_qa_violations():
    engine = ComplianceEngine()
    sections = [_structured_section("clean", "Clean Section", blocks=[
        {"type": "paragraph", "text": "Clean prose with no issues at all."},
        {"type": "runIn", "label": "Personnel:", "text": "Dr. Smith leads the effort."},
        {"type": "bulletList", "items": ["Item one", "Item two"]},
        {"type": "table", "columns": ["A", "B"], "rows": [["1", "2"], ["3", "4"]]},
        {"type": "schedule", "phases": [{"name": "Design", "start_month": 1, "end_month": 3}]},
        {"type": "figure", "caption": "Workflow", "altText": "Step1 to Step2"},
        {"type": "callout", "kind": "note", "text": "Just a note."},
        {"type": "references", "entries": [{"text": "Clean ref", "url": "https://example.gov/doc"}]},
    ])]
    report = engine.validate(_proposal(Agency.OTHER), sections)

    qa_rules = {"Invalid section data", "Missing placeholder", "Malformed table",
                "Possible unparsed table", "Malformed reference URL",
                "Markdown formatting leaked into content"}
    assert [v for v in report.violations if v.rule in qa_rules] == []


def test_legacy_section_without_structured_content_still_flags_missing_and_markdown():
    engine = ComplianceEngine()
    sections = [_section("legacy", "Legacy Section", words=20,
                          content="This has a [MISSING: budget figure] gap and uses **bold** markdown.")]
    report = engine.validate(_proposal(Agency.OTHER), sections)

    assert any(v.rule == "Missing placeholder" and v.severity == "error" for v in report.violations)
    assert any(v.rule == "Markdown formatting leaked into content" for v in report.violations)
