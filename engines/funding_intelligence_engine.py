"""
Engine 15 — Funding Intelligence Engine
Continuous opportunity monitoring (Grants.gov/SAM.gov), watchlists, and the
pre-award pipeline (Clariva Enterprise™ PRD §15), built on top of the
existing FOARecord opportunity record from foa.py/Phase 0.

Design notes (same discipline as engines/collaboration_engine.py and
engines/document_library_engine.py):
- Every method takes an already-open AsyncSession and only flushes, never
  commits — the caller's request-scoped session commits once, after the
  router handler returns.
- No permission checks live here — routers/foa.py and
  routers/funding_intelligence.py gate access via rbac.py's
  ROLE_PERMISSIONS ("manage_pipeline", "manage_watchlists") for org-shared
  records, and the existing uploaded_by-ownership check for personal ones.
- Every create/update path that returns a row for serialization calls
  `await db.refresh(obj)` after `await db.flush()` — same MissingGreenlet
  precaution documented since credit_engine.py.

Grants.gov integration is real, not stubbed: `search2`/`fetchOpportunity`
are public endpoints that need no API key (confirmed against
https://www.grants.gov/api/api-guide as of this writing). SAM.gov's
opportunity API does need a key; `sync_sam_gov()` reports
`configured=False` and no-ops when `settings.SAM_GOV_API_KEY` is blank,
the same "degrade gracefully when unconfigured" pattern already used for
document_library_engine.py's OpenAI-dependent semantic search. Both sync
paths are triggered manually (POST /funding/sync) — there is no background
scheduler in this environment, same reasoning as Phase 3's manual
archive-expired action.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import FOARecord, Notification, PipelineStageEvent, Watchlist, new_uuid

GRANTS_GOV_SEARCH_URL = "https://api.grants.gov/v1/api/search2"
GRANTS_GOV_FETCH_URL = "https://api.grants.gov/v1/api/fetchOpportunity"
SAM_GOV_OPPORTUNITIES_URL = "https://api.sam.gov/opportunities/v2/search"

VALID_STAGES: Tuple[str, ...] = (
    "identified", "qualifying", "pursuing", "submitted", "awarded", "declined", "no_go",
)
TERMINAL_STAGES: Tuple[str, ...] = ("awarded", "declined", "no_go")
VALID_BID_DECISIONS: Tuple[str, ...] = ("bid", "no_go", "undecided")


def _parse_mmddyyyy(value: Optional[str]) -> Optional[datetime]:
    """Grants.gov dates come back as 'MM/DD/YYYY' strings, or '' if unset."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%m/%d/%Y")
    except ValueError:
        return None


