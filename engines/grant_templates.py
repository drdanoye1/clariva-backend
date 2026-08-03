"""
Grant Template Library — defines section structures, compliance rules,
budget types, and reviewer personas for every supported grant type.
"""

from __future__ import annotations
from typing import Dict, List, Any

# ── Grant type registry ────────────────────────────────────────────────────────

GRANT_TYPES: Dict[str, Dict[str, Any]] = {

    # ── SBIR / STTR (existing) ─────────────────────────────────────────────────
    "sbir": {
        "label": "SBIR – Small Business Innovation Research",
        "grantor_class": "federal",
        "budget_type": "sbir",
        "has_fee": True,
        "typical_size": "$150K–$2M",
        "sections": [
            {"id": "cover",            "title": "Cover Page & Project Summary",       "page_limit": 1,  "required": True,  "weight": 0.05},
            {"id": "specific_aims",    "title": "Specific Aims",                      "page_limit": 1,  "required": True,  "weight": 0.10},
            {"id": "tech_merit",       "title": "Technical Merit & Innovation",       "page_limit": 6,  "required": True,  "weight": 0.25},
            {"id": "schedule",         "title": "Work Plan, Schedule & Milestones",  "page_limit": 2,  "required": True,  "weight": 0.06},
            {"id": "commercialization","title": "Commercialization Potential",        "page_limit": 4,  "required": True,  "weight": 0.18},
            {"id": "phase2_plan",      "title": "Phase II Transition Plan",           "page_limit": 2,  "required": False, "weight": 0.08},
            {"id": "team",             "title": "PI & Team Qualifications",           "page_limit": 3,  "required": True,  "weight": 0.13},
            {"id": "facilities",       "title": "Facilities & Resources",             "page_limit": 2,  "required": True,  "weight": 0.08},
            {"id": "budget_narrative", "title": "Budget Justification",               "page_limit": 3,  "required": True,  "weight": 0.05},
            {"id": "references",       "title": "References",                         "page_limit": None,"required": True, "weight": 0.02},
        ],
        "compliance_rules": [
            "Company must be U.S.-based small business (<500 employees)",
            "PI must be primarily employed by awardee (>50% effort for SBIR)",
            "UEI number required (SAM.gov registration)",
            "CAGE code required",
            "Phase I typically 6-12 months; Phase II 24 months",
        ],
        "reviewer_personas": ["sbir_panel", "program_officer"],
        "scoring_criteria": {
            "technical_merit": 0.30,
            "commercialization": 0.25,
            "innovation": 0.20,
            "team": 0.15,
            "facilities": 0.10,
        },
    },

    "sttr": {
        "label": "STTR – Small Business Technology Transfer",
        "grantor_class": "federal",
        "budget_type": "sbir",
        "has_fee": True,
        "typical_size": "$150K–$2M",
        "sections": [
            {"id": "cover",            "title": "Cover Page & Project Summary",       "page_limit": 1,  "required": True,  "weight": 0.05},
            {"id": "specific_aims",    "title": "Specific Aims",                      "page_limit": 1,  "required": True,  "weight": 0.10},
            {"id": "tech_merit",       "title": "Technical Merit & Innovation",       "page_limit": 6,  "required": True,  "weight": 0.25},
            {"id": "schedule",         "title": "Work Plan, Schedule & Milestones",  "page_limit": 2,  "required": True,  "weight": 0.06},
            {"id": "commercialization","title": "Commercialization Potential",        "page_limit": 4,  "required": True,  "weight": 0.18},
            {"id": "cooperative_rd",   "title": "Cooperative R&D Plan",               "page_limit": 2,  "required": True,  "weight": 0.08},
            {"id": "team",             "title": "PI, Team & Research Institution",    "page_limit": 3,  "required": True,  "weight": 0.13},
            {"id": "facilities",       "title": "Facilities & Resources",             "page_limit": 2,  "required": True,  "weight": 0.08},
            {"id": "budget_narrative", "title": "Budget Justification",               "page_limit": 3,  "required": True,  "weight": 0.05},
            {"id": "references",       "title": "References",                         "page_limit": None,"required": True, "weight": 0.02},
        ],
        "compliance_rules": [
            "Small business must perform ≥40% of work",
            "Research institution must perform ≥30% of work",
            "Formal IP agreement between small business and research institution required",
            "PI may be primarily employed at research institution",
            "UEI and CAGE required",
        ],
        "reviewer_personas": ["sbir_panel", "program_officer"],
        "scoring_criteria": {
            "technical_merit": 0.30,
            "commercialization": 0.20,
            "innovation": 0.20,
            "team": 0.15,
            "cooperative_rd": 0.10,
            "facilities": 0.05,
        },
    },

    # ── NIH R01 ────────────────────────────────────────────────────────────────
    "nih_r01": {
        "label": "NIH R01 – Research Project Grant",
        "grantor_class": "federal",
        "budget_type": "nih_modular",
        "has_fee": False,
        "typical_size": "$250K–$500K/yr (direct costs)",
        "sections": [
            {"id": "specific_aims",    "title": "Specific Aims (1 page)",             "page_limit": 1,  "required": True,  "weight": 0.15},
            {"id": "significance",     "title": "Significance",                        "page_limit": 4,  "required": True,  "weight": 0.20},
            {"id": "innovation",       "title": "Innovation",                          "page_limit": 2,  "required": True,  "weight": 0.15},
            {"id": "approach",         "title": "Approach",                            "page_limit": 10, "required": True,  "weight": 0.25},
            {"id": "environment",      "title": "Environment & Resources",             "page_limit": 1,  "required": True,  "weight": 0.05},
            {"id": "team_biosketch",   "title": "Investigators & Biosketches",         "page_limit": 5,  "required": True,  "weight": 0.10},
            {"id": "human_subjects",   "title": "Human Subjects / Vertebrate Animals","page_limit": 2,  "required": False, "weight": 0.05},
            {"id": "budget_narrative", "title": "Budget Justification",               "page_limit": None,"required": True, "weight": 0.03},
            {"id": "references",       "title": "References",                         "page_limit": None,"required": True, "weight": 0.02},
        ],
        "compliance_rules": [
            "Must use SF424 (R&R) application forms",
            "12-page Research Strategy limit (Significance + Innovation + Approach)",
            "Biosketch required for all senior/key personnel (NIH format, 5 pages max each)",
            "Human subjects and animal use protocols must be addressed",
            "Modular budget: $250K/yr direct costs in $25K modules; detailed for higher amounts",
            "Must have Institutional Assurance (FWA) for human subjects",
        ],
        "reviewer_personas": ["nih_study_section", "nih_scientific_officer"],
        "scoring_criteria": {
            "significance": 0.20,
            "investigators": 0.20,
            "innovation": 0.15,
            "approach": 0.30,
            "environment": 0.15,
        },
    },

    # ── NIH R21 ────────────────────────────────────────────────────────────────
    "nih_r21": {
        "label": "NIH R21 – Exploratory/Developmental Research",
        "grantor_class": "federal",
        "budget_type": "nih_modular",
        "has_fee": False,
        "typical_size": "Up to $275K (2 years)",
        "sections": [
            {"id": "specific_aims",    "title": "Specific Aims (1 page)",             "page_limit": 1,  "required": True,  "weight": 0.15},
            {"id": "significance",     "title": "Significance & Innovation",           "page_limit": 3,  "required": True,  "weight": 0.30},
            {"id": "approach",         "title": "Approach",                            "page_limit": 3,  "required": True,  "weight": 0.30},
            {"id": "environment",      "title": "Environment & Resources",             "page_limit": 1,  "required": True,  "weight": 0.05},
            {"id": "team_biosketch",   "title": "Investigators & Biosketches",         "page_limit": 5,  "required": True,  "weight": 0.10},
            {"id": "human_subjects",   "title": "Human Subjects / Vertebrate Animals","page_limit": 2,  "required": False, "weight": 0.05},
            {"id": "budget_narrative", "title": "Budget Justification",               "page_limit": None,"required": True, "weight": 0.03},
            {"id": "references",       "title": "References",                         "page_limit": None,"required": True, "weight": 0.02},
        ],
        "compliance_rules": [
            "6-page Research Strategy (Significance/Innovation + Approach combined)",
            "Modular budget: max $275K total direct costs over 2 years",
            "Biosketch required (NIH format)",
            "No preliminary data required but strengthens application",
        ],
        "reviewer_personas": ["nih_study_section"],
        "scoring_criteria": {
            "significance": 0.25,
            "investigators": 0.20,
            "innovation": 0.20,
            "approach": 0.25,
            "environment": 0.10,
        },
    },

    # ── NSF Standard Research ──────────────────────────────────────────────────
    "nsf_standard": {
        "label": "NSF Standard Research Grant",
        "grantor_class": "federal",
        "budget_type": "federal_simple",
        "has_fee": False,
        "typical_size": "$100K–$500K/yr",
        "sections": [
            {"id": "project_summary",  "title": "Project Summary (1 page)",           "page_limit": 1,  "required": True,  "weight": 0.05},
            {"id": "project_description","title": "Project Description",              "page_limit": 15, "required": True,  "weight": 0.50},
            {"id": "broader_impacts",  "title": "Broader Impacts",                    "page_limit": 3,  "required": True,  "weight": 0.20},
            {"id": "intellectual_merit","title": "Intellectual Merit",                "page_limit": 3,  "required": True,  "weight": 0.20},
            {"id": "facilities",       "title": "Facilities & Resources",             "page_limit": 1,  "required": True,  "weight": 0.03},
            {"id": "data_management",  "title": "Data Management Plan",               "page_limit": 2,  "required": True,  "weight": 0.02},
            {"id": "budget_narrative", "title": "Budget Justification",               "page_limit": None,"required": True, "weight": 0.02},
            {"id": "references",       "title": "References",                         "page_limit": None,"required": True, "weight": 0.02},
            {"id": "biosketch",        "title": "Biographical Sketches (CVs)",        "page_limit": 3,  "required": True,  "weight": 0.03},
        ],
        "compliance_rules": [
            "Must use NSF FastLane or Research.gov submission system",
            "Project Description limited to 15 pages",
            "Broader Impacts must be addressed both in Project Summary and Project Description",
            "Data Management Plan required (2 pages max)",
            "Biosketch max 3 pages per person (NSF format)",
            "F&A (indirect) costs at negotiated rate",
        ],
        "reviewer_personas": ["nsf_panel_reviewer", "nsf_program_officer"],
        "scoring_criteria": {
            "intellectual_merit": 0.50,
            "broader_impacts": 0.30,
            "feasibility": 0.15,
            "team": 0.05,
        },
    },

    # ── Federal Other (generic BAA, OTA, etc.) ─────────────────────────────────
    "federal_other": {
        "label": "Federal Grant / BAA / OTA (Other)",
        "grantor_class": "federal",
        "budget_type": "federal_simple",
        "has_fee": False,
        "typical_size": "Varies",
        "sections": [
            {"id": "executive_summary","title": "Executive Summary",                  "page_limit": 2,  "required": True,  "weight": 0.10},
            {"id": "technical_approach","title": "Technical Approach",                "page_limit": 10, "required": True,  "weight": 0.35},
            {"id": "statement_of_work","title": "Statement of Work",                  "page_limit": 5,  "required": True,  "weight": 0.20},
            {"id": "team",             "title": "Team & Key Personnel",               "page_limit": 3,  "required": True,  "weight": 0.15},
            {"id": "schedule",         "title": "Schedule & Milestones",              "page_limit": 2,  "required": True,  "weight": 0.10},
            {"id": "facilities",       "title": "Facilities & Resources",             "page_limit": 2,  "required": False, "weight": 0.05},
            {"id": "budget_narrative", "title": "Budget Justification",               "page_limit": None,"required": True, "weight": 0.05},
            {"id": "references",       "title": "References",                         "page_limit": None,"required": False,"weight": 0.01},
        ],
        "compliance_rules": [
            "Must comply with solicitation-specific requirements",
            "UEI (SAM.gov) registration typically required",
            "Cost sharing requirements vary by solicitation",
            "Security clearance may be required for certain DoD solicitations",
        ],
        "reviewer_personas": ["federal_program_manager", "technical_evaluator"],
        "scoring_criteria": {
            "technical_merit": 0.40,
            "team": 0.20,
            "feasibility": 0.20,
            "management": 0.10,
            "cost": 0.10,
        },
    },

    # ── State Grant ────────────────────────────────────────────────────────────
    "state_grant": {
        "label": "State Government Grant",
        "grantor_class": "state",
        "budget_type": "simple",
        "has_fee": False,
        "typical_size": "$10K–$500K",
        "sections": [
            {"id": "cover",            "title": "Cover Page & Organizational Information","page_limit": 2,"required": True, "weight": 0.05},
            {"id": "executive_summary","title": "Executive Summary / Abstract",       "page_limit": 1,  "required": True,  "weight": 0.05},
            {"id": "need_statement",   "title": "Statement of Need / Problem",        "page_limit": 3,  "required": True,  "weight": 0.20},
            {"id": "project_description","title": "Project Description / Scope of Work","page_limit": 8,"required": True,  "weight": 0.30},
            {"id": "goals_objectives", "title": "Goals, Objectives & Outcomes",       "page_limit": 3,  "required": True,  "weight": 0.15},
            {"id": "evaluation",       "title": "Evaluation Plan & Metrics",          "page_limit": 2,  "required": True,  "weight": 0.10},
            {"id": "team",             "title": "Organizational Capacity & Team",     "page_limit": 2,  "required": True,  "weight": 0.05},
            {"id": "sustainability",   "title": "Sustainability Plan",                "page_limit": 1,  "required": False, "weight": 0.05},
            {"id": "budget_narrative", "title": "Budget & Budget Justification",      "page_limit": 3,  "required": True,  "weight": 0.05},
            {"id": "references",       "title": "References / Appendices",            "page_limit": None,"required": False,"weight": 0.00},
        ],
        "compliance_rules": [
            "Applicant must be registered in state vendor/grant system",
            "Must demonstrate nexus to state (operations, jobs, or benefit within state)",
            "Matching funds may be required (varies by program)",
            "Must comply with state procurement and reporting requirements",
            "Annual or semi-annual progress reports typically required",
        ],
        "reviewer_personas": ["state_program_officer", "state_peer_reviewer"],
        "scoring_criteria": {
            "need": 0.25,
            "approach": 0.30,
            "outcomes": 0.20,
            "capacity": 0.15,
            "budget": 0.10,
        },
    },

    # ── Private Foundation ─────────────────────────────────────────────────────
    "foundation": {
        "label": "Private Foundation / Non-profit Grant",
        "grantor_class": "foundation",
        "budget_type": "simple",
        "has_fee": False,
        "typical_size": "$5K–$500K",
        "sections": [
            {"id": "cover",            "title": "Cover / Letter of Inquiry Summary",  "page_limit": 1,  "required": True,  "weight": 0.05},
            {"id": "executive_summary","title": "Executive Summary",                  "page_limit": 1,  "required": True,  "weight": 0.10},
            {"id": "need_statement",   "title": "Statement of Need",                  "page_limit": 2,  "required": True,  "weight": 0.20},
            {"id": "goals_objectives", "title": "Goals, Objectives & Activities",     "page_limit": 3,  "required": True,  "weight": 0.20},
            {"id": "project_description","title": "Project Description / Methods",   "page_limit": 5,  "required": True,  "weight": 0.20},
            {"id": "evaluation",       "title": "Evaluation & Measurement Plan",      "page_limit": 2,  "required": True,  "weight": 0.10},
            {"id": "sustainability",   "title": "Sustainability & Future Funding",    "page_limit": 1,  "required": True,  "weight": 0.05},
            {"id": "org_capacity",     "title": "Organizational Background & Capacity","page_limit": 2, "required": True,  "weight": 0.05},
            {"id": "budget_narrative", "title": "Budget & Budget Narrative",          "page_limit": 2,  "required": True,  "weight": 0.05},
        ],
        "compliance_rules": [
            "Organization must be 501(c)(3) or fiscal sponsor required",
            "LOI (Letter of Inquiry) may be required before full proposal",
            "Foundation may have geographic or mission alignment requirements",
            "Indirect costs often capped (10–15%) or not allowed",
            "Annual narrative and financial reports required if funded",
        ],
        "reviewer_personas": ["foundation_program_officer", "foundation_board"],
        "scoring_criteria": {
            "mission_alignment": 0.25,
            "need": 0.20,
            "approach": 0.25,
            "evaluation": 0.15,
            "capacity": 0.10,
            "sustainability": 0.05,
        },
    },

    # ── Corporate / Industry ───────────────────────────────────────────────────
    "corporate": {
        "label": "Corporate / Industry-Sponsored Research",
        "grantor_class": "corporate",
        "budget_type": "simple",
        "has_fee": False,
        "typical_size": "$25K–$2M",
        "sections": [
            {"id": "executive_summary","title": "Executive Summary",                  "page_limit": 1,  "required": True,  "weight": 0.10},
            {"id": "technical_approach","title": "Technical Approach & Methodology",  "page_limit": 8,  "required": True,  "weight": 0.35},
            {"id": "deliverables",     "title": "Deliverables & IP / Licensing Terms","page_limit": 2,  "required": True,  "weight": 0.15},
            {"id": "team",             "title": "Team & Expertise",                   "page_limit": 2,  "required": True,  "weight": 0.15},
            {"id": "schedule",         "title": "Timeline & Milestones",              "page_limit": 2,  "required": True,  "weight": 0.10},
            {"id": "budget_narrative", "title": "Budget & Cost Breakdown",            "page_limit": 2,  "required": True,  "weight": 0.15},
        ],
        "compliance_rules": [
            "IP ownership and licensing terms must be negotiated upfront",
            "Publication rights may be restricted",
            "Conflict of interest disclosure may be required",
        ],
        "reviewer_personas": ["corporate_technical_reviewer", "business_development"],
        "scoring_criteria": {
            "technical_merit": 0.35,
            "deliverables": 0.20,
            "team": 0.20,
            "timeline": 0.15,
            "cost": 0.10,
        },
    },
}


