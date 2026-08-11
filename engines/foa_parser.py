"""
Engine 1 — FOA Parsing Engine
Extracts: required sections, evaluation criteria, page limits, compliance rules.
Supports: PDF, PDF with images, DOCX, DOCX with images, plain text, pasted text.
"""

from __future__ import annotations

import io
import json
import re
from typing import Any, Dict, List, Optional

import openai

from config import settings

# Optional PDF library
try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

# Optional DOCX library
try:
    from docx import Document as DocxDocument
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False


# NOTE: all literal { } in the JSON example are escaped as {{ }} so that
# .format(foa_text=...) does not raise KeyError on them.
PARSE_PROMPT = """You are an expert at analyzing federal funding opportunity announcements (FOAs) for SBIR/STTR programs.

Analyze the following FOA text and extract structured information in valid JSON.

CRITICAL DISTINCTION — "ordered_sections" must contain ONLY the PROPOSAL NARRATIVE SECTIONS that applicants must WRITE AND SUBMIT in their proposal response. Examples of valid proposal sections:
- Project Summary / Abstract
- Specific Aims
- Technical Merit / Technical Approach
- Innovation & Originality
- Intellectual Merit
- Broader Impacts
- Commercialization Plan / Market Opportunity
- Phase II Transition Plan
- Team Qualifications / Key Personnel
- Facilities & Resources
- Budget Justification
- References

DO NOT include any of these as ordered_sections — they are NOT proposal sections:
- Technology topic areas or research clusters (e.g. "Advanced Manufacturing", "Advanced Materials", "Agricultural Technologies", "Artificial Intelligence") — these are program subtopics/areas applicants choose from, NOT sections to write
- Administrative FOA headings (e.g. "Eligibility Requirements", "Application Preparation Instructions", "Award Information", "Program Overview", "Definitions")
- Tables of contents or part/section labels from the FOA document itself

If the FOA lists only topic areas and does not explicitly define proposal narrative sections, extract an empty list for ordered_sections — the system will apply agency-standard sections automatically.

Also produce two short plain-language fields so a reader can decide whether
this opportunity is worth pursuing WITHOUT reading the full solicitation:
- "summary": 2-4 sentences covering what this funds, who it's for, the
  approximate award amount (if stated), and the core ask.
- "eligibility_summary": 1-3 sentences on who is eligible to apply and any
  major restrictions (e.g. business size, nonprofit status, citizenship,
  prior award history). If the FOA doesn't state eligibility, say so briefly
  rather than inventing requirements.

Return ONLY a JSON object with this exact structure:
{{
  "agency": "<NSF|DOE|NIH|DOD|DARPA|ARPA-E|NASA|OTHER>",
  "program_title": "<string>",
  "solicitation_number": "<string or null>",
  "phase": "<pre_phase_i|phase_i|phase_ii|fast_track>",
  "total_page_limit": <integer or null>,
  "deadline": "<ISO8601 datetime or null>",
  "summary": "<2-4 sentence plain-language summary>",
  "eligibility_summary": "<1-3 sentence plain-language eligibility summary>",
  "ordered_sections": [
    {{
      "section_id": "<snake_case_id>",
      "title": "<section title>",
      "required": <true|false>,
      "page_limit": <integer or null>,
      "guidance": "<brief guidance from FOA>",
      "evaluation_weight": <0.0-1.0>
    }}
  ],
  "compliance_rules": ["<rule 1>", "<rule 2>"],
  "weights": {{
    "<section_id>": <float 0-1>
  }}
}}

FOA TEXT:
{foa_text}
"""

# A lighter prompt for on-demand backfill (POST /foa/{foa_id}/summarize) —
# used for FOAs parsed before `summary`/`eligibility_summary` existed. Asks
# for only the two short fields instead of the full section/weights
# extraction, so backfilling an old record is cheaper than a full re-parse.
SUMMARIZE_ONLY_PROMPT = """You are an expert federal grant analyst. Read the following funding opportunity text and produce two short plain-language fields so a reader can decide whether it's worth pursuing without reading the whole thing:
- "summary": 2-4 sentences covering what this funds, who it's for, the approximate award amount (if stated), and the core ask.
- "eligibility_summary": 1-3 sentences on who is eligible to apply and any major restrictions. If not stated, say so briefly rather than inventing requirements.

Return ONLY a JSON object: {{"summary": "<string>", "eligibility_summary": "<string>"}}

FOA TEXT:
{foa_text}
"""

