"""
Engine 10 — Organizational Memory & KPI System
Stores proposals, outcomes, and enables semantic retrieval.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import openai

from config import settings
from models.schemas import KPIDashboard


class MemoryEngine:
    """
    Organizational memory system providing:
    - KPI tracking and dashboard
    - Semantic search over past proposals
    - Lesson distillation and reuse
    """

    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    # ── KPI Dashboard ─────────────────────────────────────────────────────────

    def build_kpi(self, org_id: str, records: List[Any]) -> KPIDashboard:
        """Compute KPI metrics from memory records."""
        total = len(records)
        funded = sum(1 for r in records if r.outcome == "funded")
        success_rate = (funded / total * 100) if total > 0 else 0.0

        scores = [r.score for r in records if r.score is not None]
        avg_score = sum(scores) / len(scores) if scores else 0.0

        # Proposals by agency
        agency_counts: Dict[str, int] = {}
        for r in records:
            agency_counts[r.agency] = agency_counts.get(r.agency, 0) + 1

        # Score trend (by creation date)
        trend = []
        for r in sorted(records, key=lambda x: x.created_at):
            if r.score is not None:
                trend.append({
                    "date": r.created_at.isoformat(),
                    "score": r.score,
                    "agency": r.agency,
                    "outcome": r.outcome,
                })

        # Top weaknesses (from lessons learned)
        weakness_counts: Dict[str, int] = {}
        for r in records:
            for lesson in (r.lessons_learned or []):
                weakness_counts[lesson] = weakness_counts.get(lesson, 0) + 1
        top_weaknesses = sorted(weakness_counts, key=lambda k: -weakness_counts[k])[:5]

        return KPIDashboard(
            org_id=org_id,
            total_proposals=total,
            funded=funded,
            success_rate=round(success_rate, 1),
            avg_score=round(avg_score, 2),
            proposals_by_agency=agency_counts,
            score_trend=trend,
            top_weaknesses=top_weaknesses,
        )

    # ── Semantic Search ───────────────────────────────────────────────────────

    async def semantic_search(
        self,
        query: str,
        org_id: str,
        db: Any,
        limit: int = 5,
    ) -> List[Dict]:
        """
        Find past proposals similar to query using embedding similarity.
        Falls back to keyword search if pgvector not configured.
        """
        try:
            embedding = await self._embed(query)
            return await self._vector_search(embedding, org_id, db, limit)
        except Exception:
            return await self._keyword_search(query, org_id, db, limit)

    async def _embed(self, text: str) -> List[float]:
        """Generate embedding using OpenAI API."""
        response = await self.client.embeddings.create(
            model=settings.OPENAI_EMBEDDING_MODEL,
            input=text[:8000],
        )
        return response.data[0].embedding

    async def _vector_search(
        self, embedding: List[float], org_id: str, db: Any, limit: int
    ) -> List[Dict]:
        """pgvector cosine similarity search."""
        from sqlalchemy import text
        from models.db_models import MemoryRecord, Proposal

        # Requires pgvector extension and vector column
        query = text("""
            SELECT mr.id, mr.proposal_id, mr.agency, mr.outcome, mr.score,
                   p.title, p.research_focus,
                   1 - (mr.embedding <=> :embedding) AS similarity
            FROM memory_records mr
            JOIN proposals p ON p.id = mr.proposal_id
            WHERE mr.org_id = :org_id
              AND mr.embedding IS NOT NULL
            ORDER BY similarity DESC
            LIMIT :limit
        """)
        result = await db.execute(query, {
            "embedding": json.dumps(embedding),
            "org_id": org_id,
            "limit": limit,
        })
        return [dict(row._mapping) for row in result]

    async def _keyword_search(
        self, query: str, org_id: str, db: Any, limit: int
    ) -> List[Dict]:
        """Simple keyword fallback search."""
        from sqlalchemy import select, or_
        from models.db_models import MemoryRecord, Proposal

        keywords = query.lower().split()[:5]
        result = await db.execute(
            select(Proposal.id, Proposal.title, Proposal.research_focus,
                   Proposal.agency, Proposal.phase)
            .where(Proposal.owner_id == org_id)
            .order_by(Proposal.created_at.desc())
            .limit(limit)
        )
        rows = result.all()
        return [
            {
                "proposal_id": r.id,
                "title": r.title,
                "agency": r.agency,
                "phase": r.phase,
                "research_focus": r.research_focus,
                "similarity": None,
            }
            for r in rows
        ]

    # ── Lesson Distillation ───────────────────────────────────────────────────

    async def distill_lessons(
        self, proposal_content: str, outcome: str, score: Optional[float]
    ) -> List[str]:
        """Extract lessons learned from a proposal outcome."""
        prompt = f"""
A SBIR proposal with score {score}/100 was {outcome}.

Proposal summary (first 2000 chars):
{proposal_content[:2000]}

Extract 3-5 specific, actionable lessons learned for future proposals.
Return as a JSON array of strings.
"""
        response = await self.client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "Extract concise lessons learned. Return JSON array only."},
                {"role": "user",   "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        raw = json.loads(response.choices[0].message.content)
        if isinstance(raw, list):
            return raw
        return raw.get("lessons", [])
