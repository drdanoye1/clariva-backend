"""
Engine 19 — Supporting Documents Engine
AI-drafts the Communication, Partnership, Planning, and (partial)
Organizational document types from the original "Supporting Documents
Studio" concept (Cover Letter, Letter of Inquiry, Concept Paper; Letter
of Support, Letter of Commitment, Memorandum of Understanding; Logic
Model, Monitoring & Evaluation Plan, Sustainability Plan, Risk Management
Plan, Data Management Plan, Project Management Plan; Capability
Statement) — a module that appeared in the pre-v5 planning briefs but was
dropped during consolidation into PRD v5 and so was never built. Added
post-launch once the gap was flagged directly by the product owner; see
docs/ARCHITECTURE.md's Phase 3 addendum for the full design rationale.
The remaining Organizational items (Org Profile, Key Personnel,
Facilities, Past Performance as *standalone exports*) stay deferred —
those are just re-renders of fields already editable on the Company
Profile page, whereas a Capability Statement is a genuinely new
synthesized document worth generating.

Design notes (same discipline as engines/scope_of_work_engine.py):
- This engine only *generates content* (a plain-text draft). Persistence
  reuses Phase 3's existing DocumentLibraryEngine.create_document() as-is —
  a generated letter becomes a normal versioned Document, tagged with one
  of the new SUPPORTING_DOCUMENT_TYPES keys as its library_type. No new
  tables, no new storage/versioning/sharing logic: "every generator uses
  identical project information" and consumes the shared Project Knowledge
  Base per the PRD's own framing, which is exactly what reusing the
  Document Library achieves for free.
- `SUPPORTING_DOCUMENT_TYPES` is the registry the frontend's type dropdown
  and the router's validation both read from — same pattern as
  connector_engine.py's `CONNECTOR_TYPES`.
- Every method takes an already-open AsyncSession and only flushes, never
  commits — the caller's request-scoped session commits once, after the
  router handler returns.
"""
from __future__ import annotations

import logging
import traceback
from typing import Any, Dict, Optional

import openai
from fastapi import HTTPException

from config import settings

_log = logging.getLogger(__name__)


