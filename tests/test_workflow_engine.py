"""Engine 3 — Workflow Engine. Pure data/lookup logic, no OpenAI calls."""
from __future__ import annotations

from engines.workflow_engine import WorkflowEngine
from models.schemas import Agency, Phase


def test_known_agency_phase_returns_specific_workflow():
    engine = WorkflowEngine()
    wf = engine.get_workflow(Agency.NSF, Phase.PHASE_I)

    assert wf["label"] == "NSF SBIR Phase I"
    assert wf["max_budget"] == 275000


def test_unknown_agency_phase_falls_back_to_generic_default():
    engine = WorkflowEngine()
    wf = engine.get_workflow(Agency.HUD, Phase.PHASE_I)  # HUD has no explicit workflow entry

    assert wf["max_budget"] == 300000
    assert "key_criteria" in wf


def test_requires_concept_paper_true_for_doe_phase_i():
    engine = WorkflowEngine()
    assert engine.requires_concept_paper(Agency.DOE, Phase.PHASE_I) is True


def test_requires_concept_paper_false_when_unspecified():
    engine = WorkflowEngine()
    assert engine.requires_concept_paper(Agency.NSF, Phase.PHASE_I) is False


def test_default_sections_pre_phase_i_uses_pitch_scaffold():
    engine = WorkflowEngine()
    sections = engine.get_default_sections(Agency.NSF, Phase.PRE_PHASE_I)

    ids = {s["section_id"] for s in sections}
    assert "commercial_potential" in ids
    assert "project_narrative" not in ids  # that's the full Phase I scaffold, not the pitch


def test_default_sections_phase_ii_doubles_page_limits():
    engine = WorkflowEngine()
    phase_i = engine.get_default_sections(Agency.NSF, Phase.PHASE_I)
    phase_ii = engine.get_default_sections(Agency.NSF, Phase.PHASE_II)

    by_id_i = {s["section_id"]: s["page_limit"] for s in phase_i}
    by_id_ii = {s["section_id"]: s["page_limit"] for s in phase_ii}

    for section_id, limit in by_id_i.items():
        if limit is not None:
            assert by_id_ii[section_id] == limit * 2


def test_default_sections_unknown_agency_falls_back_to_dod():
    engine = WorkflowEngine()
    fallback = engine.get_default_sections(Agency.HUD, Phase.PHASE_I)
    dod = engine.get_default_sections(Agency.DOD, Phase.PHASE_I)

    assert [s["section_id"] for s in fallback] == [s["section_id"] for s in dod]
