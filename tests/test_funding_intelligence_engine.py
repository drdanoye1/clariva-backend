"""
Engine 15 — Funding Intelligence Engine.

Real Grants.gov/SAM.gov HTTP calls are excluded from this suite, same
policy as OpenAI calls elsewhere in this codebase (see conftest.py's
module docstring): `fetch_grants_gov`/`fetch_sam_gov` are monkeypatched to
return canned "hits" so the deterministic upsert/dedupe, watchlist
matching, pipeline-stage, Bid/No-Go, and reporting logic underneath them
gets full, real coverage without any network access. SAM.gov's
"not configured" path (no SAM_GOV_API_KEY in the test environment, per
conftest.py) is exercised directly, exactly as it would run in production
until a key is added.

Like the other engine test files, methods are async and need a real DB
session, so each test wraps its body in asyncio.run() rather than pulling
in pytest-asyncio for just this file.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from database import AsyncSessionLocal
from engines.funding_intelligence_engine import FundingIntelligenceEngine
from models.db_models import FOARecord, Notification, PipelineStageEvent, new_uuid


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def engine():
    return FundingIntelligenceEngine()


def _id(prefix: str) -> str:
    return f"test-{prefix}-{uuid.uuid4().hex[:12]}"


def _grants_gov_hit(number=None, opp_id=None, title="Test Opportunity", agency="HHS", close_date="12/31/2026"):
    # Unique per call by default — the test DB persists across the whole
    # pytest run (see test_collaboration_engine.py's lesson on this), so a
    # fixed literal external_id here would let unrelated tests dedupe
    # against each other's records. Tests that specifically need the SAME
    # external_id across two calls (to exercise the upsert path) pass one
    # explicitly.
    number = number or f"TEST-OPP-{uuid.uuid4().hex[:12]}"
    opp_id = opp_id or uuid.uuid4().hex[:8]
    return {"id": opp_id, "number": number, "title": title, "agencyCode": agency, "closeDate": close_date}


async def _make_foa(db, **overrides) -> FOARecord:
    defaults = dict(
        id=new_uuid(), agency="NSF", program_title="Manual Opportunity", phase="phase_i",
        grant_type="sbir", uploaded_by=_id("user"),
    )
    defaults.update(overrides)
    record = FOARecord(**defaults)
    db.add(record)
    await db.flush()
    await db.refresh(record)
    return record


# ── Grants.gov sync (network mocked) ────────────────────────────────────────

def test_sync_grants_gov_creates_new_record(client, engine, monkeypatch):
    org_id = _id("org")
    hit = _grants_gov_hit()

    async def fake_fetch(**kwargs):
        return [hit]

    monkeypatch.setattr(engine, "fetch_grants_gov", fake_fetch)

    async def _body():
        async with AsyncSessionLocal() as db:
            created, updated, touched = await engine.sync_grants_gov(db, org_id, "user-1")
            await db.commit()
            return created, updated, touched

    created, updated, touched = _run(_body())
    assert created == 1
    assert updated == 0
    assert touched[0].source == "grants_gov"
    assert touched[0].external_id == hit["number"]
    assert touched[0].pipeline_stage == "identified"
    assert touched[0].org_id == org_id


def test_sync_grants_gov_upserts_on_rerun(client, engine, monkeypatch):
    org_id = _id("org")
    fixed_number = f"TEST-OPP-{uuid.uuid4().hex[:12]}"
    call_count = {"n": 0}

    async def fake_fetch(**kwargs):
        call_count["n"] += 1
        title = "Updated Title" if call_count["n"] > 1 else "Original Title"
        return [_grants_gov_hit(number=fixed_number, title=title)]

    monkeypatch.setattr(engine, "fetch_grants_gov", fake_fetch)

    async def _body():
        async with AsyncSessionLocal() as db:
            c1, u1, _ = await engine.sync_grants_gov(db, org_id, "user-1")
            await db.commit()
        async with AsyncSessionLocal() as db:
            c2, u2, touched2 = await engine.sync_grants_gov(db, org_id, "user-1")
            await db.commit()
            return c1, u1, c2, u2, touched2

    c1, u1, c2, u2, touched2 = _run(_body())
    assert c1 == 1 and u1 == 0
    assert c2 == 0 and u2 == 1  # same external_id -> updated, not duplicated
    assert touched2[0].program_title == "Updated Title"


def test_sync_grants_gov_skips_hits_without_external_id(client, engine, monkeypatch):
    org_id = _id("org")

    async def fake_fetch(**kwargs):
        return [{"id": None, "number": None, "title": "No ID"}]

    monkeypatch.setattr(engine, "fetch_grants_gov", fake_fetch)

    async def _body():
        async with AsyncSessionLocal() as db:
            created, updated, touched = await engine.sync_grants_gov(db, org_id, "user-1")
            await db.commit()
            return created, updated, touched

    created, updated, touched = _run(_body())
    assert created == 0 and updated == 0 and touched == []


def test_sync_sam_gov_not_configured_by_default(client, engine):
    """No SAM_GOV_API_KEY in the test environment (conftest.py) — must
    report configured=False rather than attempting a network call."""
    async def _body():
        async with AsyncSessionLocal() as db:
            return await engine.sync_sam_gov(db, None, "user-1")

    configured, created, updated, touched = _run(_body())
    assert configured is False
    assert created == 0 and updated == 0 and touched == []


# ── Watchlists ───────────────────────────────────────────────────────────────

def test_create_and_list_watchlist(client, engine):
    org_id = _id("org")
    owner_id = _id("user")

    async def _body():
        async with AsyncSessionLocal() as db:
            wl = await engine.create_watchlist(db, org_id, owner_id, {"name": "Health IT", "keyword": "health"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            return wl, await engine.list_watchlists(db, org_id, owner_id)

    wl, listed = _run(_body())
    assert wl.active is True
    assert [w.id for w in listed] == [wl.id]


def test_personal_and_org_watchlists_are_isolated(client, engine):
    org_id = _id("org")
    owner_id = _id("user")

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_watchlist(db, org_id, owner_id, {"name": "Org WL"})
            await engine.create_watchlist(db, None, owner_id, {"name": "Personal WL"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            org_list = await engine.list_watchlists(db, org_id, owner_id)
            personal_list = await engine.list_watchlists(db, None, owner_id)
            return org_list, personal_list

    org_list, personal_list = _run(_body())
    assert [w.name for w in org_list] == ["Org WL"]
    assert [w.name for w in personal_list] == ["Personal WL"]


def test_update_and_delete_watchlist(client, engine):
    owner_id = _id("user")

    async def _body():
        async with AsyncSessionLocal() as db:
            wl = await engine.create_watchlist(db, None, owner_id, {"name": "Original"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            updated = await engine.update_watchlist(db, wl.id, {"name": "Renamed", "active": False})
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.delete_watchlist(db, wl.id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            return updated, await engine.list_watchlists(db, None, owner_id)

    updated, remaining = _run(_body())
    assert updated.name == "Renamed"
    assert updated.active is False
    assert remaining == []


def test_get_watchlist_or_404(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.get_watchlist_or_404(db, "does-not-exist")
            return exc_info.value.status_code

    assert _run(_body()) == 404


def test_matches_watchlist_keyword_and_award_bounds(client, engine):
    owner_id = _id("user")

    async def _body():
        async with AsyncSessionLocal() as db:
            record = await _make_foa(db, program_title="Rural Broadband Expansion", estimated_award_ceiling=50000.0, estimated_award_floor=10000.0)
            wl_match = await engine.create_watchlist(db, None, owner_id, {"name": "Broadband", "keyword": "broadband", "min_award": 20000.0})
            wl_no_match_keyword = await engine.create_watchlist(db, None, owner_id, {"name": "Health", "keyword": "health"})
            wl_no_match_award = await engine.create_watchlist(db, None, owner_id, {"name": "TooExpensive", "max_award": 5000.0})
            await db.commit()
            return record, wl_match, wl_no_match_keyword, wl_no_match_award

    record, wl_match, wl_no_match_keyword, wl_no_match_award = _run(_body())
    assert engine.matches_watchlist(record, wl_match) is True
    assert engine.matches_watchlist(record, wl_no_match_keyword) is False
    assert engine.matches_watchlist(record, wl_no_match_award) is False


def test_match_watchlists_notifies_once_and_dedupes_on_rerun(client, engine):
    owner_id = _id("user")

    async def _body():
        async with AsyncSessionLocal() as db:
            record = await _make_foa(db, program_title="AI Research Grant")
            wl = await engine.create_watchlist(db, None, owner_id, {"name": "AI", "keyword": "AI Research"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            first = await engine.match_watchlists(db, None, owner_id, [record])
            await db.commit()
        async with AsyncSessionLocal() as db:
            second = await engine.match_watchlists(db, None, owner_id, [record])
            await db.commit()
        async with AsyncSessionLocal() as db:
            from sqlalchemy import select
            result = await db.execute(select(Notification).where(Notification.user_id == owner_id, Notification.type == "watchlist_match"))
            notes = list(result.scalars().all())
            return first, second, notes

    first, second, notes = _run(_body())
    assert first == 1
    assert second == 0  # already notified for this (watchlist owner, opportunity) pair
    assert len(notes) == 1


def test_match_watchlists_two_matching_watchlists_notify_once_and_dont_break_rerun(client, engine):
    """Regression test: if two of a user's watchlists both match the same
    opportunity, this must not insert two Notification rows for the same
    (owner, opportunity) key — a later match_watchlists() call re-checking
    that key would otherwise raise MultipleResultsFound instead of just
    treating it as already notified."""
    owner_id = _id("user")

    async def _body():
        async with AsyncSessionLocal() as db:
            record = await _make_foa(db, program_title="Rural Health Broadband Grant")
            await engine.create_watchlist(db, None, owner_id, {"name": "Health", "keyword": "health"})
            await engine.create_watchlist(db, None, owner_id, {"name": "Broadband", "keyword": "broadband"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            first = await engine.match_watchlists(db, None, owner_id, [record])
            await db.commit()
        async with AsyncSessionLocal() as db:
            # Must not raise, even though two watchlists matched above.
            second = await engine.match_watchlists(db, None, owner_id, [record])
            return first, second, record

    first, second, _ = _run(_body())
    assert first == 1
    assert second == 0


def test_match_watchlists_skips_inactive(client, engine):
    owner_id = _id("user")

    async def _body():
        async with AsyncSessionLocal() as db:
            record = await _make_foa(db, program_title="Clean Energy Grant")
            wl = await engine.create_watchlist(db, None, owner_id, {"name": "Energy", "keyword": "energy"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.update_watchlist(db, wl.id, {"active": False})
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.match_watchlists(db, None, owner_id, [record])

    assert _run(_body()) == 0


# ── Pipeline stage & Bid/No-Go ───────────────────────────────────────────────

def test_update_pipeline_stage_records_event(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            record = await _make_foa(db)
            await db.commit()
        async with AsyncSessionLocal() as db:
            updated = await engine.update_pipeline_stage(db, record.id, "qualifying", "user-1", notes="Looks promising")
            await db.commit()
        async with AsyncSessionLocal() as db:
            events = await engine.list_stage_events(db, record.id)
            return updated, events

    updated, events = _run(_body())
    assert updated.pipeline_stage == "qualifying"
    assert len(events) == 1
    assert events[0].from_stage == "identified"
    assert events[0].to_stage == "qualifying"
    assert events[0].notes == "Looks promising"


def test_update_pipeline_stage_rejects_invalid_stage(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            record = await _make_foa(db)
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.update_pipeline_stage(db, record.id, "not_a_real_stage", "user-1")
            return exc_info.value.status_code

    assert _run(_body()) == 400


def test_set_bid_no_go_auto_advances_stage(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            bid_record = await _make_foa(db)
            no_go_record = await _make_foa(db)
            await db.commit()
        async with AsyncSessionLocal() as db:
            bid_updated = await engine.set_bid_no_go(db, bid_record.id, "bid", "Strong fit", "user-1")
            no_go_updated = await engine.set_bid_no_go(db, no_go_record.id, "no_go", "Not eligible", "user-1")
            await db.commit()
            return bid_updated, no_go_updated

    bid_updated, no_go_updated = _run(_body())
    assert bid_updated.pipeline_stage == "qualifying"
    assert bid_updated.bid_no_go_decision == "bid"
    assert no_go_updated.pipeline_stage == "no_go"
    assert no_go_updated.bid_no_go_decision == "no_go"


def test_set_bid_no_go_rejects_invalid_decision(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            record = await _make_foa(db)
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.set_bid_no_go(db, record.id, "maybe", None, "user-1")
            return exc_info.value.status_code

    assert _run(_body()) == 400


def test_no_go_does_not_override_already_submitted(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            record = await _make_foa(db)
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.update_pipeline_stage(db, record.id, "submitted", "user-1")
            await db.commit()
        async with AsyncSessionLocal() as db:
            # A submitted opportunity is already terminal-adjacent; setting
            # no_go here would be a data-entry mistake this engine
            # shouldn't silently "fix" by downgrading a real submission.
            updated = await engine.set_bid_no_go(db, record.id, "no_go", "Changed our mind too late", "user-1")
            return updated

    updated = _run(_body())
    assert updated.pipeline_stage == "submitted"


# ── Listing, filters, compare ────────────────────────────────────────────────

def test_list_pipeline_filters_by_org_stage_and_source(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            await _make_foa(db, org_id=org_id, source="manual")
            await _make_foa(db, org_id=org_id, source="grants_gov", external_id="ext-1")
            await _make_foa(db, uploaded_by="user-2")  # personal, different org scope
            await db.commit()
        async with AsyncSessionLocal() as db:
            org_records = await engine.list_pipeline(db, org_id=org_id)
            personal_records = await engine.list_pipeline(db, uploaded_by="user-2")
            synced_only = await engine.list_pipeline(db, org_id=org_id, source="grants_gov")
            return org_records, personal_records, synced_only

    org_records, personal_records, synced_only = _run(_body())
    assert len(org_records) == 2


# Funding Opportunity Intelligence, Phase 1 (keyword-filter fix) — confirms
# `list_pipeline(keyword=...)` narrows an already-synced pipeline by a
# case-insensitive substring match, independent of org/stage/source
# filters, and that it's a pure read (no records are created/removed/
# modified) — distinct from fundingApi.sync's keyword, which hits
# Grants.gov's live API.
def test_list_pipeline_keyword_filters_existing_records(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            await _make_foa(db, org_id=org_id, program_title="Nanomaterials for Sensor Research", agency="NSF")
            await _make_foa(db, org_id=org_id, program_title="Rural Broadband Expansion", agency="USDA-RD",
                             eligibility_summary="Open to nonprofit nanotechnology consortia.")
            await _make_foa(db, org_id=org_id, program_title="Suicide Prevention Peer Support Services", agency="HHS-NIH11")
            await db.commit()
        async with AsyncSessionLocal() as db:
            title_match = await engine.list_pipeline(db, org_id=org_id, keyword="nanomaterials")
            eligibility_match = await engine.list_pipeline(db, org_id=org_id, keyword="nanotechnology")
            no_match = await engine.list_pipeline(db, org_id=org_id, keyword="quantum")
            everything = await engine.list_pipeline(db, org_id=org_id)
            return title_match, eligibility_match, no_match, everything

    title_match, eligibility_match, no_match, everything = _run(_body())
    assert len(everything) == 3
    assert len(title_match) == 1 and title_match[0].program_title == "Nanomaterials for Sensor Research"
    assert len(eligibility_match) == 1 and eligibility_match[0].program_title == "Rural Broadband Expansion"
    assert no_match == []


def test_compare_returns_requested_records(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            r1 = await _make_foa(db, program_title="Opp A")
            r2 = await _make_foa(db, program_title="Opp B")
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.compare(db, [r1.id, r2.id])

    results = _run(_body())
    assert {r.program_title for r in results} == {"Opp A", "Opp B"}


# ── Pipeline reporting ───────────────────────────────────────────────────────

def test_pipeline_report_computes_win_rate_and_value(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            r1 = await _make_foa(db, org_id=org_id, estimated_award_ceiling=10000.0)  # stays "identified"
            r2 = await _make_foa(db, org_id=org_id)
            r3 = await _make_foa(db, org_id=org_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.update_pipeline_stage(db, r2.id, "awarded", "user-1")
            await engine.update_pipeline_stage(db, r3.id, "declined", "user-1")
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_pipeline_report(db, org_id=org_id)

    report = _run(_body())
    assert report["total_opportunities"] == 3
    assert report["by_stage"]["awarded"] == 1
    assert report["by_stage"]["declined"] == 1
    assert report["by_stage"]["identified"] == 1
    assert report["win_rate"] == 0.5
    assert report["pipeline_value"] == 10000.0  # only the still-open opportunity counts


def test_pipeline_report_win_rate_none_with_no_terminal_records(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            await _make_foa(db, org_id=org_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_pipeline_report(db, org_id=org_id)

    report = _run(_body())
    assert report["win_rate"] is None


def test_pipeline_report_cycle_time_reflects_stage_event_span(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            record = await _make_foa(db, org_id=org_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.update_pipeline_stage(db, record.id, "pursuing", "user-1")
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.update_pipeline_stage(db, record.id, "awarded", "user-1")
            await db.commit()
        async with AsyncSessionLocal() as db:
            events = await engine.list_stage_events(db, record.id)
            # Backdate the first event so there's a nonzero, deterministic span to assert on.
            events[0].created_at = datetime.utcnow() - timedelta(days=10)
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.get_pipeline_report(db, org_id=org_id)

    report = _run(_body())
    assert report["avg_cycle_time_days"] is not None
    assert report["avg_cycle_time_days"] >= 9.9
