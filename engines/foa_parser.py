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
