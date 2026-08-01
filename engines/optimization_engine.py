"""
Engine 8 — Optimization Engine
Iterative proposal refinement based on scoring and reviewer feedback.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import openai

from config import settings


OPTIMIZE_PROMPT = """
You are an expert SBIR proposal consultant. Improve the following proposal section based on
reviewer feedback and scoring analysis.

Section: {section_title}
Agency: {agency}
Current Score: {current_score}/10

Reviewer Weaknesses Identified:
{weaknesses}

Scoring Recommendations:
{recommendations}

Current Content:
{current_content}

Rewrite this section to:
1. Directly address each identified weakness
2. Implement each recommendation
3. Maintain technical accuracy
4. Strengthen the argument for funding

Return ONLY the improved section content (no headers, no meta-commentary).
"""


class OptimizationEngine:
    """
    Improves weak proposal sections using iterative LLM-driven refinement.
    Targets sections below a score threshold and applies reviewer feedback.
    """

    SCORE_THRESHOLD = 7.0  # Sections below this are targeted for optimization

    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    async def optimize_proposal(
        self,
        proposal: Any,
        sections: List[Any],
        scoring_result: Any,
        reviewer_result: Optional[Any] = None,
        max_sections: int = 3,
    ) -> Dict[str, str]:
        """
        Optimize the weakest sections of a proposal.
        Returns a dict of {section_id: improved_content}.
        """
        # Find sections that need improvement
        score_map: Dict[str, Any] = {
            s.section_id: s for s in scoring_result.section_scores
        }
        section_map: Dict[str, Any] = {s.section_id: s for s in sections}

        # Sort by score ascending — worst first
        weak_sections = sorted(
            [s for s in scoring_result.section_scores if s.score < self.SCORE_THRESHOLD],
            key=lambda x: x.score,
        )[:max_sections]

        results: Dict[str, str] = {}

        for sec_score in weak_sections:
            section = section_map.get(sec_score.section_id)
            if not section or not section.content:
                continue

            # Collect reviewer weaknesses for this section
            reviewer_weaknesses = []
            if reviewer_result:
                reviewer_weaknesses = reviewer_result.weaknesses[:3]

            improved = await self._optimize_section(
                section_title=sec_score.section_title,
                agency=proposal.agency,
                current_score=sec_score.score,
                current_content=section.content,
                weaknesses=sec_score.weaknesses + reviewer_weaknesses,
                recommendations=sec_score.recommendations,
            )
            results[sec_score.section_id] = improved

        return results

    async def _optimize_section(
        self,
        section_title: str,
        agency: str,
        current_score: float,
        current_content: str,
        weaknesses: List[str],
        recommendations: List[str],
    ) -> str:
        """Run one optimization pass on a section."""
        prompt = OPTIMIZE_PROMPT.format(
            section_title=section_title,
            agency=agency,
            current_score=current_score,
            weaknesses="\n".join(f"- {w}" for w in weaknesses) or "None identified",
            recommendations="\n".join(f"- {r}" for r in recommendations) or "None provided",
            current_content=current_content[:3000],
        )

        response = await self.client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert SBIR proposal editor. "
                        "Improve proposals to maximize funding scores."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            max_tokens=2000,
        )

        return response.choices[0].message.content.strip()

    async def suggest_improvements(
        self, proposal: Any, scoring_result: Any
    ) -> List[Dict[str, Any]]:
        """Return prioritized improvement suggestions without rewriting."""
        suggestions = []
        for sec in sorted(scoring_result.section_scores, key=lambda x: x.score):
            if sec.recommendations:
                suggestions.append({
                    "section_id": sec.section_id,
                    "section_title": sec.section_title,
                    "current_score": sec.score,
                    "priority": "high" if sec.score < 6.0 else "medium",
                    "recommendations": sec.recommendations,
                })
        return suggestions
