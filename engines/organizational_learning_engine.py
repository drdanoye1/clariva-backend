"""
Engine 22 — Organizational Learning (Version 3.0 architecture upgrade,
Phase 3.1 — "Institutional Funding Intelligence & Portfolio Optimization",
docs/Clariva Funding Opportunity Intelligence product definition upgrade
for monetization_1.docx §4.1).

Deliberately NOT a new event-sourcing system: every event type below is
read from a table that already exists for its own reason (PipelineStageEvent
from Phase 4's pipeline-stage history, ProposalStatusEvent from this same
Phase 3.1 batch, MemoryRecord from the original win/loss memory system,
Award/AwardCloseout from Phase 5). This engine's only job is to read all of
them, scoped consistently, and merge them into one chronological feed a
user can actually look at — "what has this organization learned from
pursuing funding" — without duplicating any of that underlying data.

Scoping mirrors FundingIntelligenceEngine.list_pipeline exactly (single
scope at a time: an org's shared activity via `org_id`, or the caller's own
personal activity via `uploaded_by`), matching the same "Personal / one
Organization" selector already used on the Funding Pipeline and Company
Profile pages — not PortfolioEngine's "everything visible across every org
I belong to" model, since a Team wants its own org's learning history, not
blended with a member's personal one.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import (
    Award, AwardCloseout, FOARecord, MemoryRecord, OrgProposal,
    PipelineStageEvent, Proposal, ProposalStatusEvent,
)

_STAGE_LABELS = {
    "identified": "Identified",
    "qualifying": "Moved to Qualifying",
    "pursuing": "Moved to Pursuing",
    "submitted": "Submitted",
    "awarded": "Awarded",
    "declined": "Declined",
    "no_go": "No-Go decision",
}


class OrganizationalLearningEngine:
    async def _visible_proposal_ids(
        self, db: AsyncSession, org_id: Optional[str], uploaded_by: Optional[str],
    ) -> List[str]:
        """Same single-scope rule as FOARecord's list_pipeline: an org's
        shared proposals (via OrgProposal), or the caller's own — never
        both at once."""
        if org_id:
            result = await db.execute(select(OrgProposal.proposal_id).where(OrgProposal.org_id == org_id))
        elif uploaded_by:
            result = await db.execute(select(Proposal.id).where(Proposal.owner_id == uploaded_by))
        else:
            return []
        return [row[0] for row in result.all()]

    async def get_timeline(
        self, db: AsyncSession, org_id: Optional[str] = None, uploaded_by: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []

        # ── FOA-scoped events: discovery + every pipeline-stage transition ──
        foa_query = select(FOARecord)
        if org_id:
            foa_query = foa_query.where(FOARecord.org_id == org_id)
        elif uploaded_by:
            foa_query = foa_query.where(FOARecord.org_id.is_(None), FOARecord.uploaded_by == uploaded_by)
        else:
            foa_query = foa_query.where(FOARecord.id.is_(None))  # neither scope given — nothing visible
        foa_records = list((await db.execute(foa_query)).scalars().all())
        foa_by_id = {r.id: r for r in foa_records}
        foa_ids = list(foa_by_id.keys())

        for r in foa_records:
            events.append({
                "event_type": "opportunity_discovered",
                "occurred_at": r.created_at,
                "title": f"Discovered: {r.program_title}",
                "description": f"{r.agency} · via {r.source}",
                "foa_id": r.id,
            })

        if foa_ids:
            stage_events = (await db.execute(
                select(PipelineStageEvent).where(PipelineStageEvent.foa_id.in_(foa_ids))
            )).scalars().all()
            for ev in stage_events:
                foa = foa_by_id.get(ev.foa_id)
                label = _STAGE_LABELS.get(ev.to_stage, ev.to_stage.replace("_", " ").title())
                events.append({
                    "event_type": "stage_change",
                    "occurred_at": ev.created_at,
                    "title": f"{label}: {foa.program_title if foa else ev.foa_id}",
                    "description": f"{ev.from_stage or '(new)'} → {ev.to_stage}" + (f" — {ev.notes}" if ev.notes else ""),
                    "foa_id": ev.foa_id,
                })

        # ── Proposal-scoped events: status history, outcomes, awards ────────
        proposal_ids = await self._visible_proposal_ids(db, org_id, uploaded_by)
        if proposal_ids:
            proposals = {
                p.id: p for p in (await db.execute(
                    select(Proposal).where(Proposal.id.in_(proposal_ids))
                )).scalars().all()
            }

            status_events = (await db.execute(
                select(ProposalStatusEvent).where(ProposalStatusEvent.proposal_id.in_(proposal_ids))
            )).scalars().all()
            for ev in status_events:
                p = proposals.get(ev.proposal_id)
                events.append({
                    "event_type": "proposal_status_change",
                    "occurred_at": ev.created_at,
                    "title": f"Proposal {ev.to_status}: {p.title if p else ev.proposal_id}",
                    "description": f"{ev.from_status or '(new)'} → {ev.to_status}",
                    "proposal_id": ev.proposal_id,
                })

            memory_records = (await db.execute(
                select(MemoryRecord).where(MemoryRecord.proposal_id.in_(proposal_ids))
            )).scalars().all()
            for m in memory_records:
                p = proposals.get(m.proposal_id)
                label = {"funded": "Funded", "not_funded": "Not funded"}.get(m.outcome, "Outcome recorded")
                lessons = "; ".join(m.lessons_learned) if m.lessons_learned else None
                events.append({
                    "event_type": "outcome_recorded",
                    "occurred_at": m.created_at,
                    "title": f"{label}: {p.title if p else m.proposal_id}",
                    "description": lessons or f"Agency: {m.agency}",
                    "proposal_id": m.proposal_id,
                })

            awards = list((await db.execute(
                select(Award).where(Award.proposal_id.in_(proposal_ids))
            )).scalars().all())
            award_by_id = {a.id: a for a in awards}
            for a in awards:
                p = proposals.get(a.proposal_id)
                value = f" · ${a.total_award_value:,.0f}" if a.total_award_value else ""
                events.append({
                    "event_type": "award_created",
                    "occurred_at": a.created_at,
                    "title": f"Award received: {p.title if p else a.proposal_id}",
                    "description": f"{a.funding_agency}{value}",
                    "proposal_id": a.proposal_id,
                    "award_id": a.id,
                })

            award_ids = list(award_by_id.keys())
            if award_ids:
                closeouts = (await db.execute(
                    select(AwardCloseout).where(
                        AwardCloseout.award_id.in_(award_ids), AwardCloseout.closed_at.isnot(None)
                    )
                )).scalars().all()
                for c in closeouts:
                    a = award_by_id.get(c.award_id)
                    p = proposals.get(a.proposal_id) if a else None
                    events.append({
                        "event_type": "award_closed",
                        "occurred_at": c.closed_at,
                        "title": f"Closed out: {p.title if p else (a.proposal_id if a else c.award_id)}",
                        "description": "Final report submitted" if c.final_report_submitted else "Closeout recorded",
                        "proposal_id": a.proposal_id if a else None,
                        "award_id": c.award_id,
                    })

        events = [e for e in events if e.get("occurred_at") is not None]
        events.sort(key=lambda e: e["occurred_at"], reverse=True)
        return events[:limit]
