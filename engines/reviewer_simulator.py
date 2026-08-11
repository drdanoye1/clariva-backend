"""
Engine 7 — Reviewer Simulation Engine
Simulates agency-specific reviewer panels and funding decisions.
Includes company profile gap analysis so reviewers surface missing information.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import openai
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from engines import usage_tracking
from engines.usage_tracking import usage_from_response
from models.schemas import ReviewerSimulation, ReviewerType


REVIEWER_PROFILES: dict[str, str] = {
    ReviewerType.NSF_PANELIST: """
You are an NSF SBIR panelist — a research professor and industry expert. You evaluate proposals
on Intellectual Merit, Broader Impacts, and Commercial Potential. You value scientific rigor,
clear hypotheses, and evidence of customer discovery. You are skeptical of vague claims and
unsubstantiated market projections.
""",
    ReviewerType.NIH_REVIEWER: """
You are an NIH SBIR study section reviewer. You evaluate on Significance, Investigator,
Innovation, Approach, and Environment (SIIIA) criteria. You prioritize translational impact,
regulatory pathway clarity, and the PI's publication record. You expect detailed preliminary data.
""",
    ReviewerType.DOD_EVALUATOR: """
You are a DoD SBIR program evaluator. You focus on dual-use potential, transition to military
programs (TRL pathway), and Phase III commercialization. You value demonstrated technical
feasibility, clear milestones, and cost realism.
""",
    ReviewerType.DARPA_PM: """
You are a DARPA Program Manager reviewing an SBIR proposal. You look for revolutionary
breakthroughs, not incremental improvements. You reward technical risk combined with
unconventional approaches. You are deeply skeptical of "safe" research plans.
""",
    ReviewerType.GENERIC: """
You are an experienced grant reviewer with expertise across federal, state, foundation,
and international funding programs. You evaluate technical merit, innovation, feasibility,
impact, and team qualifications objectively — adjusting your criteria to the specific
grant program and funder type described in the proposal context.
""",
}

REVIEW_PROMPT = """
Review the following grant proposal and provide a structured evaluation as a JSON object.

GRANT CONTEXT:
- Program: {grant_label}
- Agency / Funder: {agency}
- Grant Phase / Category: {phase}
- Research Focus: {research_focus}

IMPORTANT: Evaluate this proposal based on the criteria appropriate for "{grant_label}",
NOT as an SBIR/STTR proposal unless the program above explicitly states SBIR or STTR.
Use funder-appropriate language (e.g., "award period" not "Phase I period" for non-SBIR grants,
"program budget" not "Phase I budget", etc.).

{profile_gap_block}

Return ONLY valid JSON in this exact format:
{{
  "overall_impression": "<2-3 sentence summary>",
  "strengths": ["<strength 1>", "<strength 2>", "<strength 3>"],
  "weaknesses": ["<weakness 1>", "<weakness 2>", "<weakness 3>"],
  "questions_for_applicant": ["<question 1>", "<question 2>"],
  "decision": "<Fund|Fund with Minor Revisions|Major Revisions Required|Do Not Fund>",
  "confidence": 0.0
}}

