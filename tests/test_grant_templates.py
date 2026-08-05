"""Grant Template Library — pure data lookups, no OpenAI calls."""
from __future__ import annotations

from engines import grant_templates as gt


def test_get_grant_type_known_key():
    sbir = gt.get_grant_type("sbir")
    assert sbir["label"]


def test_get_grant_type_unknown_key_falls_back_to_federal_other():
    unknown = gt.get_grant_type("not_a_real_grant_type")
    assert unknown == gt.get_grant_type("federal_other")


def test_get_budget_type_unknown_grant_type_falls_back_through_federal_other():
    # get_budget_type("unknown") -> get_grant_type("unknown") -> "federal_other"
    # -> federal_other's own budget_type ("federal_simple"), NOT the literal
    # "simple" budget type — those are two different registered budget types.
    assert gt.get_budget_type("not_a_real_grant_type") == gt.get_budget_type("federal_other")
    assert gt.get_grant_type("federal_other")["budget_type"] == "federal_simple"


def test_get_sections_returns_nonempty_list_for_every_registered_grant_type():
    for entry in gt.list_grant_types():
        sections = gt.get_sections(entry["id"])
        assert isinstance(sections, list)
        assert len(sections) > 0, f"{entry['id']} has no sections defined"


def test_get_reviewer_persona_unknown_key_falls_back():
    persona = gt.get_reviewer_persona("not_a_real_persona")
    assert persona == gt.REVIEWER_PERSONAS["federal_program_manager"]


def test_list_grant_types_shape():
    types = gt.list_grant_types()
    assert len(types) > 0
    for entry in types:
        assert set(entry.keys()) >= {"id", "label", "grantor_class", "typical_size"}


def test_get_generation_context_includes_label_and_sections():
    context = gt.get_generation_context("sbir")
    sbir = gt.get_grant_type("sbir")
    assert sbir["label"] in context
    assert "SECTIONS:" in context
