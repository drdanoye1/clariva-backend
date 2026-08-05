"""
Engine 2 — FOA Template Builder Engine  [CORE DIFFERENTIATOR]
Converts parsed FOA data → dynamic proposal architecture (FOATemplate).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from models.schemas import Agency, FOASection, FOATemplate, Phase


class FOATemplateBuilderEngine:
    """
    The core differentiator of the Clariva platform.
    Transforms raw FOA parse output into a validated, ordered FOATemplate
    that drives all downstream engines.
    """

    def build(self, parsed: Dict[str, Any]) -> FOATemplate:
        """
        Input:  Output of FOAParserEngine.parse()
        Output: FOATemplate — the single source of truth for this proposal.
        """
        agency = self._resolve_agency(parsed.get("agency", "OTHER"))
        phase  = self._resolve_phase(parsed.get("phase", "phase_i"))

        raw_sections = parsed.get("ordered_sections", [])
        weights      = parsed.get("weights", {})

        # Build FOASection objects with enriched metadata.
        # Raw weights come from FOA parsing (often GPT-4o's JSON output) and
        # aren't guaranteed to be well-formed fractions — e.g. a weight
        # meant as "20%" occasionally comes back as 20 instead of 0.2.
        # FOASection.evaluation_weight is constrained to [0, 1], so an
        # out-of-range raw value would otherwise raise a ValidationError and
        # abort proposal creation entirely, before _normalize_weights() ever
        # gets a chance to fix the proportions below. Clamp defensively here;
        # normalization still restores correct relative weighting afterward.
        sections = [
            FOASection(
                section_id=s.get("section_id", f"section_{i}"),
                title=s.get("title", f"Section {i+1}"),
                required=s.get("required", True),
                page_limit=s.get("page_limit"),
                guidance=self._enrich_guidance(s, agency, phase),
                evaluation_weight=max(0.0, min(1.0, weights.get(
                    s.get("section_id", ""), s.get("evaluation_weight", 0.1)
                ))),
            )
            for i, s in enumerate(raw_sections)
        ]

        # Validate & normalize weights
        sections = self._normalize_weights(sections)

        # Parse deadline
        deadline = None
        raw_deadline = parsed.get("deadline")
        if raw_deadline:
            try:
                deadline = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                deadline = None

        return FOATemplate(
            foa_id="",  # assigned by DB after save
            agency=agency,
            program_title=parsed.get("program_title", "Unknown Program"),
            solicitation_number=parsed.get("solicitation_number"),
            phase=phase,
            total_page_limit=parsed.get("total_page_limit"),
            ordered_sections=sections,
            compliance_rules=self._build_compliance_rules(parsed, agency),
            deadline=deadline,
            weights={s.section_id: s.evaluation_weight for s in sections},
        )

    # ── Agency/Phase resolution ───────────────────────────────────────────────

    def _resolve_agency(self, raw: str) -> Agency:
        mapping = {
            "NSF": Agency.NSF, "DOE": Agency.DOE, "NIH": Agency.NIH,
            "DOD": Agency.DOD, "DARPA": Agency.DARPA, "ARPA-E": Agency.ARPA_E,
            "ARPA_E": Agency.ARPA_E, "NASA": Agency.NASA,
        }
        return mapping.get(raw.upper(), Agency.OTHER)

    def _resolve_phase(self, raw: str) -> Phase:
        mapping = {
            "pre_phase_i": Phase.PRE_PHASE_I,
            "phase_i": Phase.PHASE_I,
            "phase_ii": Phase.PHASE_II,
            "fast_track": Phase.FAST_TRACK,
        }
        return mapping.get(raw.lower(), Phase.PHASE_I)

    # ── Guidance enrichment ──────────────────────────────────────────────────

    AGENCY_GUIDANCE: Dict[str, Dict[str, str]] = {
        "technical_merit": {
            Agency.NSF: (
                "NSF emphasizes scientific merit, intellectual merit, and broader impacts. "
                "Clearly state hypotheses and experimental approaches."
            ),
            Agency.NIH: (
                "NIH reviewers score on Significance, Investigator, Innovation, Approach, "
                "and Environment (SIIIA). Address each criterion explicitly."
            ),
            Agency.DARPA: (
                "DARPA values revolutionary breakthroughs over incremental advances. "
                "Emphasize technical risk and transformative potential."
            ),
        },
        "commercialization": {
            Agency.DOE: (
                "DOE requires a detailed Technology Transfer plan and identification of "
                "licensing or spin-off opportunities."
            ),
            Agency.NSF: (
                "NSF SBIR mandates a clear path to commercialization with specific customer "
                "discovery evidence."
            ),
        },
    }

    def _enrich_guidance(self, section: Dict[str, Any], agency: Agency, phase: Phase) -> str:
        base = section.get("guidance", "")
        section_id = section.get("section_id", "")

        extra = (
            self.AGENCY_GUIDANCE.get(section_id, {}).get(agency, "")
        )
        if extra:
            return f"{base}\n\nAgency Note: {extra}"
        return base

    # ── Compliance rules ──────────────────────────────────────────────────────

    AGENCY_COMPLIANCE: Dict[str, list] = {
        Agency.NSF: [
            "Project Summary must not exceed 1 page",
            "References do not count against page limit",
            "Font size minimum 11pt",
            "Margins minimum 1 inch on all sides",
        ],
        Agency.NIH: [
            "Research Strategy must not exceed 6 pages for Phase I",
            "All pages must include header with application number",
            "All figures must have legends",
            "NIH SF424 R&R forms required",
        ],
        Agency.DOD: [
            "Technical Volume limited per solicitation",
            "Cost Accounting Standards (CAS) disclosure required",
            "DD Form 2345 required for export control",
        ],
        Agency.DARPA: [
            "BAA-specific page limits are strictly enforced",
            "No color figures unless explicitly permitted",
            "Abstract limited to 200 words",
        ],
    }

    def _build_compliance_rules(self, parsed: Dict[str, Any], agency: Agency) -> list:
        base_rules = parsed.get("compliance_rules", [])
        agency_rules = self.AGENCY_COMPLIANCE.get(agency, [])
        # Merge, deduplicate
        all_rules = list(dict.fromkeys(base_rules + agency_rules))
        return all_rules

    # ── Weight normalization ──────────────────────────────────────────────────

    def _normalize_weights(self, sections: list) -> list:
        total = sum(s.evaluation_weight for s in sections)
        if total == 0:
            equal_weight = 1.0 / len(sections) if sections else 1.0
            for s in sections:
                object.__setattr__(s, "evaluation_weight", equal_weight)
        elif abs(total - 1.0) > 0.01:
            for s in sections:
                object.__setattr__(s, "evaluation_weight", s.evaluation_weight / total)
        return sections