SUPPORTING_DOCUMENT_TYPES: Dict[str, Dict[str, str]] = {
    "cover_letter": {
        "label": "Cover Letter",
        "category": "Communication",
        "focus": (
            "a brief, professional cover letter to accompany a full grant proposal submission — "
            "introduces the organization, states the funding request and amount, and summarizes "
            "why the project fits the funder's priorities. 2-3 short paragraphs."
        ),
    },
    "letter_of_inquiry": {
        "label": "Letter of Inquiry",
        "category": "Communication",
        "focus": (
            "a Letter of Inquiry (LOI) — a short pre-proposal pitch some funders require before "
            "inviting a full application. States the problem, the proposed approach, the amount "
            "being requested, and why this organization is positioned to do the work. 3-4 paragraphs, "
            "written to stand alone without a full proposal attached."
        ),
    },
    "concept_paper": {
        "label": "Concept Paper",
        "category": "Communication",
        "focus": (
            "a concept paper — a short narrative pitching the project idea before a full proposal is "
            "developed: the need, the proposed activities, intended outcomes, and rough scale/cost. "
            "4-6 paragraphs, more detailed than a Letter of Inquiry but still pre-proposal."
        ),
    },
    "letter_of_support": {
        "label": "Letter of Support",
        "category": "Partnership",
        "focus": (
            "a letter of support, written from the perspective of a partner organization endorsing "
            "this project — expresses enthusiasm for the project's goals and explains why the partner "
            "believes it should be funded, without committing specific resources (that's a Letter of "
            "Commitment, a different document). 2-3 paragraphs, addressed to the funder."
        ),
    },
    "letter_of_commitment": {
        "label": "Letter of Commitment",
        "category": "Partnership",
        "focus": (
            "a letter of commitment, written from the perspective of a partner organization formally "
            "committing specific resources, staff time, facilities, or matching funds to this project "
            "if it is funded. Should name the specific commitment being made, not just general support. "
            "2-3 paragraphs, addressed to the funder."
        ),
    },
    "mou": {
        "label": "Memorandum of Understanding (MOU)",
        "category": "Partnership",
        "focus": (
            "a Memorandum of Understanding (MOU) between the applicant organization and a named "
            "partner organization — outlines each party's roles and responsibilities, the scope of "
            "collaboration, and the term of the agreement. Written as a formal but non-binding "
            "agreement, with clearly separated sections for each party's responsibilities."
        ),
    },
    "logic_model": {
        "label": "Logic Model",
        "category": "Planning",
        "focus": (
            "a logic model narrative for this project, walking through the causal chain from "
            "resources to results. Write exactly five short paragraphs, each opening with its stage "
            "name (Inputs, Activities, Outputs, Outcomes, Impact) so it can later be reformatted into "
            "a table: Inputs (resources/funding/staff committed), Activities (what will be done), "
            "Outputs (immediate, countable products of those activities), Outcomes (short/medium-term "
            "changes for the target population), and Impact (the long-term change the project "
            "contributes to)."
        ),
    },
    "me_plan": {
        "label": "Monitoring & Evaluation Plan",
        "category": "Planning",
        "focus": (
            "a Monitoring & Evaluation (M&E) Plan describing how project performance will be tracked "
            "and evaluated. Treat monitoring (ongoing tracking of activities and outputs against the "
            "workplan) and evaluation (periodic assessment of whether outcomes/impact are being "
            "achieved) as distinct, and for each describe what will be measured, how data will be "
            "collected, how often, and who is responsible. 4-6 paragraphs."
        ),
    },
    "sustainability_plan": {
        "label": "Sustainability Plan",
        "category": "Planning",
        "focus": (
            "a Sustainability Plan describing how the project's activities, benefits, or "
            "infrastructure will continue after the grant funding period ends — covering funding "
            "diversification, institutionalization within the organization, ongoing partnerships, "
            "or capacity building that outlasts the award. 3-5 paragraphs."
        ),
    },
    "risk_management_plan": {
        "label": "Risk Management Plan",
        "category": "Planning",
        "focus": (
            "a Risk Management Plan identifying the most significant risks to this project's success "
            "(programmatic, financial, operational, or partner-related) and the mitigation strategy "
            "for each. Open with a brief framing paragraph, then one paragraph per major risk naming "
            "the risk, its likely severity, and the planned mitigation. 4-6 paragraphs total."
        ),
    },
    "data_management_plan": {
        "label": "Data Management Plan",
        "category": "Planning",
        "focus": (
            "a Data Management Plan describing what data the project will collect or generate, how "
            "it will be stored and secured, who will have access during the project, and how it will "
            "be shared or archived after the project ends — in the style expected by federal funders "
            "that require a DMP (e.g. NSF, NIH). 4-5 paragraphs."
        ),
    },
    "project_management_plan": {
        "label": "Project Management Plan",
        "category": "Planning",
        "focus": (
            "a Project Management Plan describing how the project will be governed and executed: "
            "team roles and responsibilities, communication and reporting cadence, decision-making "
            "process, and how schedule, budget, and scope will be tracked and controlled over the "
            "period of performance. 4-6 paragraphs."
        ),
    },
    "capability_statement": {
        "label": "Capability Statement",
        "category": "Organizational",
        "focus": (
            "a one-page Capability Statement in the standard government-contracting format used to "
            "introduce a company to a funder, prime contractor, or partner. Write exactly five "
            "paragraphs, each opening with its section name so it can later be reformatted into a "
            "flyer layout: Company Overview (who the organization is and what it does, in 2-3 "
            "sentences), Core Competencies (the organization's primary technical capabilities and "
            "areas of expertise), Differentiators (what sets this organization apart — past "
            "performance, certifications, unique technology, or key personnel expertise), Past "
            "Performance (2-3 of the most relevant prior awards or contracts, naming the funder/"
            "client and outcome), and Company Data (organization name, UEI, CAGE code, and any "
            "certifications, formatted as a short list of labeled facts within the paragraph). Do "
            "not fabricate a UEI, CAGE code, or past performance record that wasn't provided below — "
            "omit that fact rather than inventing one."
        ),
    },
}


def _ai_error(exc: Exception) -> HTTPException:
    """Local copy per this codebase's convention (see the near-identical
    helper in engines/scope_of_work_engine.py) — no provider names exposed."""
    if isinstance(exc, openai.RateLimitError):
        _log.error("AI service rate limit exceeded: %s", exc)
        return HTTPException(status_code=503, detail="The AI writing service is temporarily unavailable due to high demand. Please try again in a few minutes.")
    if isinstance(exc, openai.AuthenticationError):
        _log.critical("AI service authentication failure: %s", exc)
        return HTTPException(status_code=503, detail="The AI writing service is not configured correctly. Please contact support.")
    if isinstance(exc, openai.APIConnectionError):
        _log.error("AI service connection error: %s", exc)
        return HTTPException(status_code=503, detail="The AI writing service is unreachable. Please try again in a moment.")
    if isinstance(exc, openai.APIStatusError):
        _log.error("AI service API error %s: %s", getattr(exc, "status_code", "?"), getattr(exc, "message", str(exc)))
        return HTTPException(status_code=503, detail="The AI writing service returned an unexpected error. Please try again.")
    _log.error("Unexpected supporting-document generation error:\n%s", traceback.format_exc())
    return HTTPException(status_code=500, detail="Document generation failed. Please try again or contact support.")