class FundingIntelligenceEngine:
    # ── Grants.gov ────────────────────────────────────────────────────────────

    async def fetch_grants_gov(
        self, keyword: Optional[str] = None, agencies: Optional[List[str]] = None,
        funding_categories: Optional[List[str]] = None, opp_statuses: str = "forecasted|posted",
        rows: int = 100,
    ) -> List[Dict[str, Any]]:
        """Raw call to Grants.gov's public search2 API. Raises on network/HTTP
        error — callers (sync_grants_gov) decide how to surface that."""
        payload: Dict[str, Any] = {"rows": rows, "oppStatuses": opp_statuses}
        if keyword:
            payload["keyword"] = keyword
        if agencies:
            payload["agencies"] = ",".join(agencies)
        if funding_categories:
            payload["fundingCategories"] = ",".join(funding_categories)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(GRANTS_GOV_SEARCH_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
        return (data.get("data") or {}).get("oppHits") or []

    async def fetch_grants_gov_detail(self, opportunity_id: str) -> Dict[str, Any]:
        """Raw call to Grants.gov's public fetchOpportunity API — full
        synopsis detail (description, award floor/ceiling, applicant
        types) for one opportunity, used by the "enrich" action to
        AI-parse a lightweight synced record into a full template."""
        try:
            payload_id: Any = int(opportunity_id)
        except (TypeError, ValueError):
            payload_id = opportunity_id
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(GRANTS_GOV_FETCH_URL, json={"opportunityId": payload_id})
            resp.raise_for_status()
            body = resp.json()
        return body.get("data") or {}

    def _map_grants_gov_hit(self, hit: Dict[str, Any]) -> Dict[str, Any]:
        opp_id = hit.get("id")
        return {
            "external_id": hit.get("number") or str(opp_id) if opp_id else None,
            "program_title": hit.get("title") or "Untitled Opportunity",
            "agency": (hit.get("agencyCode") or "OTHER")[:20],
            "solicitation_number": hit.get("number"),
            "deadline": _parse_mmddyyyy(hit.get("closeDate")),
            "external_url": f"https://www.grants.gov/search-results-detail/{opp_id}" if opp_id else None,
        }

    async def sync_grants_gov(
        self, db: AsyncSession, org_id: Optional[str], user_id: str,
        keyword: Optional[str] = None, agencies: Optional[List[str]] = None,
        funding_categories: Optional[List[str]] = None, rows: int = 100,
    ) -> Tuple[int, int, List[FOARecord]]:
        """Fetch + upsert-by-(source, external_id). Returns (created, updated, touched_records)."""
        hits = await self.fetch_grants_gov(
            keyword=keyword, agencies=agencies, funding_categories=funding_categories, rows=rows,
        )
        created, updated, touched = await self._upsert_hits(db, org_id, user_id, "grants_gov", hits, self._map_grants_gov_hit)
        return created, updated, touched

    # ── SAM.gov (config-gated) ───────────────────────────────────────────────

    async def fetch_sam_gov(self, keyword: Optional[str] = None, rows: int = 100) -> List[Dict[str, Any]]:
        from config import settings
        params: Dict[str, Any] = {"api_key": settings.SAM_GOV_API_KEY, "limit": rows}
        if keyword:
            params["title"] = keyword
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(SAM_GOV_OPPORTUNITIES_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        return data.get("opportunitiesData") or []

    def _map_sam_gov_hit(self, hit: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "external_id": hit.get("noticeId") or hit.get("solicitationNumber"),
            "program_title": hit.get("title") or "Untitled Opportunity",
            "agency": (hit.get("fullParentPathName") or "OTHER").split(".")[0][:20],
            "solicitation_number": hit.get("solicitationNumber"),
            "deadline": _parse_sam_date(hit.get("responseDeadLine")),
            "external_url": hit.get("uiLink"),
        }

    async def sync_sam_gov(
        self, db: AsyncSession, org_id: Optional[str], user_id: str,
        keyword: Optional[str] = None, rows: int = 100,
    ) -> Tuple[bool, int, int, List[FOARecord]]:
        """Returns (configured, created, updated, touched_records). configured=False
        (with zero counts) when no SAM_GOV_API_KEY is set — the caller reports
        that as a clean "not configured" result rather than an error."""
        from config import settings
        if not settings.SAM_GOV_API_KEY:
            return False, 0, 0, []

        hits = await self.fetch_sam_gov(keyword=keyword, rows=rows)
        created, updated, touched = await self._upsert_hits(db, org_id, user_id, "sam_gov", hits, self._map_sam_gov_hit)
        return True, created, updated, touched

    # ── Shared upsert-by-external-id logic ──────────────────────────────────

    async def _get_by_external(self, db: AsyncSession, source: str, external_id: str) -> Optional[FOARecord]:
        result = await db.execute(
            select(FOARecord).where(FOARecord.source == source, FOARecord.external_id == external_id)
        )
        return result.scalar_one_or_none()

    async def _upsert_hits(
        self, db: AsyncSession, org_id: Optional[str], user_id: str, source: str,
        hits: List[Dict[str, Any]], mapper,
    ) -> Tuple[int, int, List[FOARecord]]:
        created = 0
        updated = 0
        touched: List[FOARecord] = []
        now = datetime.utcnow()

        for hit in hits:
            mapped = mapper(hit)
            external_id = mapped.get("external_id")
            if not external_id:
                continue  # can't dedupe without a stable external key — skip rather than risk duplicates

            existing = await self._get_by_external(db, source, external_id)
            if existing:
                changed = False
                for field in ("program_title", "agency", "solicitation_number", "deadline", "external_url"):
                    if getattr(existing, field) != mapped.get(field):
                        setattr(existing, field, mapped.get(field))
                        changed = True
                existing.last_synced_at = now
                if changed:
                    updated += 1
                touched.append(existing)
            else:
                record = FOARecord(
                    id=new_uuid(), org_id=org_id, uploaded_by=user_id, source=source,
                    phase="n/a", grant_type="federal_other", pipeline_stage="identified",
                    last_synced_at=now, **mapped,
                )
                db.add(record)
                created += 1
                touched.append(record)

        await db.flush()
        for r in touched:
            await db.refresh(r)
        return created, updated, touched

    # ── Watchlists ───────────────────────────────────────────────────────────

    async def create_watchlist(self, db: AsyncSession, org_id: Optional[str], owner_id: str, data: Dict[str, Any]) -> Watchlist:
        wl = Watchlist(
            id=new_uuid(), org_id=org_id, owner_id=owner_id, name=data["name"],
            keyword=data.get("keyword"), agencies=data.get("agencies"),
            funding_categories=data.get("funding_categories"),
            min_award=data.get("min_award"), max_award=data.get("max_award"),
        )
        db.add(wl)
        await db.flush()
        await db.refresh(wl)
        return wl

    async def list_watchlists(self, db: AsyncSession, org_id: Optional[str], owner_id: str) -> List[Watchlist]:
        if org_id:
            query = select(Watchlist).where(Watchlist.org_id == org_id)
        else:
            query = select(Watchlist).where(Watchlist.org_id.is_(None), Watchlist.owner_id == owner_id)
        result = await db.execute(query.order_by(Watchlist.created_at.desc()))
        return list(result.scalars().all())

    async def get_watchlist_or_404(self, db: AsyncSession, watchlist_id: str) -> Watchlist:
        result = await db.execute(select(Watchlist).where(Watchlist.id == watchlist_id))
        wl = result.scalar_one_or_none()
        if not wl:
            raise HTTPException(status_code=404, detail="Watchlist not found")
        return wl

    async def update_watchlist(self, db: AsyncSession, watchlist_id: str, data: Dict[str, Any]) -> Watchlist:
        wl = await self.get_watchlist_or_404(db, watchlist_id)
        for field in ("name", "keyword", "agencies", "funding_categories", "min_award", "max_award", "active"):
            if field in data and data[field] is not None:
                setattr(wl, field, data[field])
        await db.flush()
        await db.refresh(wl)
        return wl

    async def delete_watchlist(self, db: AsyncSession, watchlist_id: str) -> None:
        wl = await self.get_watchlist_or_404(db, watchlist_id)
        await db.delete(wl)
        await db.flush()

    def matches_watchlist(self, record: FOARecord, watchlist: Watchlist) -> bool:
        if watchlist.keyword:
            haystack = f"{record.program_title} {record.eligibility_summary or ''}".lower()
            if watchlist.keyword.lower() not in haystack:
                return False
        if watchlist.agencies and record.agency not in watchlist.agencies:
            return False
        if watchlist.min_award is not None:
            ceiling = record.estimated_award_ceiling
            if ceiling is not None and ceiling < watchlist.min_award:
                return False
        if watchlist.max_award is not None:
            floor = record.estimated_award_floor
            if floor is not None and floor > watchlist.max_award:
                return False
        return True

    async def match_watchlists(self, db: AsyncSession, org_id: Optional[str], owner_id: str, records: List[FOARecord]) -> int:
        """Checks `records` against active watchlists scoped the same way
        list_watchlists() resolves them, and raises a Notification (Phase
        3's existing table/UI) on each new match. De-duplicates against
        Notifications already raised for that (owner, opportunity) pair, so
        re-running a sync doesn't re-notify on unchanged matches."""
        if not records:
            return 0
        watchlists = await self.list_watchlists(db, org_id, owner_id)
        active = [w for w in watchlists if w.active]
        if not active:
            return 0

        notified = 0
        now = datetime.utcnow()
        # (owner_id, foa_id) pairs already notified — seeded from newly-added
        # rows below too, so two different watchlists matching the same
        # opportunity in this same call raise one notification, not two.
        # Using .first() rather than .scalar_one_or_none() deliberately:
        # this key is intentionally not unique-constrained (a user could in
        # principle end up with more than one such row), so the dedupe
        # check must tolerate that rather than raising MultipleResultsFound.
        already_notified: set = set()

        for wl in active:
            for record in records:
                if not self.matches_watchlist(record, wl):
                    continue
                key = (wl.owner_id, record.id)
                if key in already_notified:
                    continue
                existing = await db.execute(
                    select(Notification).where(
                        Notification.user_id == wl.owner_id, Notification.type == "watchlist_match",
                        Notification.object_type == "foa_record", Notification.object_id == record.id,
                    ).limit(1)
                )
                if existing.scalars().first():
                    already_notified.add(key)
                    continue
                db.add(Notification(
                    id=new_uuid(), user_id=wl.owner_id, type="watchlist_match",
                    message=f'New match for watchlist "{wl.name}": {record.program_title}',
                    object_type="foa_record", object_id=record.id,
                ))
                already_notified.add(key)
                notified += 1
            wl.last_run_at = now
        await db.flush()
        return notified

    # ── Pipeline stage & Bid/No-Go ───────────────────────────────────────────

    async def get_foa_or_404(self, db: AsyncSession, foa_id: str) -> FOARecord:
        result = await db.execute(select(FOARecord).where(FOARecord.id == foa_id))
        record = result.scalar_one_or_none()
        if not record:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        return record

    async def update_pipeline_stage(
        self, db: AsyncSession, foa_id: str, new_stage: str, changed_by: Optional[str], notes: Optional[str] = None,
    ) -> FOARecord:
        if new_stage not in VALID_STAGES:
            raise HTTPException(status_code=400, detail=f"Invalid pipeline stage '{new_stage}'. Must be one of: {', '.join(VALID_STAGES)}.")
        record = await self.get_foa_or_404(db, foa_id)
        old_stage = record.pipeline_stage
        record.pipeline_stage = new_stage
        db.add(PipelineStageEvent(
            id=new_uuid(), foa_id=foa_id, from_stage=old_stage, to_stage=new_stage,
            changed_by=changed_by, notes=notes,
        ))
        await db.flush()
        await db.refresh(record)
        return record

    async def set_bid_no_go(
        self, db: AsyncSession, foa_id: str, decision: str, rationale: Optional[str], changed_by: Optional[str],
    ) -> FOARecord:
        if decision not in VALID_BID_DECISIONS:
            raise HTTPException(status_code=400, detail=f"Invalid decision '{decision}'. Must be one of: {', '.join(VALID_BID_DECISIONS)}.")
        record = await self.get_foa_or_404(db, foa_id)
        record.bid_no_go_decision = decision
        record.bid_no_go_rationale = rationale
        await db.flush()

        # Auto-advance the pipeline stage to reflect the decision — a
        # convenience, not a hard rule: it only nudges the stage forward
        # from an early, pre-submission stage, and never overrides a stage
        # a human already progressed further (e.g. "submitted" or any
        # terminal stage) — flipping a Bid/No-Go toggle after submission
        # shouldn't silently rewrite what actually happened.
        PRE_SUBMISSION_STAGES = ("identified", "qualifying", "pursuing")
        if decision == "no_go" and record.pipeline_stage in PRE_SUBMISSION_STAGES:
            await self.update_pipeline_stage(db, foa_id, "no_go", changed_by, notes="Auto-set from Bid/No-Go decision.")
        elif decision == "bid" and record.pipeline_stage == "identified":
            await self.update_pipeline_stage(db, foa_id, "qualifying", changed_by, notes="Auto-set from Bid/No-Go decision.")

        await db.refresh(record)
        return record

    async def list_stage_events(self, db: AsyncSession, foa_id: str) -> List[PipelineStageEvent]:
        result = await db.execute(
            select(PipelineStageEvent).where(PipelineStageEvent.foa_id == foa_id).order_by(PipelineStageEvent.created_at)
        )
        return list(result.scalars().all())

    # ── Listing, filters, compare ────────────────────────────────────────────

    async def list_pipeline(
        self, db: AsyncSession, org_id: Optional[str] = None, uploaded_by: Optional[str] = None,
        pipeline_stage: Optional[str] = None, source: Optional[str] = None, assigned_to: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> List[FOARecord]:
        """
        `keyword`, if given, narrows an already-synced pipeline by a simple
        case-insensitive substring match across the fields a user would
        actually recognize an opportunity by (title, agency,
        solicitation number, eligibility read, AI summary/brief). This is
        NOT the same thing as the sync keyword (POST /funding/sync's
        `keyword`, which is sent to Grants.gov's search API to pull in NEW
        matching records from the outside world) — this filters what's
        already sitting in the pipeline table, non-destructively, so a
        user narrowing "NanoResearch, Inc"'s 80+ broadly-synced
        opportunities down to ones actually mentioning e.g. "nanomaterials"
        doesn't need to trigger another live Grants.gov call to do it.
        This is a deliberately simple substring filter, not the
        organization-profile fit-scoring from the Funding Opportunity
        Intelligence roadmap's Phase 2 (Funding Intelligence Profile /
        FOFS) — that's a separate, larger, explicitly-deferred piece of
        work; this just stops obviously-irrelevant results from cluttering
        the view today.
        """
        query = select(FOARecord)
        if org_id:
            query = query.where(FOARecord.org_id == org_id)
        elif uploaded_by:
            query = query.where(FOARecord.org_id.is_(None), FOARecord.uploaded_by == uploaded_by)
        if pipeline_stage:
            query = query.where(FOARecord.pipeline_stage == pipeline_stage)
        if source:
            query = query.where(FOARecord.source == source)
        if assigned_to:
            query = query.where(FOARecord.assigned_to == assigned_to)
        if keyword:
            like = f"%{keyword.strip()}%"
            query = query.where(or_(
                FOARecord.program_title.ilike(like),
                FOARecord.agency.ilike(like),
                FOARecord.solicitation_number.ilike(like),
                FOARecord.eligibility_summary.ilike(like),
                FOARecord.ai_summary.ilike(like),
            ))
        result = await db.execute(query.order_by(FOARecord.created_at.desc()))
        return list(result.scalars().all())

    async def compare(self, db: AsyncSession, foa_ids: List[str]) -> List[FOARecord]:
        result = await db.execute(select(FOARecord).where(FOARecord.id.in_(foa_ids)))
        return list(result.scalars().all())

    # ── Pipeline reporting ───────────────────────────────────────────────────

    async def get_pipeline_report(
        self, db: AsyncSession, org_id: Optional[str] = None, uploaded_by: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> Dict[str, Any]:
        records = await self.list_pipeline(db, org_id=org_id, uploaded_by=uploaded_by, keyword=keyword)

        by_stage: Dict[str, int] = {}
        for r in records:
            by_stage[r.pipeline_stage] = by_stage.get(r.pipeline_stage, 0) + 1

        awarded = by_stage.get("awarded", 0)
        lost = by_stage.get("declined", 0) + by_stage.get("no_go", 0)
        win_rate = (awarded / (awarded + lost)) if (awarded + lost) > 0 else None

        pipeline_value = sum(
            (r.estimated_award_ceiling or 0.0) for r in records if r.pipeline_stage not in TERMINAL_STAGES
        )

        cycle_times: List[float] = []
        for r in records:
            if r.pipeline_stage not in TERMINAL_STAGES:
                continue
            events = await self.list_stage_events(db, r.id)
            start = events[0].created_at if events else r.created_at
            end = events[-1].created_at if events else r.created_at
            if start and end:
                cycle_times.append((end - start).total_seconds() / 86400.0)
        avg_cycle_time_days = (sum(cycle_times) / len(cycle_times)) if cycle_times else None

        return {
            "total_opportunities": len(records),
            "by_stage": by_stage,
            "win_rate": win_rate,
            "avg_cycle_time_days": avg_cycle_time_days,
            "pipeline_value": pipeline_value,
        }


def _parse_sam_date(value: Optional[str]) -> Optional[datetime]:
    """SAM.gov dates come back as 'MM/dd/yyyy HH:mm:ss zzz' strings."""
    if not value:
        return None
    for fmt in ("%m/%d/%Y %H:%M:%S %Z", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
