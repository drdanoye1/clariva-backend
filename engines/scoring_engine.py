"""
Engine 6 — Scoring Engine (Mathematical)
Formula: Score = Σ(Wi × Si) − Risk Penalties
Dimensions: technical_merit, commercialization, innovation, team, compliance
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import openai
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from engines import usage_tracking
from engines.usage_tracking import usage_from_response
from models.schemas import RiskPenalty, SectionScore, ScoringResult


SCORE_SYSTEM_PROMPT = """
You are a senior federal grants reviewer with deep expertise in evaluating SBIR proposals.

Evaluate each section of this proposal and return a JSON scoring object.
Be rigorous, objective, and specific. Use the full 0-10 scale.

Return ONLY valid JSON in this exact format:
{
  "section_scores": [
    {
      "section_id": "<id>",
      "section_title": "<title>",
      "score": <0-10 float>,
      "strengths": ["<strength 1>", "..."],
      "weaknesses": ["<weakness 1>", "..."],
      "recommendations": ["<recommendation 1>", "..."]
    }
  ],
  "risk_penalties": [
    {"category": "<category>", "description": "<detail>", "penalty": <float>}
  ],
  "dimension_scores": {
    "technical_merit": <0-10>,
    "commercialization": <0-10>,
    "innovation": <0-10>,
    "team": <0-10>,
    "compliance": <0-10>
  }
}
"""


class ScoringEngine:
    """
    Mathematical scoring engine.
    Score = Σ(Wi × Si) − Risk Penalties, normalized to 0–100.
    """

    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    async def score(
        self, proposal: Any, sections: List[Any],
        db: Optional[AsyncSession] = None, user_id: Optional[str] = None,
    ) -> ScoringResult:
        """Score a proposal across all dimensions."""

        # Build section summaries for the LLM
        section_texts = "\n\n".join(
            f"[{s.section_id.upper()}] {s.title}\n{(s.content or '')[:1500]}"
            for s in sections
            if s.content
        )

        user_prompt = f"""
Evaluate this SBIR {proposal.phase} proposal to {proposal.agency}:

Research Focus: {proposal.research_focus}
Innovation: {proposal.innovation_description}

--- PROPOSAL SECTIONS ---
{section_texts}
---

Score each section and provide overall dimension scores.
"""

        response = await self.client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SCORE_SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )

        # Phase 3 §4.7 — Administrator-Only Engineering Economics. This
        # endpoint is unmetered (free) — no credit debit here — so
        # price_cents_charged stays 0; we still record COGS for margin math.
        if db is not None:
            prompt_tokens, completion_tokens = usage_from_response(response)
            await usage_tracking.record_usage(
                db, org_id=None, user_id=user_id, operation="scoring:score",
                model=settings.OPENAI_MODEL, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                price_cents_charged=0, reference={"proposal_id": proposal.id},
            )

        raw = json.loads(response.choices[0].message.content)
        return self._build_result(proposal.id, raw, sections)

    def _build_result(
        self, proposal_id: str, raw: Dict, sections: List[Any]
    ) -> ScoringResult:
        # Build section weight map
        weights = {s.section_id: getattr(s, "evaluation_weight", 0.1) for s in sections}
        total_weight = sum(weights.values()) or 1.0

        section_scores: List[SectionScore] = []
        weighted_total = 0.0

        for sec_data in raw.get("section_scores", []):
            section_id = sec_data.get("section_id", "unknown")
            score = float(sec_data.get("score", 5.0))
            weight = weights.get(section_id, 1.0 / len(sections) if sections else 0.1)
            normalized_weight = weight / total_weight
            weighted_score = normalized_weight * score

            section_scores.append(SectionScore(
                section_id=section_id,
                section_title=sec_data.get("section_title", section_id),
                score=round(score, 2),
                weight=round(normalized_weight, 3),
                weighted_score=round(weighted_score, 3),
                strengths=sec_data.get("strengths", []),
                weaknesses=sec_data.get("weaknesses", []),
                recommendations=sec_data.get("recommendations", []),
            ))
            weighted_total += weighted_score

        # Apply risk penalties
        risk_penalties: List[RiskPenalty] = [
            RiskPenalty(**rp) for rp in raw.get("risk_penalties", [])
        ]
        total_penalty = sum(rp.penalty for rp in risk_penalties)

        # Normalize to 0–100
        raw_score = weighted_total * 10  # section scores 0-10, weighted sum ~0-10
        final_score = max(0.0, min(100.0, raw_score - total_penalty))

        # Dimension scores
        dims = raw.get("dimension_scores", {})

        return ScoringResult(
            proposal_id=proposal_id,
            total_score=round(final_score, 2),
            section_scores=section_scores,
            risk_penalties=risk_penalties,
            compliance_score=round(dims.get("compliance", 7.0), 2),
            technical_merit=round(dims.get("technical_merit", 7.0), 2),
            commercialization=round(dims.get("commercialization", 7.0), 2),
            innovation=round(dims.get("innovation", 7.0), 2),
            team_score=round(dims.get("team", 7.0), 2),
            scored_at=datetime.utcnow(),
        )