PROPOSAL SECTIONS:
{sections_text}
"""


# ── Profile gap analysis ──────────────────────────────────────────────────────

def _build_profile_gap_block(profile: dict, phase: str) -> str:
    """
    Analyze the company profile for gaps and return a block the reviewer
    can use to surface weaknesses and questions about missing information.
    """
    if not profile:
        return ""

    gaps: List[str] = []
    strengths: List[str] = []
    is_sttr = "sttr" in phase.lower()

    # PI
    if not profile.get("pi_name"):
        gaps.append("PI name is not provided — reviewer cannot assess investigator qualifications.")
    else:
        pi_name = profile["pi_name"]
        if profile.get("pi_credentials"):
            strengths.append(f"PI {pi_name} has documented credentials.")
        else:
            gaps.append(f"PI {pi_name} is named but no credentials or background narrative was provided.")

        if profile.get("pi_degree"):
            strengths.append(f"PI holds {profile['pi_degree']}.")
        else:
            gaps.append("PI's highest degree is not specified.")

        pubs = profile.get("pi_publications")
        if pubs is not None:
            if pubs >= 10:
                strengths.append(f"PI has {pubs} peer-reviewed publications, demonstrating strong research track record.")
            elif pubs > 0:
                strengths.append(f"PI has {pubs} peer-reviewed publications.")
            else:
                gaps.append("PI has zero listed publications — this is a significant weakness for most agencies.")

        awards = profile.get("pi_prior_sbir_awards")
        if is_sttr or "sbir" in phase.lower():
            if awards and awards > 0:
                strengths.append(f"PI has {awards} prior SBIR/STTR award(s), indicating federal commercialization experience.")
            elif awards == 0:
                gaps.append("PI has no prior SBIR/STTR awards — first-time applicant risk.")

    # Team
    team = profile.get("team_members") or []
    if not team:
        gaps.append("No key personnel are listed beyond the PI — reviewer cannot assess team depth or capacity.")
    else:
        names = [m.get("name", "Unnamed") for m in team]
        strengths.append(f"Team includes {len(team)} named personnel: {', '.join(names[:4])}{'...' if len(names) > 4 else ''}.")
        low_effort = [m for m in team if (m.get("effort_pct") or 0) < 10]
        if low_effort:
            gaps.append(
                f"{len(low_effort)} team member(s) have <10% effort — reviewer may question their meaningful contribution."
            )

    # Facilities
    facilities = profile.get("facilities") or []
    if not facilities:
        gaps.append("No facilities or equipment are described — reviewer cannot assess technical execution environment.")
    else:
        fnames = [f.get("name", "unnamed") for f in facilities]
        strengths.append(f"Facilities include: {', '.join(fnames[:3])}{'...' if len(fnames) > 3 else ''}.")

    # Capabilities
    if not profile.get("company_capabilities"):
        gaps.append("No company capabilities narrative — reviewer cannot assess the organization's core competencies.")
    else:
        strengths.append("Company capabilities narrative is provided.")

    # Past performance
    past = profile.get("past_performance") or []
    if not past:
        gaps.append("No prior federal R&D performance listed — reviewer will note this as first-time applicant risk.")
    else:
        agencies = [a.get("agency", "") for a in past if a.get("agency")]
        strengths.append(f"Organization has {len(past)} prior federal award(s) from: {', '.join(set(agencies))}.")

    # STTR-specific
    if is_sttr:
        partners = profile.get("partners") or []
        ri_partners = [p for p in partners if p.get("type") == "research_institution"]
        if not ri_partners:
            gaps.append(
                "CRITICAL: No research institution partner identified. "
                "STTR requires ≥40% of effort at a university or federal lab — this proposal is non-compliant."
            )
        else:
            ri = ri_partners[0]
            effort = ri.get("effort_pct") or 0
            if effort < 40:
                gaps.append(
                    f"CRITICAL: Research institution partner '{ri.get('name', 'unnamed')}' is listed at {effort}% effort, "
                    f"but STTR requires ≥40% — this violates program rules."
                )
            else:
                strengths.append(
                    f"STTR-compliant: '{ri.get('name')}' serves as research institution partner at {effort}% effort."
                )

    # UEI / CAGE
    if not profile.get("uei_number"):
        gaps.append("UEI number not provided — SAM.gov registration must be confirmed before submission.")

    # Assemble block
    lines = []
    if strengths or gaps:
        lines.append("=== APPLICANT PROFILE INTELLIGENCE ===")
        lines.append("(Use this to inform your weaknesses and questions — reference specific gaps as a real reviewer would.)")
        lines.append("")

    if strengths:
        lines.append("Profile strengths (reference these in your strengths list where relevant):")
        for s in strengths:
            lines.append(f"  + {s}")
        lines.append("")

    if gaps:
        lines.append("Profile gaps the reviewer MUST surface (add these as weaknesses or questions):")
        for g in gaps:
            lines.append(f"  ! {g}")
        lines.append("")

    return "\n".join(lines)


class ReviewerSimulatorEngine:
    """
    Simulates funding agency reviewer decisions using agency-specific personas.
    Injects company profile gap analysis so reviewers surface specific weaknesses.
    """

    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    def _parse_json(self, text: str) -> dict:
        """Robustly parse JSON from model output regardless of formatting."""
        text = text.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass

        try:
            return json.loads("{" + text.strip().rstrip(",") + "}")
        except json.JSONDecodeError:
            pass

        print(f"[reviewer] Could not parse JSON, using defaults. Raw: {text[:300]}")
        return {
            "overall_impression": "Automated review completed. Manual review recommended.",
            "strengths": ["Technical innovation demonstrated", "Clear research focus"],
            "weaknesses": ["Additional detail needed on methodology"],
            "questions_for_applicant": ["Please clarify the validation approach."],
            "decision": "Major Revisions Required",
            "confidence": 0.5,
        }

    async def simulate(
        self,
        proposal: Any,
        sections: List[Any],
        reviewer_type: ReviewerType = ReviewerType.GENERIC,
        company_profile: Optional[Dict] = None,
        db: Optional[AsyncSession] = None, user_id: Optional[str] = None,
    ) -> ReviewerSimulation:
        """Simulate a reviewer evaluating the proposal with profile gap awareness."""

        sections_text = "\n\n".join(
            f"## {s.title}\n{(s.content or '')[:1200]}"
            for s in sections
            if s.content
        )

        phase = proposal.phase if hasattr(proposal, "phase") else "phase_i"
        grant_type = getattr(proposal, "grant_type", "") or "federal_other"
        program_label = getattr(proposal, "program_label", None) or ""
        grant_label = (
            program_label
            if program_label
            else (grant_type.upper() if grant_type in ("sbir", "sttr")
                  else grant_type.replace("_", " ").title())
        )

        profile_gap_block = _build_profile_gap_block(company_profile or {}, grant_type)

        reviewer_persona = REVIEWER_PROFILES.get(
            reviewer_type, REVIEWER_PROFILES[ReviewerType.GENERIC]
        )

        prompt = REVIEW_PROMPT.format(
            grant_label=grant_label,
            agency=proposal.agency,
            phase=phase.replace("_", " ").title(),
            research_focus=proposal.research_focus or "Not specified",
            profile_gap_block=profile_gap_block,
            sections_text=sections_text[:8000],
        )

        response = await self.client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": reviewer_persona.strip()},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.4,
        )

        content_str = response.choices[0].message.content or ""
        raw = self._parse_json(content_str)

        # Phase 3 §4.7 — Administrator-Only Engineering Economics. This
        # endpoint is unmetered (free) — no credit debit here — so
        # price_cents_charged stays 0; we still record COGS for margin math.
        if db is not None:
            prompt_tokens, completion_tokens = usage_from_response(response)
            await usage_tracking.record_usage(
                db, org_id=None, user_id=user_id, operation="reviewer:simulate",
                model=settings.OPENAI_MODEL, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                price_cents_charged=0, reference={"proposal_id": proposal.id, "reviewer_type": reviewer_type.value},
            )

        return ReviewerSimulation(
            proposal_id=proposal.id,
            reviewer_type=reviewer_type,
            overall_impression=raw.get("overall_impression", "Review completed."),
            strengths=raw.get("strengths", []),
            weaknesses=raw.get("weaknesses", []),
            questions_for_applicant=raw.get("questions_for_applicant", []),
            decision=raw.get("decision", "Major Revisions Required"),
            confidence=float(raw.get("confidence", 0.7)),
            simulated_at=datetime.utcnow(),
        )