# Funding Opportunity Intelligence, Phase 1 ("Clariva Funding Opportunity
# Intelligence Product Definition Specification — Grant Finding Workspace
# Upgrade") — the paid AI service backing POST /foa/{foa_id}/summarize.
# Replaces the two-field SUMMARIZE_ONLY_PROMPT above with a structured,
# 14-section report that answers "is this worth pursuing, why, what would
# it take to compete, and what should we do next" instead of a plain
# paragraph. Every ranking/status/level below must be explainable in plain
# language — never an unexplained numeric score or an unsupported
# win-probability claim (e.g. never "83% chance of winning").
INTELLIGENCE_REPORT_PROMPT = """You are a senior federal/private grants strategist producing a Funding Opportunity Intelligence Report for an organization deciding whether to pursue this opportunity. Every judgment you make must be explained in plain language grounded in the text below — never output an unexplained score, and never state or imply a win-probability percentage.

Analyze the following funding opportunity text and return ONLY a JSON object with this exact structure:
{{
  "executive_brief": "<3-5 sentences: what this funds, who it's for, and the core ask — enough for a reader to decide whether to keep reading>",
  "funding_information": {{
    "award_floor": <number or null>,
    "award_ceiling": <number or null>,
    "total_program_funding": <number or null>,
    "expected_number_of_awards": <integer or null>,
    "period_of_performance": "<string, e.g. '24 months', or null>"
  }},
  "eligibility_assessment": {{
    "status": "<Eligible|Conditional|Unlikely|Requires Verification>",
    "explanation": "<plain-language explanation for the status above>",
    "issues": ["<specific eligibility issue or open question, if any>"]
  }},
  "funding_priorities": ["<priority/topic area the funder cares about>"],
  "requirements": ["<concrete requirement an applicant must satisfy>"],
  "evaluation_criteria": [
    {{"criterion": "<name>", "weight": "<e.g. '30%' or 'Not specified'>", "practical_implication": "<what this means for how the proposal should be written>"}}
  ],
  "required_documents": ["<document/attachment the application must include>"],
  "cost_share": {{"required": <true|false|null>, "details": "<cost-share/match requirement, or 'None identified'>"}},
  "deadline_analysis": "<how much lead time this gives and what that implies for readiness>",
  "complexity": {{"level": "<Low|Moderate|High|Very High>", "reason": "<what drives this complexity rating>"}},
  "key_risks": [
    {{"risk": "<specific risk to pursuing or winning this>", "severity": "<Low|Medium|High>"}}
  ],
  "opportunity_attractiveness": {{"level": "<High|Moderate|Low>", "reason": "<plain-language justification, never a numeric score>"}},
  "go_no_go_considerations": ["<factor the organization should weigh — NOT a recommendation to bid or not bid>"],
  "recommended_next_actions": ["<concrete next step, e.g. 'Confirm cost-share capacity with finance' or 'Assign a technical lead to review Section 3'>"]
}}

If the text doesn't state a field, use null (for numbers) or a brief honest statement like "Not specified in the available text" (for strings) rather than inventing information.

FOA TEXT:
{foa_text}
"""

# Fixed, non-AI-generated text appended to every report — not something the
# model is trusted to phrase consistently or to remember to include. Per
# explicit product requirement: "Each report must include disclaimer and
# must always include human in the loop."
INTELLIGENCE_REPORT_DISCLAIMER = (
    "This report is AI-generated decision support based on the text of this "
    "opportunity as parsed by Clariva. It is not legal, financial, or "
    "compliance advice, and it may contain errors or omissions. Always "
    "verify eligibility, deadlines, funding amounts, and requirements "
    "against the official solicitation and funder guidance before acting."
)
HUMAN_IN_THE_LOOP_NOTE = (
    "A qualified person at your organization must review this report and "
    "make the final pursue/no-pursue decision. Clariva does not submit "
    "applications, commit your organization, or make Go/No-Go decisions on "
    "its own — every recommendation here is an input to your team's "
    "judgment, not a substitute for it."
)


