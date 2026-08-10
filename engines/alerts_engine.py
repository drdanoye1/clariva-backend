"""
Engine 25 — Intelligent Alerts
Funding Opportunity Intelligence, Phase 3 §4.4 ("Institutional Funding
Intelligence & Portfolio Optimization — Intelligent Alerts").

No background scheduler exists in this environment (same constraint noted
in funding_intelligence_engine.py's module docstring), so every alert type
here is evaluated at the one point a user already triggers fresh work:
POST /funding/sync ("Sync Now"), alongside the existing watchlist-match
check. `run_sync_checks()` is the single entry point routers/
funding_intelligence.py calls right after `match_watchlists()`.

Five alert types, each mapped to the cheapest correct signal already
available rather than new detection machinery:

  1. High-fit opportunity discovered   — FOARecords newly created this
     sync (no `_changed_fields` attribute at all — see _upsert_hits) that
     score STRONG PURSUE/PURSUE against the caller's Fit Score profile.
  2. Solicitation amendment changed    — FOARecords already in the
     pipeline whose _upsert_hits() diff (`_changed_fields`, set on the
     same touched-record instances this sync already produced) is
     non-empty.
  3. Deadline approaching, gaps remain — records in an active pursuit
     stage with a deadline inside DEADLINE_WARNING_DAYS and no linked
     Proposal (or one still in "draft").
  4. Resource conflict, high-priority  — reuses
     PortfolioRecommendationEngine._find_conflicts() (Phase 3.2) over just
     the subset of actively-pursued records that also score STRONG
     PURSUE/PURSUE, rather than reimplementing the clustering.
  5. Capability match to a past award  — newly created records whose
     agency or grant_type matches the agency/grant_type of an FOA that led
     to one of this scope's Awards (via Award.foa_id), reusing
     OrganizationalLearningEngine's exact `_visible_proposal_ids` scoping
     rule for "which Awards are visible here."

Every write goes through notifications.py::notify(..., dedupe=True) —
same at-most-once-per-(user, type, object) rule watchlist matches already
rely on, so re-running Sync Now never re-notifies on something already
raised.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engines.company_profile import get_org_context
from engines.fit_score_engine import score_opportunity
from engines.funding_intelligence_engine import FundingIntelligenceEngine
from engines.portfolio_recommendation_engine import (
    ACTIVE_PURSUIT_STAGES, PURSUE_RECOMMENDATIONS, PortfolioRecommendationEngine,
)
from models.db_models import Award, FOARecord, OrgProposal, Proposal
from notifications import notify

DEADLINE_WARNING_DAYS = 7


class AlertsEngine:
    def __init__(self):
        self.fi_engine = FundingIntelligenceEngine()
        self.portfolio_engine = PortfolioRecommendationEngine()

    async def run_sync_checks(
        self, db: AsyncSession, org_id: Optional[str], user_id: str,
        touched_records: List[FOARecord],
    ) -> int:
        """Called right after a sync's match_watchlists() with the exact
        same `touched_records` list (newly created + updated FOARecords
        from this sync). Returns the number of alerts actually written
        (post-dedupe). Also runs the two checks that aren't tied to
        newness (deadline-approaching, resource conflicts) over the full
        current pipeline, since those conditions can become true purely
        from time passing, not just from a fresh sync hit."""
        profile = await get_org_context(db, user_id=user_id, org_id=org_id)
        count = 0

        new_records = [r for r in touched_records if getattr(r, "_changed_fields", None) is None]
        amended_records = [r for r in touched_records if getattr(r, "_changed_fields", None)]

        count += await self._check_high_fit_discovered(db, user_id, new_records, profile)
        count += await self._check_amendments(db, user_id, amended_records)
        count += await self._check_capability_match(db, org_id, user_id, new_records)

        # Full-pipeline sweeps — not tied to what this sync happened to touch.
        records = await self.fi_engine.list_pipeline(db, org_id=org_id, uploaded_by=None if org_id else user_id)
        count += await self._check_deadlines_approaching(db, user_id, records)
        count += await self._check_resource_conflicts(db, user_id, records, profile)

        return count

    # ── 1. High-fit opportunity discovered ───────────────────────────────────

    async def _check_high_fit_discovered(
        self, db: AsyncSession, user_id: str, new_records: List[FOARecord], profile,
    ) -> int:
        if not profile or not new_records:
            return 0
        count = 0
        for r in new_records:
            fit = score_opportunity(r, profile)
            if not fit or fit.recommendation not in PURSUE_RECOMMENDATIONS:
                continue
            created = await notify(
                db, user_id, "high_fit_opportunity",
                f'High-fit opportunity discovered: "{r.program_title}" ({r.agency}) — '
                f'Fit Score {fit.overall_score}, {fit.recommendation}.',
                object_type="foa_record", object_id=r.id, dedupe=True,
            )
            if created:
                count += 1
        return count

    # ── 2. Solicitation amendment changed ────────────────────────────────────

    _FIELD_LABELS = {
        "program_title": "title", "agency": "agency", "solicitation_number": "solicitation number",
        "deadline": "deadline", "external_url": "link",
    }

    async def _check_amendments(self, db: AsyncSession, user_id: str, amended_records: List[FOARecord]) -> int:
        count = 0
        for r in amended_records:
            fields = getattr(r, "_changed_fields", [])
            labels = ", ".join(self._FIELD_LABELS.get(f, f) for f in fields)
            created = await notify(
                db, user_id, "solicitation_amended",
                f'"{r.program_title}" was updated since your last sync — {labels} changed.',
                object_type="foa_record", object_id=r.id, dedupe=True,
            )
            if created:
                count += 1
        return count

    # ── 3. Deadline approaching with incomplete requirements ────────────────

    async def _check_deadlines_approaching(
        self, db: AsyncSession, user_id: str, records: List[FOARecord],
    ) -> int:
        active = [r for r in records if r.pipeline_stage in ACTIVE_PURSUIT_STAGES and r.deadline]
        if not active:
            return 0
        now = datetime.now(timezone.utc)
        soon = [r for r in active if now <= r.deadline <= now + timedelta(days=DEADLINE_WARNING_DAYS)]
        if not soon:
            return 0

        foa_ids = [r.id for r in soon]
        proposals = (await db.execute(
            select(Proposal).where(Proposal.foa_id.in_(foa_ids))
        )).scalars().all()
        proposal_by_foa = {p.foa_id: p for p in proposals}

        count = 0
        for r in soon:
            p = proposal_by_foa.get(r.id)
            if p and p.status != "draft":
                continue  # a proposal exists and has moved past draft — not "incomplete" anymore
            days_left = (r.deadline - now).days
            gap = "no proposal started yet" if not p else "proposal still in draft"
            created = await notify(
                db, user_id, "deadline_approaching",
                f'Deadline in {days_left} day{"s" if days_left != 1 else ""} for "{r.program_title}" — {gap}.',
                object_type="foa_record", object_id=r.id, dedupe=True,
            )
            if created:
                count += 1
        return count

    # ── 4. Resource conflict between high-priority pursuits ─────────────────

    async def _check_resource_conflicts(
        self, db: AsyncSession, user_id: str, records: List[FOARecord], profile,
    ) -> int:
        if not profile:
            return 0
        high_priority = []
        for r in records:
            if r.pipeline_stage not in ACTIVE_PURSUIT_STAGES:
                continue
            fit = score_opportunity(r, profile)
            if fit and fit.recommendation in PURSUE_RECOMMENDATIONS:
                high_priority.append(r)

        conflicts = self.portfolio_engine._find_conflicts(high_priority)
        count = 0
        for c in conflicts:
            # Dedupe key: the conflict cluster itself, keyed off its first
            # (earliest-deadline) opportunity — stable across re-syncs as
            # long as that cluster's membership doesn't change.
            created = await notify(
                db, user_id, "resource_conflict",
                "Resource conflict among high-priority pursuits: " + "; ".join(c["titles"]) + ". " + c["message"],
                object_type="foa_record", object_id=c["foa_ids"][0], dedupe=True,
            )
            if created:
                count += 1
        return count

    # ── 5. New opportunity aligns with capabilities from a prior award ──────

    async def _check_capability_match(
        self, db: AsyncSession, org_id: Optional[str], user_id: str, new_records: List[FOARecord],
    ) -> int:
        if not new_records:
            return 0

        if org_id:
            proposal_ids = [row[0] for row in (await db.execute(
                select(OrgProposal.proposal_id).where(OrgProposal.org_id == org_id)
            )).all()]
        else:
            proposal_ids = [row[0] for row in (await db.execute(
                select(Proposal.id).where(Proposal.owner_id == user_id)
            )).all()]
        if not proposal_ids:
            return 0

        awards = (await db.execute(
            select(Award).where(Award.proposal_id.in_(proposal_ids), Award.foa_id.isnot(None))
        )).scalars().all()
        award_foa_ids = [a.foa_id for a in awards]
        if not award_foa_ids:
            return 0

        past_foas = (await db.execute(
            select(FOARecord).where(FOARecord.id.in_(award_foa_ids))
        )).scalars().all()
        past_agencies = {f.agency for f in past_foas if f.agency}
        past_grant_types = {f.grant_type for f in past_foas if f.grant_type}

        count = 0
        for r in new_records:
            matched_on = []
            if r.agency in past_agencies:
                matched_on.append(f"agency ({r.agency})")
            if r.grant_type in past_grant_types:
                matched_on.append(f"program type ({r.grant_type})")
            if not matched_on:
                continue
            created = await notify(
                db, user_id, "capability_match",
                f'"{r.program_title}" matches capabilities from a past award — {" and ".join(matched_on)} align.',
                object_type="foa_record", object_id=r.id, dedupe=True,
            )
            if created:
                count += 1
        return count
