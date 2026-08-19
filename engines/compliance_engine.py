"""
Engine 5 — Formatting & Compliance Engine
Validates proposals against agency templates and compliance rules.

Word/PDF Report Generation Development Specification (CLARIVA-DOCGEN-SPEC-001),
Phase 5 — "Export QA gate": this engine already had the right shape for this
(ComplianceReport.passed = no error-severity violations, already wired for a
pass/fail gate) but was never actually called from any router; only its own
unit test exercised it. Rather than build a second, parallel "QA" engine with
its own report schema, this phase extends validate() with four new checks —
block-schema validity, unresolved [MISSING: ...] placeholders, malformed
tables, and malformed reference URLs — and routers/documents.py::export_proposal()
/ routers/proposals.py finally give it real callers (see ExportRequest.
require_clean_export and GET /proposals/{id}/compliance).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List

from models.schemas import Agency, ComplianceReport, ComplianceViolation


# Words per page estimates by section type
WORDS_PER_PAGE = 250

# Markdown characters the AI is instructed never to emit (proposal_generator.py
# BASE_SYSTEM_PROMPT) — a match here means either a leak from the block-JSON
# generation path or the legacy prose path, both worth flagging since the
# characters render literally in Word/PDF and look unprofessional/broken.
_MARKDOWN_LEAK_RE = re.compile(r'(\*\*[^*]+\*\*|__[^_]+__|^#{1,6}\s|^\s*[-*]\s|`[^`]+`)', re.MULTILINE)

# Deliberately permissive — this is a "does this look obviously broken" check,
# not a full RFC 3986 validator. Flags things like unescaped spaces, stray
# markdown-link brackets left behind by a botched (Source: ...) citation, or a
# URL missing its scheme, without false-positiving on ordinary long query strings.
_URL_RE = re.compile(r'https?://\S+')
_MALFORMED_URL_RE = re.compile(r'[\s<>\[\]"]|\.{2,}$')


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
        section_limits_override: Dict[str, int] = None,
        total_limit_override: int = None,
    ) -> ComplianceReport:
        """Run full compliance check on a proposal.

        Word/PDF Report Generation Development Specification
        (CLARIVA-DOCGEN-SPEC-001), Phase 7 — section_limits_override /
        total_limit_override let a caller inject the resolved, admin-editable
        AgencyProfile limits (see engines/agency_profile_engine.py::resolve())
        instead of this class's hardcoded AGENCY_SECTION_LIMITS/
        AGENCY_TOTAL_LIMITS. Deliberately plain dict/int params, not an async
        DB lookup here — validate() stays synchronous so its two existing
        callers (document_output.py::export(), routers/proposals.py's
        GET /compliance) don't have to change their own signatures; they
        resolve the AgencyProfile once (they already have a db session) and
        pass the result in. foa_template's per-section page limits still take
        precedence over everything when present — that's a proposal-specific
        override, one level more specific than an agency-wide profile."""
        violations: List[ComplianceViolation] = []
        page_counts: Dict[str, float] = {}
        agency = proposal.agency

        # Per-section page limit checks
        section_limits = {}
        if foa_template:
            for s in foa_template.ordered_sections:
                if s.page_limit:
                    section_limits[s.section_id] = s.page_limit
        elif section_limits_override is not None:
            section_limits = section_limits_override
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
        total_limit = (
            (foa_template.total_page_limit if foa_template else None)
            or total_limit_override
            or self.AGENCY_TOTAL_LIMITS.get(agency, 25)
        )
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

        # Phase 5 — export QA gate: schema validity, unresolved placeholders,
        # malformed tables/URLs, and stray markdown formatting.
        violations += self._structured_content_checks(sections)

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

    def _structured_content_checks(self, sections: List[Any]) -> List[ComplianceViolation]:
        """Phase 5 export QA gate. For sections with `structured_content`
        (Phase 4+), validates the block JSON and inspects the parsed blocks
        directly — this is stronger than string-scanning the derived
        `content` mirror, since a bug in flatten_section_blocks_to_text()
        could hide a real problem that's still present in what actually gets
        rendered into the DOCX/PDF. Sections without structured_content
        (pre-Phase-4 content, or a manual raw-text edit — see
        routers/proposals.py::update_section_content) fall back to
        string-scanning `content`, since that's all that exists for them.
        """
        from pydantic import ValidationError
        from models.schemas import (
            StructuredSectionContent, ParagraphBlock, RunInBlock, BulletListBlock,
            NumberedListBlock, TableBlock, FigureBlock, CalloutBlock, ReferencesBlock,
        )

        violations: List[ComplianceViolation] = []

        def _block_texts(block) -> List[str]:
            """Every user-facing string on a block, for the markdown-leak scan."""
            if isinstance(block, ParagraphBlock):
                return [block.text] if block.text else [r.text for r in (block.runs or [])]
            if isinstance(block, RunInBlock):
                return [block.label, block.text]
            if isinstance(block, (BulletListBlock, NumberedListBlock)):
                return list(block.items)
            if isinstance(block, TableBlock):
                return [block.caption or "", *block.columns, *[c for row in block.rows for c in row]]
            if isinstance(block, FigureBlock):
                return [block.caption, block.altText or ""]
            if isinstance(block, CalloutBlock):
                return [block.title or "", block.text]
            return []

        for section in sections:
            sid = getattr(section, "section_id", None)
            title = getattr(section, "title", sid or "Section")
            structured = getattr(section, "structured_content", None)

            if structured:
                try:
                    model = StructuredSectionContent.model_validate(structured)
                except ValidationError as exc:
                    violations.append(ComplianceViolation(
                        rule="Invalid section data",
                        severity="error",
                        section_id=sid,
                        detail=f"'{title}' has malformed structured content ({exc.error_count()} "
                                f"schema error(s)) and cannot be reliably rendered.",
                    ))
                    continue  # can't safely inspect blocks that failed validation

                markdown_leaked = False
                for block in model.blocks:
                    if isinstance(block, ParagraphBlock) and block.missing is not None:
                        violations.append(ComplianceViolation(
                            rule="Missing placeholder",
                            severity="error" if block.missing.severity == "blocking" else "warning",
                            section_id=sid,
                            detail=f"'{title}': {block.missing.label}",
                        ))
                    elif isinstance(block, CalloutBlock) and block.kind == "missing":
                        violations.append(ComplianceViolation(
                            rule="Missing placeholder",
                            severity="error",
                            section_id=sid,
                            detail=f"'{title}': {block.title or block.text}",
                        ))
                    elif isinstance(block, TableBlock):
                        ncols = len(block.columns)
                        table_label = f'"{block.caption}" ' if block.caption else ""
                        for i, row in enumerate(block.rows, start=1):
                            if len(row) != ncols:
                                violations.append(ComplianceViolation(
                                    rule="Malformed table",
                                    severity="error",
                                    section_id=sid,
                                    detail=f"'{title}': table {table_label}"
                                            f"row {i} has {len(row)} cell(s), expected {ncols}.",
                                ))
                        if any("|" in c for c in (*block.columns, *[c for row in block.rows for c in row])):
                            violations.append(ComplianceViolation(
                                rule="Possible unparsed table",
                                severity="warning",
                                section_id=sid,
                                detail=f"'{title}': a table cell contains a raw '|' character — "
                                        f"check for markdown-table syntax that wasn't converted to real rows.",
                            ))
                    elif isinstance(block, ReferencesBlock):
                        for entry in block.entries:
                            if entry.url and (not entry.url.startswith(("http://", "https://"))
                                               or _MALFORMED_URL_RE.search(entry.url)):
                                violations.append(ComplianceViolation(
                                    rule="Malformed reference URL",
                                    severity="warning",
                                    section_id=sid,
                                    detail=f"'{title}': reference URL looks malformed: {entry.url!r}",
                                ))

                    if not markdown_leaked and any(_MARKDOWN_LEAK_RE.search(t) for t in _block_texts(block) if t):
                        markdown_leaked = True
                        violations.append(ComplianceViolation(
                            rule="Markdown formatting leaked into content",
                            severity="warning",
                            section_id=sid,
                            detail=f"'{title}' contains markdown characters (**, ##, backticks, etc.) "
                                    f"that will render literally in the exported document.",
                        ))
            else:
                content = getattr(section, "content", None) or ""
                if not content.strip():
                    continue  # already flagged as "Empty section" above
                for m in re.finditer(r"\[MISSING:([^\]]+)\]", content):
                    violations.append(ComplianceViolation(
                        rule="Missing placeholder",
                        severity="error",
                        section_id=sid,
                        detail=f"'{title}': {m.group(1).strip()}",
                    ))
                if _MARKDOWN_LEAK_RE.search(content):
                    violations.append(ComplianceViolation(
                        rule="Markdown formatting leaked into content",
                        severity="warning",
                        section_id=sid,
                        detail=f"'{title}' contains markdown characters that will render literally "
                                f"in the exported document.",
                    ))
                for m in _URL_RE.finditer(content):
                    if _MALFORMED_URL_RE.search(m.group(0)):
                        violations.append(ComplianceViolation(
                            rule="Malformed reference URL",
                            severity="warning",
                            section_id=sid,
                            detail=f"'{title}': URL looks malformed: {m.group(0)!r}",
                        ))

        return violations
