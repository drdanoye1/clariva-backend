"""
Funding Intelligence router — Grants.gov/SAM.gov sync, watchlists, and
portfolio pipeline reporting (Clariva Enterprise™ PRD §15, Phase 4).
Mounted at /api/v1/funding. FOARecord-centric pipeline-stage/Bid-No-Go/
compare endpoints live directly on routers/foa.py instead (per the PRD's
"extending foa.py" framing) — this router is for the surface that's new
rather than an extension of an existing FOARecord action.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import User
from models.schemas import (
    HistoricalPerformanceOut, LearningTimelineEventOut, PipelineReportOut,
    SyncResultOut, WatchlistCreate, WatchlistOut, WatchlistUpdate,
)
from routers.auth import get_current_user
from routers.organizations import _assert_member, _assert_permission
from engines.funding_intelligence_engine import FundingIntelligenceEngine
from engines.organizational_learning_engine import OrganizationalLearningEngine
from engines.historical_performance_engine import HistoricalFundingPerformanceEngine

router = APIRouter()
engine = FundingIntelligenceEngine()
learning_engine = OrganizationalLearningEngine()
performance_engine = HistoricalFundingPerformanceEngine()


# ── Sync (manual trigger — no background scheduler in this environment) ────

@router.post("/sync", response_model=List[SyncResultOut])
async def sync_opportunities(
    org_id: Optional[str] = None, keyword: Optional[str] = None,
    agencies: Optional[str] = None, funding_categories: Optional[str] = None, rows: int = 100,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """
    Fetch new/updated opportunities from Grants.gov (always) and SAM.gov
    (only if SAM_GOV_API_KEY is configured) and upsert them into the
    pipeline, then check active watchlists for matches. `agencies` and
    `funding_categories` are comma-separated lists.
    """
    if org_id:
        await _assert_permission(org_id, current_user.id, "manage_watchlists", db)

    agency_list = [a.strip() for a in agencies.split(",")] if agencies else None
    category_list = [c.strip() for c in funding_categories.split(",")] if funding_categories else None

    results: List[SyncResultOut] = []
    all_touched = []

    try:
        created, updated, touched = await engine.sync_grants_gov(
            db, org_id, current_user.id, keyword=keyword, agencies=agency_list,
            funding_categories=category_list, rows=rows,
        )
        results.append(SyncResultOut(source="grants_gov", configured=True, created_count=created, updated_count=updated))
        all_touched.extend(touched)
    except Exception as exc:
        results.append(SyncResultOut(source="grants_gov", configured=True, message=f"Sync failed: {exc}"))

    try:
        configured, created, updated, touched = await engine.sync_sam_gov(db, org_id, current_user.id, keyword=keyword, rows=rows)
        if configured:
            results.append(SyncResultOut(source="sam_gov", configured=True, created_count=created, updated_count=updated))
            all_touched.extend(touched)
        else:
            results.append(SyncResultOut(source="sam_gov", configured=False, message="SAM_GOV_API_KEY is not configured."))
    except Exception as exc:
        results.append(SyncResultOut(source="sam_gov", configured=True, message=f"Sync failed: {exc}"))

    if all_touched:
        matched = await engine.match_watchlists(db, org_id, current_user.id, all_touched)
        for r in results:
            if r.created_count or r.updated_count:
                r.matched_watchlists = matched

    return results


# ── Watchlists ───────────────────────────────────────────────────────────────

@router.post("/watchlists", response_model=WatchlistOut, status_code=201)
async def create_watchlist(
    body: WatchlistCreate, org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    if org_id:
        await _assert_permission(org_id, current_user.id, "manage_watchlists", db)
    return await engine.create_watchlist(db, org_id, current_user.id, body.model_dump())


@router.get("/watchlists", response_model=List[WatchlistOut])
async def list_watchlists(
    org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    if org_id:
        await _assert_member(org_id, current_user.id, db)
    return await engine.list_watchlists(db, org_id, current_user.id)


@router.patch("/watchlists/{watchlist_id}", response_model=WatchlistOut)
async def update_watchlist(
    watchlist_id: str, body: WatchlistUpdate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    wl = await engine.get_watchlist_or_404(db, watchlist_id)
    if wl.org_id:
        await _assert_permission(wl.org_id, current_user.id, "manage_watchlists", db)
    elif wl.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return await engine.update_watchlist(db, watchlist_id, body.model_dump(exclude_unset=True))


@router.delete("/watchlists/{watchlist_id}", status_code=204)
async def delete_watchlist(
    watchlist_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    wl = await engine.get_watchlist_or_404(db, watchlist_id)
    if wl.org_id:
        await _assert_permission(wl.org_id, current_user.id, "manage_watchlists", db)
    elif wl.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    await engine.delete_watchlist(db, watchlist_id)


# ── Pipeline Reporting ───────────────────────────────────────────────────────

@router.get("/pipeline-report", response_model=PipelineReportOut)
async def get_pipeline_report(
    org_id: Optional[str] = None, keyword: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Portfolio-level win rate, cycle time, and pipeline value (PRD §15) —
    over an org's shared pipeline, or the current user's personal one.
    `keyword`, when given, scopes these totals to the same non-destructive
    substring-filtered subset the pipeline board is currently showing
    (Funding Opportunity Intelligence Phase 1's keyword-filter fix), so
    "Total Opportunities" reflects what's actually on screen rather than
    the org's whole unfiltered pipeline."""
    if org_id:
        await _assert_member(org_id, current_user.id, db)
    report = await engine.get_pipeline_report(
        db, org_id=org_id, uploaded_by=None if org_id else current_user.id, keyword=keyword,
    )
    return PipelineReportOut(**report)


# ── Organizational Learning & Historical Performance (Version 3.0 upgrade, ──
# Phase 3.1) — same "Personal / one Organization" single-scope selector as
# every other endpoint in this router.

@router.get("/learning-timeline", response_model=List[LearningTimelineEventOut])
async def get_learning_timeline(
    org_id: Optional[str] = None, limit: int = 100,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """A chronological feed of what this pipeline has actually done —
    opportunities discovered, stage/Bid-No-Go transitions, proposal status
    changes, funded/not-funded outcomes, and award creation/closeout —
    assembled from tables that already exist for their own reasons (see
    engines/organizational_learning_engine.py's module docstring)."""
    if org_id:
        await _assert_member(org_id, current_user.id, db)
    events = await learning_engine.get_timeline(
        db, org_id=org_id, uploaded_by=None if org_id else current_user.id, limit=limit,
    )
    return [LearningTimelineEventOut(**e) for e in events]


@router.get("/historical-performance", response_model=HistoricalPerformanceOut)
async def get_historical_performance(
    org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Descriptive win-rate/cycle-time/dollar-value analytics, broken down
    by agency, program type, and funding range — purely historical, never
    an estimated probability of winning a specific future opportunity."""
    if org_id:
        await _assert_member(org_id, current_user.id, db)
    performance = await performance_engine.get_performance(
        db, org_id=org_id, uploaded_by=None if org_id else current_user.id,
    )
    return HistoricalPerformanceOut(**performance)
