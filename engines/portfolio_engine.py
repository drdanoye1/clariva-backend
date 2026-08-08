"""
Engine 20 — Portfolio Dashboard (Version 3.0 architecture upgrade, Phase 12
"Portfolio Dashboard Redesign" — docs/Clariva_Enterprise_v3_Roadmap.docx).

Rolls up every proposal and award a user can see (their own + every
organization they belong to) into the three lifecycle stages the rest of
this upgrade already established — Pre-Award, Award Received, Post-Award
(see Award.award_status's docstring in models/db_models.py and
components/LifecycleBanner.tsx on the frontend) — plus a small set of
portfolio-health numbers and executive alerts.

Design notes (same discipline as the other engines):
- Deliberately reuses AwardEngine and CreditEngine methods rather than
  duplicating their math — e.g. get_budget_status's variance_pct and
  get_planned_vs_actual's behind-schedule counts are computed exactly once,
  in AwardEngine, and this engine just calls them per award. This is the
  same "engines interoperate through shared models/each other's read-only
  methods" pattern already used across Phase 5-10 (see award_engine.py's
  module docstring for the one place this project draws a firmer line —
  mutating another engine's rows from here would break that precedent;
  this engine only reads).
- No permission checks live here — like every "list mine" endpoint
  (awardsApi.mine, funding_intelligence's pipeline), visibility is already
  scoped to owned + org-shared records by the caller passing in org_ids.
- Best-effort per-award: get_planned_vs_actual raises 400 for an award
  with no baseline yet (e.g. still in Award Received) — that's expected,
  not an error, so it's caught and simply excluded from the
  schedule-variance tally rather than failing the whole summary.
"""
from __future__ import annotations

from typing import List

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engines.award_engine import AwardEngine
from engines.credit_engine import LOW_BALANCE_WARNING_PCT, CreditEngine
from models.db_models import OrgProposal, Proposal
from models.schemas import PortfolioAlertOut, PortfolioSummaryOut

BUDGET_OVERSPEND_THRESHOLD_PCT = 10.0  # matches awards.tsx's existing "overspending" flag


class PortfolioEngine:
    def __init__(self):
        self.award_engine = AwardEngine()
        self.credit_engine = CreditEngine()

    async def _visible_proposal_ids(self, db: AsyncSession, user_id: str, org_ids: List[str]) -> set:
        owned = await db.execute(select(Proposal.id).where(Proposal.owner_id == user_id))
        ids = {row[0] for row in owned.all()}
        if org_ids:
            shared = await db.execute(select(OrgProposal.proposal_id).where(OrgProposal.org_id.in_(org_ids)))
            ids.update(row[0] for row in shared.all())
        return ids

    async def get_summary(self, db: AsyncSession, user_id: str, org_ids: List[str]) -> PortfolioSummaryOut:
        proposal_ids = await self._visible_proposal_ids(db, user_id, org_ids)
        awards = await self.award_engine.list_awards_for_user(db, user_id, org_ids)
        award_proposal_ids = {a.proposal_id for a in awards}

        pre_award_count = len(proposal_ids - award_proposal_ids)
        received_awards = [a for a in awards if a.award_status == "received"]
        post_awards = [a for a in awards if a.award_status in ("active", "closed")]
        active_awards = [a for a in awards if a.award_status == "active"]

        total_active_award_value = sum(a.total_award_value or 0.0 for a in active_awards)

        alerts: List[PortfolioAlertOut] = []

        # Award Received — sponsor conditions still open, blocking activation.
        open_sponsor_conditions = 0
        for award in received_awards:
            conditions = await self.award_engine.list_conditions(db, award.id)
            open_count = sum(1 for c in conditions if c.status == "open")
            open_sponsor_conditions += open_count
            if open_count:
                alerts.append(PortfolioAlertOut(
                    severity="warning",
                    message=f"{open_count} sponsor condition{'s' if open_count != 1 else ''} still open on award {award.award_number or award.id[:8]} — resolve before activating.",
                    proposal_id=award.proposal_id, award_id=award.id,
                ))

        # Post-Award (active only) — budget variance and schedule variance,
        # read from the exact same engine methods the Award workspace uses.
        awards_over_budget = 0
        milestones_behind_schedule = 0
        deliverables_behind_schedule = 0
        for award in active_awards:
            budget_status = await self.award_engine.get_budget_status(db, award.id)
            if budget_status.variance_pct is not None and budget_status.variance_pct > BUDGET_OVERSPEND_THRESHOLD_PCT:
                awards_over_budget += 1
                alerts.append(PortfolioAlertOut(
                    severity="warning",
                    message=f"Award {award.award_number or award.id[:8]} is overspending its pace by {budget_status.variance_pct:+.1f}%.",
                    proposal_id=award.proposal_id, award_id=award.id,
                ))

            try:
                pva = await self.award_engine.get_planned_vs_actual(db, award.id)
            except HTTPException:
                continue  # no baseline yet — shouldn't happen for an active award, but degrade gracefully
            milestones_behind_schedule += pva.milestones_behind_schedule
            deliverables_behind_schedule += pva.deliverables_behind_schedule
            behind_total = pva.milestones_behind_schedule + pva.deliverables_behind_schedule
            if behind_total:
                alerts.append(PortfolioAlertOut(
                    severity="warning",
                    message=f"{behind_total} milestone/deliverable{'s' if behind_total != 1 else ''} behind schedule on award {award.award_number or award.id[:8]}.",
                    proposal_id=award.proposal_id, award_id=award.id,
                ))

        # Low AI credit balance — one alert per org this user belongs to
        # that's crossed the same 20% threshold the org page's banner uses.
        for org_id in org_ids:
            ledger = await self.credit_engine.get_or_create_ledger(db, org_id)
            if ledger.reference_balance and (ledger.balance / ledger.reference_balance) < LOW_BALANCE_WARNING_PCT:
                alerts.append(PortfolioAlertOut(
                    severity="info",
                    message="An organization's AI credit balance is below 20% — consider topping up.",
                ))

        return PortfolioSummaryOut(
            pre_award_count=pre_award_count,
            award_received_count=len(received_awards),
            post_award_count=len(post_awards),
            total_active_award_value=total_active_award_value,
            open_sponsor_conditions=open_sponsor_conditions,
            awards_over_budget=awards_over_budget,
            milestones_behind_schedule=milestones_behind_schedule,
            deliverables_behind_schedule=deliverables_behind_schedule,
            alerts=alerts,
        )