class FOAParserEngine:
    """
    Extracts structured metadata from raw FOA text using GPT-4o.
    Supports PDF, PDF with images, DOCX, plain text, and pasted text.
    """

    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    def extract_text(self, raw_bytes: bytes, filename: str) -> str:
        """Extract plain text from uploaded bytes (PDF, DOCX, or TXT)."""
        name = (filename or "").lower()
        if name.endswith(".pdf"):
            return self._extract_pdf(raw_bytes)
        if name.endswith(".docx") or name.endswith(".doc"):
            return self._extract_docx(raw_bytes)
        # Plain text / pasted text
        return raw_bytes.decode("utf-8", errors="replace")

    def extract_text_from_string(self, text: str) -> str:
        """Accept pasted plain text directly."""
        return text.strip()

    def _extract_pdf(self, raw_bytes: bytes) -> str:
        """Extract text from PDF, with pdfplumber preferred, pypdf fallback."""
        if HAS_PDFPLUMBER:
            try:
                text_parts: List[str] = []
                with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
                    for page in pdf.pages:
                        text = page.extract_text()
                        if text:
                            text_parts.append(text)
                result = "\n\n".join(text_parts)
                if result.strip():
                    return result
            except Exception as e:
                print(f"[foa_parser] pdfplumber failed: {e}, trying pypdf")

        # Fallback: pypdf
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
            parts = []
            for page in reader.pages:
                t = page.extract_text()
                if t:
                    parts.append(t)
            result = "\n\n".join(parts)
            if result.strip():
                return result
        except Exception as e:
            print(f"[foa_parser] pypdf failed: {e}")

        # Last resort: decode raw bytes (might be garbage for binary PDF)
        return raw_bytes.decode("utf-8", errors="replace")

    def _extract_docx(self, raw_bytes: bytes) -> str:
        """Extract text from DOCX (including text in tables and headers)."""
        if not HAS_DOCX:
            # Fallback: try to extract readable text from raw bytes
            return raw_bytes.decode("utf-8", errors="replace")
        try:
            doc = DocxDocument(io.BytesIO(raw_bytes))
            parts: List[str] = []
            for para in doc.paragraphs:
                if para.text.strip():
                    parts.append(para.text)
            # Also extract table cells
            for table in doc.tables:
                for row in table.rows:
                    row_text = " | ".join(
                        cell.text.strip() for cell in row.cells if cell.text.strip()
                    )
                    if row_text:
                        parts.append(row_text)
            return "\n\n".join(parts)
        except Exception as e:
            print(f"[foa_parser] docx extraction failed: {e}")
            return raw_bytes.decode("utf-8", errors="replace")

    def _parse_json(self, text: str) -> dict:
        """Multi-strategy JSON extraction from LLM response."""
        text = text.strip()
        # Strip markdown code fences
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        # Direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Find first { ... }
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        print("[foa_parser] Could not parse JSON from LLM response, using defaults.")
        return {}

    async def parse(self, raw_text: str) -> Dict[str, Any]:
        """Call GPT-4o to parse FOA and return structured dict."""
        truncated = raw_text[:12000]

        response = await self.client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert federal grant analyst. Always return valid JSON only.",
                },
                {
                    "role": "user",
                    "content": PARSE_PROMPT.format(foa_text=truncated),
                },
            ],
            temperature=0.1,
        )

        raw_json = response.choices[0].message.content or ""
        parsed = self._parse_json(raw_json)
        return self._normalize(parsed)

    async def summarize(self, raw_text: str) -> Dict[str, Optional[str]]:
        """
        Cheaper sibling of parse() for on-demand backfill of FOAs that were
        parsed before `summary`/`eligibility_summary` existed (or synced
        records that were never AI-parsed at all) — asks GPT-4o for only
        the two short fields instead of the full section/weights
        extraction. Returns {"summary": ..., "eligibility_summary": ...},
        both possibly None if the model returned nothing usable.
        """
        truncated = raw_text[:12000]
        response = await self.client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert federal grant analyst. Always return valid JSON only.",
                },
                {
                    "role": "user",
                    "content": SUMMARIZE_ONLY_PROMPT.format(foa_text=truncated),
                },
            ],
            temperature=0.1,
        )
        raw_json = response.choices[0].message.content or ""
        parsed = self._parse_json(raw_json)
        return {
            "summary": parsed.get("summary") or None,
            "eligibility_summary": parsed.get("eligibility_summary") or None,
        }

    async def analyze_opportunity(self, raw_text: str) -> Dict[str, Any]:
        """
        Funding Opportunity Intelligence, Phase 1 — the paid "Analyze
        Opportunity" service. Upgrades from the two-field summarize() above
        into a structured, 14-section pursuit-decision report (see
        INTELLIGENCE_REPORT_PROMPT) so the output answers "is this worth
        pursuing, why, what would it take to compete, and what should we do
        next" instead of a plain paragraph.

        Returns a dict with:
          - "report": the full structured report (dict), always containing
            a "disclaimer" and "human_in_the_loop_note" field — these two
            are fixed, non-AI-generated strings appended here rather than
            trusted to the model, per explicit product requirement.
          - "summary": a short plain-language string derived from
            executive_brief, so callers can keep populating the existing
            `ai_summary` column for list-view backward compatibility.
          - "eligibility_status", "eligibility_summary", "complexity",
            "attractiveness", "attractiveness_reason": cached scalar
            classifications for fast card rendering (FOARecord's
            corresponding columns).
        """
        truncated = raw_text[:12000]
        response = await self.client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert federal grant strategist. Always return "
                        "valid JSON only. Never state or imply a numeric win "
                        "probability."
                    ),
                },
                {
                    "role": "user",
                    "content": INTELLIGENCE_REPORT_PROMPT.format(foa_text=truncated),
                },
            ],
            temperature=0.1,
        )
        raw_json = response.choices[0].message.content or ""
        parsed = self._parse_json(raw_json)
        report = self._normalize_intelligence_report(parsed)

        eligibility = report.get("eligibility_assessment") or {}
        complexity = report.get("complexity") or {}
        attractiveness = report.get("opportunity_attractiveness") or {}

        # Phase 3 §4.7 — Administrator-Only Engineering Economics. Token
        # usage riding along in the return dict (rather than a separate
        # return value) so every existing caller keeps working unchanged;
        # the one caller that cares (routers/foa.py::_analyze_single_opportunity)
        # pops "_usage" and hands it to engines/usage_tracking.record_usage().
        # See that module's usage_from_response() for the tolerant-of-a-
        # missing-.usage extraction.
        from engines.usage_tracking import usage_from_response
        prompt_tokens, completion_tokens = usage_from_response(response)

        return {
            "report": report,
            "summary": report.get("executive_brief"),
            "eligibility_status": eligibility.get("status"),
            "eligibility_summary": eligibility.get("explanation"),
            "complexity": complexity.get("level"),
            "attractiveness": attractiveness.get("level"),
            "attractiveness_reason": attractiveness.get("reason"),
            "_usage": {"model": settings.OPENAI_MODEL, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        }

    def _normalize_intelligence_report(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Fill in safe defaults for any field the model omitted, and
        always attach the fixed disclaimer/human-in-the-loop text."""
        data.setdefault("executive_brief", None)
        data.setdefault("funding_information", {})
        data.setdefault("eligibility_assessment", {"status": "Requires Verification", "explanation": None, "issues": []})
        data.setdefault("funding_priorities", [])
        data.setdefault("requirements", [])
        data.setdefault("evaluation_criteria", [])
        data.setdefault("required_documents", [])
        data.setdefault("cost_share", {"required": None, "details": None})
        data.setdefault("deadline_analysis", None)
        data.setdefault("complexity", {"level": "Moderate", "reason": None})
        data.setdefault("key_risks", [])
        data.setdefault("opportunity_attractiveness", {"level": "Moderate", "reason": None})
        data.setdefault("go_no_go_considerations", [])
        data.setdefault("recommended_next_actions", [])
        # Always present, always this exact text — see module-level
        # INTELLIGENCE_REPORT_DISCLAIMER / HUMAN_IN_THE_LOOP_NOTE docstrings.
        data["disclaimer"] = INTELLIGENCE_REPORT_DISCLAIMER
        data["human_in_the_loop_note"] = HUMAN_IN_THE_LOOP_NOTE
        return data

    def _normalize(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure required fields exist with defaults."""
        data.setdefault("agency", "OTHER")
        data.setdefault("program_title", "Unknown Program")
        data.setdefault("solicitation_number", None)
        data.setdefault("phase", "phase_i")
        data.setdefault("total_page_limit", None)
        data.setdefault("deadline", None)
        data.setdefault("ordered_sections", self._default_sections())
        data.setdefault("compliance_rules", [])
        data.setdefault("weights", {})
        data.setdefault("summary", None)
        data.setdefault("eligibility_summary", None)

        weights = data["weights"]
        total = sum(weights.values()) if weights else 0
        if total > 0:
            data["weights"] = {k: v / total for k, v in weights.items()}

        return data

    def _default_sections(self) -> List[Dict[str, Any]]:
        """Generic SBIR sections when FOA parsing is incomplete."""
        return [
            {"section_id": "project_summary", "title": "Project Summary",
             "required": True, "page_limit": 1, "guidance": "Brief overview of the project.",
             "evaluation_weight": 0.05},
            {"section_id": "specific_aims", "title": "Specific Aims",
             "required": True, "page_limit": 1, "guidance": "State objectives and hypothesis.",
             "evaluation_weight": 0.10},
            {"section_id": "technical_merit", "title": "Technical Merit & Innovation",
             "required": True, "page_limit": 6, "guidance": "Describe technical approach and innovation.",
             "evaluation_weight": 0.30},
            {"section_id": "commercialization", "title": "Commercialization Plan",
             "required": True, "page_limit": 4, "guidance": "Market analysis and go-to-market strategy.",
             "evaluation_weight": 0.25},
            {"section_id": "team_qualifications", "title": "Team Qualifications",
             "required": True, "page_limit": 2, "guidance": "Describe team expertise.",
             "evaluation_weight": 0.15},
            {"section_id": "budget_justification", "title": "Budget Justification",
             "required": True, "page_limit": 2, "guidance": "Itemized budget with rationale.",
             "evaluation_weight": 0.10},
            {"section_id": "references", "title": "References",
             "required": False, "page_limit": None, "guidance": "Supporting literature.",
             "evaluation_weight": 0.05},
        ]
