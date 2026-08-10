"""
Version 3.0 architecture upgrade, Phase 3.3 — Intelligent Alerts
(Engine 25). See docs/Clariva Funding Opportunity Intelligence product
definition upgrade for monetization_1.docx §4.4, and
engines/alerts_engine.py's module docstring for the design rationale
(five alert types, each mapped to an existing cheap signal, all routed
through notifications.py::notify(dedupe=True)).

Same convention as test_organizational_learning.py /
test_portfolio_recommendation_engine.py: engine methods are async and need
a real DB session, so test bodies wrap themselves in asyncio.run(). The
`client` fixture is requested purely to trigger FastAPI's lifespan startup
(database.py::create_tables()).

NOTE: this sandbox session has no network access — `pip install` fails
outright, so this file could not actually be executed here. It follows the
same fixture/assertion patterns as the executed precedents above.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from database import AsyncSessionLocal
from engines.alerts_engine import AlertsEngine, DEADLINE_WARNING_DAYS
from models.db_models import Award, FOARecord, Notification, OrgContextDB, Proposal, new_uuid


def _run(coro):
    return asyncio.run(coro)


def _id(prefix: str) -> str:
    return f"test-{prefix}-{uuid.uuid4().hex[:12]}"


async def _make_foa(db, **overrides) -> FOARecord:
    defaults = dict(
        id=new_uuid(), agency="NSF", program_title="Nanomaterials for Sensor Research",
        phase="phase_i", grant_type="sbir", uploaded_by=_id("user"),
        pipeline_stage="identified",
        eligibility_summary="Small businesses with prior SBIR experience are eligible nationwide.",
        estimated_award_floor=100_000, estimated_award_ceiling=250_000,
        eligibility_status="Eligible", complexity="Moderate", attractiveness="High",
        intelligence_report={
            "eligibility_assessment": {"status": "Eligible", "issues": []},
            "funding_priorities": ["nanotechnology", "sensors"],
            "requirements": ["prototype demonstration"],
            "cost_share": {"required": False},
        },
    )
    defaults.update(overrides)
    record = FOARecord(**defaults)
    db.add(record)
    await db.flush()
    await db.refresh(record)
    return record


async def _make_profile(db, user_id: str, **overrides) -> OrgContextDB:
    defaults = dict(
        id=new_uuid(), user_id=user_id,
        organization_name="NanoResearch, Inc", industry="Nanotechnology",
        mission_statement="Advancing nanomaterial sensor technology for industrial applications.",
        core_technologies=["nanomaterials", "sensors"], industries=["nanotechnology"],
        certifications=["small business"], entity_type="small business", prior_sbir_experience=True,
        past_performance=[{"title": "DoD sensor prototype"}],
        funding_preferences={
            "min_award": 50_000, "max_award": 300_000, "preferred_agencies": ["NSF"],
            "preferred_phase": "phase_i", "cost_share_tolerance": "none",
        },
        service_geography=["nationwide"], team_members=[{"name": "a"}, {"name": "b"}, {"name": "c"}],
    )
    defaults.update(overrides)
    profile = OrgContextDB(**defaults)
    db.add(profile)
    await db.flush()
    await db.refresh(profile)
    return profile


async def _notifications_for(db, user_id: str, type_: str):
    result = await db.execute(
        select(Notification).where(Notification.user_id == user_id, Notification.type == type_)
    )
    return list(result.scalars().all())


# ── 1. High-fit opportunity discovered ───────────────────────────────────────

def test_high_fit_discovered_notifies_only_for_new_pursue_records(client):
    user_id = _id("user")
    engine = AlertsEngine()

    async def _body():
        async with AsyncSessionLocal() as db:
            await _make_profile(db, user_id)
            strong = await _make_foa(db, uploaded_by=user_id, program_title="Strong New Opportunity")
            weak = await _make_foa(
                db, uploaded_by=user_id, program_title="Weak New Opportunity", agency="DOE",
                eligibility_summary=None, estimated_award_floor=None, estimated_award_ceiling=None,
                eligibility_status=None, complexity=None, attractiveness=None, intelligence_report=None,
            )
            await db.commit()
            # Simulate _upsert_hits leaving no `_changed_fields` on brand-new records.
            touched = [strong, weak]
            await engine.run_sync_checks(db, None, user_id, touched)
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await _notifications_for(db, user_id, "high_fit_opportunity")

    notes = _run(_body())
    assert len(notes) == 1
    assert "Strong New Opportunity" in notes[0].message


def test_high_fit_discovered_dedupes_on_rerun(client):
    user_id = _id("user")
    engine = AlertsEngine()

    async def _body():
        async with AsyncSessionLocal() as db:
            await _make_profile(db, user_id)
            record = await _make_foa(db, uploaded_by=user_id)
            await db.commit()
            await engine.run_sync_checks(db, None, user_id, [record])
            await db.commit()
            # Re-running with the exact same "new" record must not double-notify.
            await engine.run_sync_checks(db, None, user_id, [record])
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await _notifications_for(db, user_id, "high_fit_opportunity")

    assert len(_run(_body())) == 1


# ── 2. Solicitation amendment changed ────────────────────────────────────────

def test_amendment_alert_fires_when_changed_fields_present(client):
    user_id = _id("user")
    engine = AlertsEngine()

    async def _body():
        async with AsyncSessionLocal() as db:
            record = await _make_foa(db, uploaded_by=user_id, program_title="Amended Opportunity")
            await db.commit()
            record._changed_fields = ["deadline", "program_title"]
            await engine.run_sync_checks(db, None, user_id, [record])
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await _notifications_for(db, user_id, "solicitation_amended")

    notes = _run(_body())
    assert len(notes) == 1
    assert "deadline" in notes[0].message and "title" in notes[0].message


def test_no_amendment_alert_when_changed_fields_empty(client):
    user_id = _id("user")
    engine = AlertsEngine()

    async def _body():
        async with AsyncSessionLocal() as db:
            record = await _make_foa(db, uploaded_by=user_id)
            await db.commit()
            record._changed_fields = []  # _upsert_hits sets this for untouched existing records
            await engine.run_sync_checks(db, None, user_id, [record])
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await _notifications_for(db, user_id, "solicitation_amended")

    assert _run(_body()) == []


# ── 3. Deadline approaching with incomplete requirements ─────────────────────

def test_deadline_approaching_fires_for_active_pursuit_with_no_proposal(client):
    user_id = _id("user")
    engine = AlertsEngine()
    now = datetime.now(timezone.utc)

    async def _body():
        async with AsyncSessionLocal() as db:
            soon = await _make_foa(
                db, uploaded_by=user_id, program_title="Deadline Soon", pipeline_stage="pursuing",
                deadline=now + timedelta(days=DEADLINE_WARNING_DAYS - 1),
            )
            far = await _make_foa(
                db, uploaded_by=user_id, program_title="Deadline Far", pipeline_stage="pursuing",
                deadline=now + timedelta(days=90),
            )
            await db.commit()
            await engine.run_sync_checks(db, None, user_id, [])
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await _notifications_for(db, user_id, "deadline_approaching")

    notes = _run(_body())
    assert len(notes) == 1
    assert "Deadline Soon" in notes[0].message
    assert "no proposal started yet" in notes[0].message


def test_deadline_approaching_skips_when_proposal_past_draft(client):
    user_id = _id("user")
    engine = AlertsEngine()
    now = datetime.now(timezone.utc)

    async def _body():
        async with AsyncSessionLocal() as db:
            record = await _make_foa(
                db, uploaded_by=user_id, pipeline_stage="pursuing",
                deadline=now + timedelta(days=2),
            )
            db.add(Proposal(
                id=new_uuid(), owner_id=user_id, title="In-Progress Proposal", agency="NSF",
                phase="phase_i", grant_type="sbir", status="in_review", foa_id=record.id,
            ))
            await db.commit()
            await engine.run_sync_checks(db, None, user_id, [])
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await _notifications_for(db, user_id, "deadline_approaching")

    assert _run(_body()) == []


# ── 4. Resource conflict between high-priority pursuits ──────────────────────

def test_resource_conflict_alert_mirrors_portfolio_engine_clustering(client):
    user_id = _id("user")
    engine = AlertsEngine()
    now = datetime.now(timezone.utc)

    async def _body():
        async with AsyncSessionLocal() as db:
            await _make_profile(db, user_id)
            await _make_foa(
                db, uploaded_by=user_id, program_title="Conflict A", pipeline_stage="pursuing",
                deadline=now + timedelta(days=10),
            )
            await _make_foa(
                db, uploaded_by=user_id, program_title="Conflict B", pipeline_stage="pursuing",
                deadline=now + timedelta(days=15),
            )
            await db.commit()
            await engine.run_sync_checks(db, None, user_id, [])
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await _notifications_for(db, user_id, "resource_conflict")

    notes = _run(_body())
    assert len(notes) == 1
    assert "Conflict A" in notes[0].message and "Conflict B" in notes[0].message


# ── 5. Capability match to a past award ───────────────────────────────────────

def test_capability_match_notifies_on_matching_agency(client):
    user_id = _id("user")
    engine = AlertsEngine()

    async def _body():
        async with AsyncSessionLocal() as db:
            past_foa = await _make_foa(db, uploaded_by=user_id, agency="NSF", grant_type="sbir")
            past_proposal = Proposal(
                id=new_uuid(), owner_id=user_id, title="Past Won Proposal", agency="NSF",
                phase="phase_i", grant_type="sbir", status="funded", foa_id=past_foa.id,
            )
            db.add(past_proposal)
            await db.flush()
            db.add(Award(
                id=new_uuid(), proposal_id=past_proposal.id, foa_id=past_foa.id,
                funding_agency="NSF", total_award_value=250000.0, created_by=user_id,
            ))
            await db.commit()

            new_record = await _make_foa(
                db, uploaded_by=user_id, agency="NSF", program_title="New NSF Opportunity",
            )
            await db.commit()
            await engine.run_sync_checks(db, None, user_id, [new_record])
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await _notifications_for(db, user_id, "capability_match")

    notes = _run(_body())
    assert len(notes) == 1
    assert "New NSF Opportunity" in notes[0].message
    assert "agency" in notes[0].message


def test_capability_match_silent_with_no_prior_awards(client):
    user_id = _id("user")
    engine = AlertsEngine()

    async def _body():
        async with AsyncSessionLocal() as db:
            record = await _make_foa(db, uploaded_by=user_id)
            await db.commit()
            await engine.run_sync_checks(db, None, user_id, [record])
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await _notifications_for(db, user_id, "capability_match")

    assert _run(_body()) == []