class SupportingDocumentsEngine:
    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    def _profile_block(self, company_profile: Dict[str, Any]) -> str:
        """Grounds the prompt in whatever the org has actually filled out on
        the Company Profile page (routers/proposals.py::_load_company_profile
        returns the full dict; every other caller of this engine used to
        only read organization_name/industry/company_capabilities, leaving
        UEI/CAGE, key personnel, facilities, and past performance on the
        table). Every line here is opt-in — only appears if the org filled
        it in — since most document types won't need most of this, but the
        Capability Statement genuinely needs all of it."""
        lines = []
        if company_profile.get("organization_name"):
            lines.append(f"Organization: {company_profile['organization_name']}")
        if company_profile.get("industry"):
            lines.append(f"Industry: {company_profile['industry']}")
        if company_profile.get("uei_number"):
            lines.append(f"UEI: {company_profile['uei_number']}")
        if company_profile.get("cage_code"):
            lines.append(f"CAGE Code: {company_profile['cage_code']}")
        if company_profile.get("core_technologies"):
            lines.append(f"Core technologies: {', '.join(company_profile['core_technologies'])}")
        if company_profile.get("company_capabilities"):
            lines.append(f"Capabilities: {company_profile['company_capabilities']}")
        team = [m for m in (company_profile.get("team_members") or []) if m.get("name")]
        if team:
            names = ", ".join(f"{m['name']}" + (f" ({m['title']})" if m.get("title") else "") for m in team[:6])
            lines.append(f"Key personnel: {names}")
        facilities = [f for f in (company_profile.get("facilities") or []) if f.get("name")]
        if facilities:
            lines.append(f"Facilities: {', '.join(f['name'] for f in facilities[:5])}")
        past_perf = [p for p in (company_profile.get("past_performance") or []) if p.get("title")]
        if past_perf:
            summaries = "; ".join(
                f"{p['title']}" + (f" ({p['agency']})" if p.get("agency") else "") + (f" — {p['outcome']}" if p.get("outcome") else "")
                for p in past_perf[:5]
            )
            lines.append(f"Past performance: {summaries}")
        return "\n".join(lines)

    async def generate(
        self, proposal: Any, project_knowledge: Optional[Any], company_profile: Dict[str, Any],
        doc_type: str, recipient_name: Optional[str] = None, recipient_organization: Optional[str] = None,
        additional_context: Optional[str] = None,
    ) -> str:
        if doc_type not in SUPPORTING_DOCUMENT_TYPES:
            raise HTTPException(status_code=400, detail=f"Unknown supporting document type '{doc_type}'.")
        meta = SUPPORTING_DOCUMENT_TYPES[doc_type]

        recipient_line = ""
        if recipient_name or recipient_organization:
            recipient_line = f"Addressed to: {recipient_name or ''}{', ' if recipient_name and recipient_organization else ''}{recipient_organization or ''}"

        prompt = f"""
Write {meta['focus']}

Project title: {getattr(proposal, "title", "")}
Agency / grant type: {getattr(proposal, "agency", "")} / {getattr(proposal, "grant_type", "")}
Research focus: {getattr(proposal, "research_focus", "") or "Not yet specified"}
Innovation description: {getattr(proposal, "innovation_description", "") or "Not yet specified"}
Objectives: {getattr(project_knowledge, "objectives", None) or "Not yet specified"}
Need statement: {getattr(project_knowledge, "need_statement", None) or "Not yet specified"}
{self._profile_block(company_profile)}
{recipient_line}
{f"Additional context: {additional_context}" if additional_context else ""}

If information needed to write a specific, credible document is missing, write
around it generally rather than inventing specifics — do not fabricate dollar
amounts, names, dates, or commitments that weren't given above. Output plain
prose only — no markdown, no headers, no bullet characters. Do not invent a
signature block or letterhead; end with the closing paragraph.
""".strip()
        try:
            response = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are an expert grant writer drafting a supporting document for a grant proposal. Output plain prose only — no markdown, no headers, no bullet characters, no fabricated specifics."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.6,
                max_tokens=900,
            )
        except Exception as exc:
            raise _ai_error(exc)
        return response.choices[0].message.content.strip()
