"""
Engine 3 — Workflow Engine
Maps agency-specific SBIR logic and provides default section scaffolds.
"""

from __future__ import annotations

from typing import Any, Dict, List

from models.schemas import Agency, Phase


class WorkflowEngine:
    """
    Maps agency-specific SBIR/STTR logic to proposal workflows.
    Returns default section scaffolds when no FOA is available.
    """

    # ── Agency workflow rules ─────────────────────────────────────────────────

    AGENCY_WORKFLOWS: Dict[str, Dict] = {
        f"{Agency.NSF}_{Phase.PHASE_I}": {
            "label": "NSF SBIR Phase I",
            "max_budget": 275000,
            "duration_months": 12,
            "requires_concept_paper": False,
            "fast_track_available": False,
            "key_criteria": ["intellectual_merit", "broader_impacts", "commercialization"],
        },
        f"{Agency.NSF}_{Phase.PHASE_II}": {
            "label": "NSF SBIR Phase II",
            "max_budget": 1000000,
            "duration_months": 24,
            "requires_concept_paper": False,
            "requires_phase_i_completion": True,
            "key_criteria": ["intellectual_merit", "broader_impacts", "commercialization"],
        },
        f"{Agency.DOE}_{Phase.PHASE_I}": {
            "label": "DOE SBIR Phase I",
            "max_budget": 200000,
            "duration_months": 9,
            "requires_concept_paper": True,
            "concept_paper_pages": 4,
            "key_criteria": ["technical_merit", "commercialization", "qualifications"],
        },
        f"{Agency.NIH}_{Phase.PHASE_I}": {
            "label": "NIH SBIR Phase I",
            "max_budget": 314506,
            "duration_months": 6,
            "requires_concept_paper": False,
            "fast_track_available": True,
            "key_criteria": ["significance", "investigator", "innovation", "approach", "environment"],
        },
        f"{Agency.NIH}_{Phase.FAST_TRACK}": {
            "label": "NIH SBIR Fast-Track",
            "max_budget": 1600000,
            "duration_months": 24,
            "requires_concept_paper": False,
            "key_criteria": ["significance", "investigator", "innovation", "approach", "environment",
                             "commercialization"],
        },
        f"{Agency.DOD}_{Phase.PHASE_I}": {
            "label": "DoD SBIR Phase I",
            "max_budget": 250000,
            "duration_months": 6,
            "requires_concept_paper": False,
            "topic_required": True,
            "key_criteria": ["technical_merit", "feasibility", "team", "commercialization"],
        },
        f"{Agency.DARPA}_{Phase.PHASE_I}": {
            "label": "DARPA SBIR Phase I",
            "max_budget": 250000,
            "duration_months": 6,
            "requires_concept_paper": False,
            "key_criteria": ["revolutionary_potential", "technical_risk", "team", "timeline"],
        },
    }

    def get_workflow(self, agency: Agency, phase: Phase) -> Dict[str, Any]:
        key = f"{agency}_{phase}"
        return self.AGENCY_WORKFLOWS.get(key, {
            "label": f"{agency} {phase}",
            "max_budget": 300000,
            "duration_months": 12,
            "key_criteria": ["technical_merit", "innovation", "commercialization", "team"],
        })

    # ── Default section scaffolds ─────────────────────────────────────────────

    DEFAULT_SECTIONS: Dict[str, List[Dict]] = {
        Agency.NSF: [
            {"section_id": "project_summary",   "title": "Project Summary",          "required": True,  "page_limit": 1,    "evaluation_weight": 0.05},
            {"section_id": "project_narrative",  "title": "Project Narrative",        "required": True,  "page_limit": 15,   "evaluation_weight": 0.35},
            {"section_id": "intellectual_merit", "title": "Intellectual Merit",       "required": True,  "page_limit": None, "evaluation_weight": 0.20},
            {"section_id": "broader_impacts",    "title": "Broader Impacts",          "required": True,  "page_limit": None, "evaluation_weight": 0.15},
            {"section_id": "commercialization",  "title": "Commercialization Plan",   "required": True,  "page_limit": 5,    "evaluation_weight": 0.15},
            {"section_id": "team",               "title": "Team Qualifications",      "required": True,  "page_limit": 2,    "evaluation_weight": 0.10},
        ],
        Agency.NIH: [
            {"section_id": "specific_aims",      "title": "Specific Aims",            "required": True,  "page_limit": 1,    "evaluation_weight": 0.10},
            {"section_id": "significance",       "title": "Significance",             "required": True,  "page_limit": None, "evaluation_weight": 0.20},
            {"section_id": "innovation",         "title": "Innovation",               "required": True,  "page_limit": None, "evaluation_weight": 0.15},
            {"section_id": "approach",           "title": "Approach",                 "required": True,  "page_limit": 6,    "evaluation_weight": 0.25},
            {"section_id": "environment",        "title": "Environment & Resources",  "required": True,  "page_limit": None, "evaluation_weight": 0.10},
            {"section_id": "commercialization",  "title": "Commercialization Plan",   "required": True,  "page_limit": 12,   "evaluation_weight": 0.15},
            {"section_id": "budget",             "title": "Budget & Justification",   "required": True,  "page_limit": None, "evaluation_weight": 0.05},
        ],
        Agency.DOE: [
            {"section_id": "concept_paper",      "title": "Concept Paper",            "required": True,  "page_limit": 4,    "evaluation_weight": 0.15},
            {"section_id": "technical_merit",    "title": "Technical Merit",          "required": True,  "page_limit": 8,    "evaluation_weight": 0.30},
            {"section_id": "innovation",         "title": "Innovation & Originality", "required": True,  "page_limit": None, "evaluation_weight": 0.15},
            {"section_id": "commercialization",  "title": "Commercialization Plan",   "required": True,  "page_limit": 6,    "evaluation_weight": 0.20},
            {"section_id": "team",               "title": "Key Personnel",            "required": True,  "page_limit": 3,    "evaluation_weight": 0.10},
            {"section_id": "facilities",         "title": "Facilities & Resources",   "required": True,  "page_limit": 2,    "evaluation_weight": 0.05},
            {"section_id": "budget",             "title": "Budget",                   "required": True,  "page_limit": None, "evaluation_weight": 0.05},
        ],
        Agency.DOD: [
            {"section_id": "technical_abstract", "title": "Technical Abstract",       "required": True,  "page_limit": 1,    "evaluation_weight": 0.05},
            {"section_id": "technical_merit",    "title": "Technical Approach",       "required": True,  "page_limit": 12,   "evaluation_weight": 0.35},
            {"section_id": "feasibility",        "title": "Technical Feasibility",    "required": True,  "page_limit": None, "evaluation_weight": 0.20},
            {"section_id": "commercialization",  "title": "Commercialization Plan",   "required": True,  "page_limit": 5,    "evaluation_weight": 0.20},
            {"section_id": "team",               "title": "Key Personnel",            "required": True,  "page_limit": 2,    "evaluation_weight": 0.10},
            {"section_id": "facilities",         "title": "Company Facilities",       "required": True,  "page_limit": 1,    "evaluation_weight": 0.05},
            {"section_id": "subcontracting",     "title": "Subcontracting",           "required": False, "page_limit": 1,    "evaluation_weight": 0.05},
        ],
    }

    # ── Pre-Phase I / Project Pitch scaffolds (3-page briefs, no budget) ────────

    PRE_PHASE_SECTIONS: Dict[str, List[Dict]] = {
        Agency.NSF: [
            # NSF SBIR Project Pitch — 3 pages total per NSF solicitation guidelines
            {"section_id": "innovation_description", "title": "Innovation Description",
             "required": True,  "page_limit": 1,
             "guidance": "Describe the core technology innovation. What is novel? What problem does it solve? State the scientific/technical merit and any preliminary evidence.",
             "evaluation_weight": 0.30},
            {"section_id": "commercial_potential",   "title": "Commercial Potential & Market Opportunity",
             "required": True,  "page_limit": 1,
             "guidance": "Identify the target market, customer segments, estimated market size, and go-to-market strategy. Include evidence of customer discovery.",
             "evaluation_weight": 0.35},
            {"section_id": "rd_objectives",          "title": "R&D Objectives & Technical Milestones",
             "required": True,  "page_limit": 0,  # part of the 3-page pitch
             "guidance": "List 3–5 specific R&D objectives for the Phase I effort. Identify key technical risks and mitigation approaches.",
             "evaluation_weight": 0.20},
            {"section_id": "team_overview",          "title": "Team & Company Overview",
             "required": True,  "page_limit": 0,
             "guidance": "Briefly describe the founding team's relevant expertise, company background, and any facilities or partnerships that enable this work.",
             "evaluation_weight": 0.15},
        ],
        Agency.NIH: [
            # NIH SBIR/STTR Pre-Application (Letter of Intent / Project Pitch)
            {"section_id": "specific_aims_brief",    "title": "Specific Aims (Brief)",
             "required": True,  "page_limit": 1,
             "guidance": "Concisely state the problem, long-term goal, overall objective, and 2–3 specific aims.",
             "evaluation_weight": 0.25},
            {"section_id": "significance_innovation","title": "Significance & Innovation",
             "required": True,  "page_limit": 1,
             "guidance": "Explain clinical/scientific significance and what is innovative about the approach.",
             "evaluation_weight": 0.30},
            {"section_id": "approach_summary",       "title": "Approach Summary",
             "required": True,  "page_limit": 1,
             "guidance": "Summarize the research strategy, key experiments, and anticipated challenges.",
             "evaluation_weight": 0.25},
            {"section_id": "commercialization_brief","title": "Commercialization Potential",
             "required": True,  "page_limit": 0,
             "guidance": "Describe the target patient population, unmet medical need, regulatory pathway, and commercial opportunity.",
             "evaluation_weight": 0.20},
        ],
        Agency.DOE: [
            # DOE SBIR Concept Paper — 4 pages
            {"section_id": "technical_concept",      "title": "Technical Concept & Innovation",
             "required": True,  "page_limit": 2,
             "guidance": "Describe the scientific/technical concept, the innovation over current state of the art, and alignment with DOE mission areas.",
             "evaluation_weight": 0.40},
            {"section_id": "technical_objectives_brief", "title": "Phase I Technical Objectives",
             "required": True,  "page_limit": 1,
             "guidance": "List specific Phase I R&D objectives and key go/no-go milestones.",
             "evaluation_weight": 0.25},
            {"section_id": "commercialization_brief","title": "Commercialization Potential",
             "required": True,  "page_limit": 1,
             "guidance": "Identify target markets, potential customers, and path to commercialization.",
             "evaluation_weight": 0.25},
            {"section_id": "team_brief",             "title": "Team Overview",
             "required": True,  "page_limit": 0,
             "guidance": "Briefly describe the principal investigator and key team members' relevant qualifications.",
             "evaluation_weight": 0.10},
        ],
        Agency.DOD: [
            # DoD SBIR Topic-Specific White Paper / Concept Paper
            {"section_id": "technical_approach_brief","title": "Technical Approach",
             "required": True,  "page_limit": 2,
             "guidance": "Describe the proposed technical approach, innovation relative to the topic area, and anticipated Phase I results.",
             "evaluation_weight": 0.40},
            {"section_id": "relevance",              "title": "Relevance to DoD Topic",
             "required": True,  "page_limit": 1,
             "guidance": "Explicitly address how the proposed work satisfies the topic requirements. Reference specific topic text.",
             "evaluation_weight": 0.30},
            {"section_id": "commercialization_brief","title": "Commercialization Potential",
             "required": True,  "page_limit": 1,
             "guidance": "Describe the dual-use commercial potential and transition path to a Program of Record.",
             "evaluation_weight": 0.20},
            {"section_id": "team_brief",             "title": "Team & Facilities",
             "required": True,  "page_limit": 0,
             "guidance": "Identify PI and key personnel; note any DoD-relevant security clearances or facility certifications.",
             "evaluation_weight": 0.10},
        ],
    }

    def get_default_sections(self, agency: Agency, phase: Phase) -> List[Dict]:
        """Return default sections for an agency+phase when no FOA is provided."""
        # Pre-Phase I → Project Pitch / Concept Paper format (short briefs, no budget)
        if phase == Phase.PRE_PHASE_I:
            return self.PRE_PHASE_SECTIONS.get(
                agency,
                self.PRE_PHASE_SECTIONS[Agency.DOD]
            )

        sections = self.DEFAULT_SECTIONS.get(agency, self.DEFAULT_SECTIONS[Agency.DOD])

        # Phase II / Fast-Track → double page limits
        if phase in (Phase.PHASE_II, Phase.FAST_TRACK):
            sections = [{**s, "page_limit": (s["page_limit"] or 0) * 2 or None} for s in sections]

        return sections

    def requires_concept_paper(self, agency: Agency, phase: Phase) -> bool:
        wf = self.get_workflow(agency, phase)
        return wf.get("requires_concept_paper", False)
