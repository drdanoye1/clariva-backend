"""
Engine 5 — Formatting & Compliance Engine
Validates proposals against agency templates and compliance rules.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List

from models.schemas import Agency, ComplianceReport, ComplianceViolation


# Words per page estimates by section type
WORDS_PER_PAGE = 250


class ComplianceEngine:
    """
    Validates proposal content against:
    - Page limits (per section and total)
    - Required section presence
    - Agency-specific formatting rules
    """

    # ── Agency page limits ────────────────────────────────────────────────────

    AGENCY_SECTION_LIMITS: Dict[str, Dict[str, int]] = {
        Agency.NSF: {
            "project_summary": 1, "project_narrative": 15,
            "commercialization": 5, "team": 2,
        },
        Agency.NIH: {
            "specific_aims": 1, "approach": 6,
            "commercialization": 12, "budget_justification": 4,
        },
        Agency.DOE: {
            "concept_paper": 4, "technical_merit": 8,
            "commercialization": 6, "team": 3, "facilities": 2,
        },
        Agency.DOD: {
            "technical_abstract": 1, "technical_merit": 12,
            "commercialization": 5, "team": 2, "facilities": 1,
        },
        Agency.DARPA: {
            "technical_abstract": 1, "technical_merit": 10,
            "commercialization": 4, "team": 2,
        },
    }

    AGENCY_TOTAL_LIMITS: Dict[str, int] = {
        Agency.NSF:    25,
        Agency.NIH:    25,
        Agency.DOE:    25,
        Agency.DOD:    20,
        Agency.DARPA:  20,
        Agency.ARPA_E: 20,
        Agency.NASA:   25,
    }

    def validate(
        self,
        proposal: Any,
        sections: List[Any],
        compliance_rules: List[str] = None,
        foa_template: Any = None,
    ) -> ComplianceReport:
        """Run full compliance check on a proposal."""
        violations: List[ComplianceViolation] = []
        page_counts: Dict[str, float] = {}
        agency = proposal.agency

        # Per-section page limit checks
        section_limits = {}
        if foa_template:
            for s in foa_template.ordered_sections:
                if s.page_limit:
                    section_limits[s.section_id] = s.page_limit
        else:
            section_limits = self.AGENCY_SECTION_LIMITS.get(agency, {})

        for section in sections:
            pages = section.word_count / WORDS_PER_PAGE
            page_counts[section.section_id] = round(pages, 2)

            limit = section_limits.get(section.section_id)
            if limit and pages > limit:
                violations.append(ComplianceViolation(
                    rule=f"Page limit: {section.title}",
                    severity="error",
                    section_id=section.section_id,
                    detail=f"Section is {pages:.1f} pages but limit is {limit} page(s).",
                ))

            # Check for empty required sections
            if not section.content or section.content.strip() == "":
                violations.append(ComplianceViolation(
                    rule="Empty section",
                    severity="warning",
                    section_id=section.section_id,
                    detail=f"Section '{section.title}' has no content.",
                ))

        # Total page limit
        total_pages = sum(page_counts.values())
        total_limit = (foa_template.total_page_limit if foa_template else None) or \
                      self.AGENCY_TOTAL_LIMITS.get(agency, 25)
        if total_pages > total_limit:
            violations.append(ComplianceViolation(
                rule="Total page limit",
                severity="error",
                section_id=None,
                detail=f"Proposal is {total_pages:.1f} pages; limit is {total_limit} pages.",
            ))

        # Custom FOA compliance rules
        for rule in (compliance_rules or []):
            violation = self._check_custom_rule(rule, sections, page_counts)
            if violation:
                violations.append(violation)

        # Agency-specific quality checks
        violations += self._quality_checks(proposal, sections)

        return ComplianceReport(
            proposal_id=proposal.id,
            passed=not any(v.severity == "error" for v in violations),
            violations=violations,
            page_counts=page_counts,
            total_pages=round(total_pages, 2),
            checked_at=datetime.utcnow(),
        )

    def _check_custom_rule(
        self, rule: str, sections: List[Any], page_counts: Dict[str, float]
    ) -> ComplianceViolation | None:
        """Parse and check a plain-text compliance rule."""
        rule_lower = rule.lower()

        # Pattern: "X must not exceed N page(s)"
        m = re.search(r"(\w[\w\s]+?)\s+must not exceed (\d+) page", rule_lower)
        if m:
            section_hint = m.group(1).strip().replace(" ", "_")
            limit = int(m.group(2))
            for sec_id, pages in page_counts.items():
                if section_hint in sec_id.lower() and pages > limit:
                    return ComplianceViolation(
                        rule=rule,
                        severity="error",
                        section_id=sec_id,
                        detail=f"Exceeds {limit} page limit ({pages:.1f} pages actual).",
                    )
        return None

    def _quality_checks(self, proposal: Any, sections: List[Any]) -> List[ComplianceViolation]:
        """Non-page quality violations (warnings only)."""
        violations = []
        content_map = {s.section_id: s.content or "" for s in sections}

        # Check commercialization mentions market size
        comm = content_map.get("commercialization", "")
        if comm and not re.search(r"\$[\d,]+|\bbillion\b|\bmillion\b|\bmarket size\b", comm, re.I):
            violations.append(ComplianceViolation(
                rule="Commercialization quality",
                severity="warning",
                section_id="commercialization",
                detail="Commercialization section should include market size estimates.",
            ))

        # Check technical section mentions milestones/timeline
        tech = content_map.get("technical_merit", content_map.get("approach", ""))
        if tech and not re.search(r"\bmilestone\b|\btimeline\b|\bmonth\b|\bphase\b", tech, re.I):
            violations.append(ComplianceViolation(
                rule="Technical section quality",
                severity="warning",
                section_id="technical_merit",
                detail="Technical section should include milestones and a timeline.",
            ))

        return violations
