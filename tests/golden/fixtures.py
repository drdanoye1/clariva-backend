"""
Word/PDF Report Generation Development Specification (CLARIVA-DOCGEN-
SPEC-001), Phase 14 — the visual regression corpus's fixture data.

§13 Acceptance Criteria: "Automated regression tests include
representative NSF, NIH, DOE/DOD and generic/private-foundation
documents." AGENCY_FIXTURES below is exactly that: one deterministic,
hand-authored (never AI-generated — a golden corpus must be reproducible
without an API key or nondeterministic model output) proposal per agency
family, each carrying the identical rich block content (every
DocumentBlock type the renderer supports gets exercised at least once) so
that a change to any block renderer shows up in every fixture's diff, not
just one. Only proposal-level metadata (agency, phase, title) varies
between fixtures — the point of per-agency golden files is to catch a
regression in what a reviewer actually receives for that agency (the
agency name IS part of the rendered metadata line — see
document_output.py::_export_docx()), not to exercise agency-specific
block content (AgencyProfile guidance/page-limits affect the compliance
report and generation prompts, never the DOCX block renderer itself — see
engines/agency_profile_engine.py).

This module is pure data (plain dicts) — deliberately no pydantic import,
so it can be inspected/hand-validated even in an environment without the
full dependency stack installed (see this repo's own sandbox constraints,
documented throughout docs/ARCHITECTURE.md's Phase 9-14 entries). The
dicts are validated for real against StructuredSectionContent the moment
any consumer (scripts/generate_golden_files.py, tests/
test_visual_regression.py) calls utils/block_renderer.py::
render_structured_content() on them.
"""
from __future__ import annotations

from typing import Any, Dict, List


def _shared_blocks() -> List[Dict[str, Any]]:
    """Every DocumentBlock type at least once — see module docstring.
    Returns a fresh list each call so callers can safely mutate their own
    copy without affecting other fixtures."""
    return [
        {
            "type": "paragraph",
            "text": (
                "This project will develop and validate a modular sensor "
                "platform for real-time environmental monitoring, "
                "addressing a well-documented gap in low-cost, "
                "field-deployable instrumentation."
            ),
        },
        {
            "type": "runIn",
            "label": "Personnel",
            "text": (
                "The PI (0.5 FTE) will lead system architecture and "
                "validation; a postdoctoral researcher (1.0 FTE) will lead "
                "firmware development and field testing."
            ),
        },
        {
            "type": "bulletList",
            "items": [
                "Design and fabricate the sensor housing and PCB",
                "Implement the embedded firmware and calibration routine",
                "Conduct field validation across three representative sites",
            ],
        },
        {
            "type": "table",
            "columns": ["Budget Category", "Year 1", "Year 2"],
            "rows": [
                ["Personnel", "$120,000", "$126,000"],
                ["Equipment", "$45,000", "$10,000"],
                ["Travel", "$8,000", "$8,500"],
            ],
            "caption": "Table 1. Budget summary by category.",
        },
        {"type": "pageBreak"},
        {
            "type": "schedule",
            "title": "Project Schedule",
            "phases": [
                {"name": "Design & Fabrication", "start_month": 1, "end_month": 6},
                {"name": "Firmware Development", "start_month": 4, "end_month": 10},
                {"name": "Field Validation", "start_month": 10, "end_month": 18},
                {"name": "Analysis & Reporting", "start_month": 16, "end_month": 24},
            ],
        },
        {
            "type": "callout",
            "kind": "note",
            "title": "Data Management",
            "text": (
                "All field data will be deposited in a public repository "
                "within 12 months of collection, consistent with the "
                "sponsor's open-data policy."
            ),
        },
        {
            "type": "references",
            "entries": [
                {"text": "Smith, J. et al. (2023). Low-cost environmental sensing. J. Env. Monitoring, 12(3), 45-58.",
                 "url": "https://example.org/smith2023"},
                {"text": "Lee, K. (2022). Field calibration methods for distributed sensor networks."},
            ],
        },
    ]


AGENCY_FIXTURES: List[Dict[str, Any]] = [
    {
        "agency": "NSF",
        "phase": "full_proposal",
        "title": "Modular Sensor Platform for Environmental Monitoring",
        "sections": [
            {"title": "Technical Approach", "structured_content": {"schemaVersion": "1.0", "blocks": _shared_blocks()}},
        ],
    },
    {
        "agency": "NIH",
        "phase": "full_proposal",
        "title": "Wearable Biosensor Platform for Continuous Patient Monitoring",
        "sections": [
            {"title": "Research Strategy", "structured_content": {"schemaVersion": "1.0", "blocks": _shared_blocks()}},
        ],
    },
    {
        "agency": "DOD",
        "phase": "full_proposal",
        "title": "Ruggedized Sensor Network for Contested Environment Monitoring",
        "sections": [
            {"title": "Technical Approach", "structured_content": {"schemaVersion": "1.0", "blocks": _shared_blocks()}},
        ],
    },
    {
        "agency": "GENERIC",
        "phase": "full_proposal",
        "title": "Community Environmental Health Sensor Network",
        "sections": [
            {"title": "Project Narrative", "structured_content": {"schemaVersion": "1.0", "blocks": _shared_blocks()}},
        ],
    },
]