# ── Budget type configs ────────────────────────────────────────────────────────

BUDGET_TYPES: Dict[str, Dict[str, Any]] = {
    "sbir": {
        "label": "SBIR/STTR Budget",
        "has_fee": True,
        "default_fee_rate": 7.0,
        "indirect_base": "mtdc",
        "notes": "MTDC excludes equipment and subcontract costs >$25K. Fee (profit) allowed.",
    },
    "nih_modular": {
        "label": "NIH Modular Budget",
        "has_fee": False,
        "default_fee_rate": 0.0,
        "indirect_base": "mtdc",
        "notes": "Request in $25K modules up to $250K/yr direct costs. No fee/profit.",
    },
    "federal_simple": {
        "label": "Federal Detailed Budget",
        "has_fee": False,
        "default_fee_rate": 0.0,
        "indirect_base": "mtdc",
        "notes": "Standard federal budget. No fee/profit for most agencies.",
    },
    "simple": {
        "label": "Simple Budget",
        "has_fee": False,
        "default_fee_rate": 0.0,
        "indirect_base": "tdc",
        "notes": "Simple line-item budget. Indirect may be capped or excluded by funder.",
    },
}


# ── Reviewer persona prompts ───────────────────────────────────────────────────

REVIEWER_PERSONAS: Dict[str, Dict[str, str]] = {
    "sbir_panel": {
        "label": "SBIR Peer Reviewer",
        "focus": "technical feasibility, commercial potential, and team qualifications for SBIR",
    },
    "nih_study_section": {
        "label": "NIH Study Section Reviewer",
        "focus": "scientific significance, innovation, approach rigor, investigator qualifications, and research environment using NIH 1-9 scoring scale",
    },
    "nsf_panel_reviewer": {
        "label": "NSF Panel Reviewer",
        "focus": "intellectual merit and broader impacts using NSF review criteria",
    },
    "state_program_officer": {
        "label": "State Program Officer",
        "focus": "alignment with state priorities, community benefit, measurable outcomes, and budget reasonableness",
    },
    "foundation_program_officer": {
        "label": "Foundation Program Officer",
        "focus": "mission alignment, demonstrated need, clear measurable goals, organizational capacity, and sustainability",
    },
    "federal_program_manager": {
        "label": "Federal Program Manager",
        "focus": "technical approach feasibility, team credentials, schedule realism, and cost reasonableness",
    },
    "corporate_technical_reviewer": {
        "label": "Corporate Technical Reviewer",
        "focus": "technical merit, clear deliverables, IP terms, team expertise, and ROI for the sponsor",
    },
}


# ── Public helpers ─────────────────────────────────────────────────────────────

def get_grant_type(grant_type: str) -> Dict[str, Any]:
    return GRANT_TYPES.get(grant_type, GRANT_TYPES["federal_other"])


def get_sections(grant_type: str) -> List[Dict[str, Any]]:
    return get_grant_type(grant_type).get("sections", [])


def get_budget_type(grant_type: str) -> Dict[str, Any]:
    bt = get_grant_type(grant_type).get("budget_type", "simple")
    return BUDGET_TYPES.get(bt, BUDGET_TYPES["simple"])


def get_reviewer_persona(persona_key: str) -> Dict[str, str]:
    return REVIEWER_PERSONAS.get(persona_key, REVIEWER_PERSONAS["federal_program_manager"])


def list_grant_types() -> List[Dict[str, str]]:
    return [
        {"id": k, "label": v["label"], "grantor_class": v["grantor_class"],
         "typical_size": v.get("typical_size", "Varies")}
        for k, v in GRANT_TYPES.items()
    ]


def get_generation_context(grant_type: str) -> str:
    """Returns a system-prompt injection explaining the grant type to GPT-4o."""
    gt = get_grant_type(grant_type)
    sections_desc = "; ".join(f"{s['title']} ({s['page_limit'] or 'no'} pg limit)" for s in gt["sections"])
    criteria = "; ".join(f"{k} ({int(v*100)}%)" for k, v in gt.get("scoring_criteria", {}).items())
    return f"""GRANT TYPE: {gt['label']}
GRANTOR CLASS: {gt['grantor_class']}
TYPICAL AWARD SIZE: {gt.get('typical_size', 'varies')}
SECTIONS: {sections_desc}
REVIEW CRITERIA: {criteria}
COMPLIANCE REQUIREMENTS: {'; '.join(gt.get('compliance_rules', [])[:3])}"""
