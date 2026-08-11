"""
Version 3.0 architecture upgrade, Phase 3.1 — Organizational Learning
(Engine 22) & Historical Funding Performance (Engine 23). See
docs/Clariva Funding Opportunity Intelligence product definition upgrade
for monetization_1.docx §4.1-4.2, and the module docstrings of
engines/organizational_learning_engine.py and
engines/historical_performance_engine.py for the design rationale.

Like the other engine test files (see test_funding_intelligence_engine.py),
engine methods are async and need a real DB session, so test bodies wrap
themselves in asyncio.run() rather than pulling in pytest-asyncio for just
this file. The `client` fixture is still requested where needed purely to
trigger FastAPI's lifespan startup, which is what creates the test
database's tables via database.py::create_tables().
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from sqlalchemy import select

from database import AsyncSessionLocal
from engines.funding_intelligence_engine import FundingIntelligenceEngine
from engines.historical_performance_engine import HistoricalFundingPerformanceEngine
from engines.organizational_learning_engine import OrganizationalLearningEngine
from models.db_models import (
    Award, AwardCloseout, FOARecord, MemoryRecord, Proposal, ProposalStatusEvent, new_uuid,
)


def _run(coro):
    return asyncio.run(coro)


def _id(prefix: str) -> str:
    return f"test-{prefix}-{uuid.uuid4().hex[:12]}"


async def _make_foa(db, **overrides) -> FOARecord:
    defaults = dict(
        id=new_uuid(), agency="NSF", program_title="Test Opportunity", phase="phase_i",
        grant_type="sbir", uploaded_by=_id("user"),
    )
    defaults.update(overrides)
    record = FOARecord(**defaults)
    db.add(record)
    await db.flush()
    await db.refresh(record)
    return record


async def _make_proposal(db, owner_id: str, **overrides) -> Proposal:
    defaults = dict(
        id=new_uuid(), owner_id=owner_id, title="Test Proposal", agency="NSF",
        phase="phase_i", grant_type="sbir", status="draft",
    )
    defaults.update(overrides)
    proposal = Proposal(**defaults)
    db.add(proposal)
    await db.flush()
    await db.refresh(proposal)
    return proposal


# ── ProposalStatusEvent (routers/proposals.py::update_proposal) ─────────────

def test_status_change_logs_event_and_noop_does_not(client, registered_user):
    resp = client.post(
        "/api/v1/proposals/",
        json={
            "title": "Status Event Test", "agency": "NSF", "phase": "phase_i", "grant_type": "sbir",
            "org_context": {
                "organization_name": "Acme", "industry": "Biotech",
                "core_technologies": [], "prior_sbir_experience": False,
            },
            "research_focus": "Status-change instrumentation test",
            "innovation_description": "N/A — this proposal exists only to exercise status-event logging",
        },
        headers=registered_user["headers"],
    )
    assert resp.status_code == 201, resp.text
    proposal_id = resp.json()["proposal_id"]

    client.patch(f"/api/v1/proposals/{proposal_id}", json={"status": "in_review"}, headers=registered_user["headers"])
    # Re-sending the SAME status must not write a second, redundant event.
    client.patch(f"/api/v1/proposals/{proposal_id}", json={"status": "in_review"}, headers=registered_user["headers"])

    async def _events():
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ProposalStatusEvent).where(ProposalStatusEvent.proposal_id == proposal_id)
            )
            return list(result.scalars().all())

    events = _run(_events())
    assert len(events) == 1
    assert events[0].from_status == "draft"
    assert events[0].to_status == "in_review"


# ── OrganizationalLearningEngine ─────────────────────────────────────────────

def test_learning_timeline_assembles_all_event_types(client):
    user_id = _id("user")
    fi_engine = FundingIntelligenceEngine()
    learning_engine = OrganizationalLearningEngine()
    ids: dict = {}

    async def _body():
        async with AsyncSessionLocal() as db:
            foa = await _make_foa(db, uploaded_by=user_id)
            proposal = await _make_proposal(db, owner_id=user_id, foa_id=foa.id)
            ids["foa_id"], ids["proposal_id"] = foa.id, proposal.id
            await db.commit()

        async with AsyncSessionLocal() as db:
            await fi_engine.update_pipeline_stage(db, ids["foa_id"], "pursuing", user_id)
            await db.commit()

        async with AsyncSessionLocal() as db:
            db.add(ProposalStatusEvent(
                id=new_uuid(), proposal_id=ids["proposal_id"],
                from_status="draft", to_status="submitted", changed_by=user_id,
            ))
            db.add(MemoryRecord(
                id=new_uuid(), org_id=user_id, proposal_id=ids["proposal_id"], agency="NSF",
                outcome="funded", lessons_learned=["Strong technical narrative"],
            ))
            award = Award(
                id=new_uuid(), proposal_id=ids["proposal_id"], foa_id=ids["foa_id"],
                funding_agency="NSF", total_award_value=250000.0, created_by=user_id,
            )
            db.add(award)
            ids["award_id"] = award.id
            await db.commit()

        async with AsyncSessionLocal() as db:
            db.add(AwardCloseout(
                id=new_uuid(), award_id=ids["award_id"], final_report_submitted=True,
                closed_at=datetime.utcnow(),
            ))
            await db.commit()

        async with AsyncSessionLocal() as db:
            return await learning_engine.get_timeline(db, uploaded_by=user_id)

    events = _run(_body())
    event_types = {e["event_type"] for e in events}
    assert event_types == {
        "opportunity_discovered", "stage_change", "proposal_status_change",
        "outcome_recorded", "award_created", "award_closed",
    }
    occurred_ats = [e["occurred_at"] for e in events]
    assert occurred_ats == sorted(occurred_ats, reverse=True)  # newest first


def test_learning_timeline_respects_personal_scope(client):
    """A record uploaded by a different user must not leak into this
    user's personal ("uploaded_by") timeline — same isolation rule as
    FundingIntelligenceEngine.list_pipeline."""
    user_id = _id("user")
    other_id = _id("other-user")
    learning_engine = OrganizationalLearningEngine()

    async def _body():
        async with AsyncSessionLocal() as db:
            await _make_foa(db, uploaded_by=other_id, program_title="Someone Else's Opportunity")
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await learning_engine.get_timeline(db, uploaded_by=user_id)

    assert _run(_body()) == []


# ── HistoricalFundingPerformanceEngine ───────────────────────────────────────

def test_historical_performance_breaks_down_by_agency_range_and_program(client):
    org_id = _id("org")
    fi_engine = FundingIntelligenceEngine()
    perf_engine = HistoricalFundingPerformanceEngine()

    async def _body():
        async with AsyncSessionLocal() as db:
            r1 = await _make_foa(db, org_id=org_id, agency="NSF", grant_type="sbir", estimated_award_ceiling=50000.0)
            r2 = await _make_foa(db, org_id=org_id, agency="NSF", grant_type="sbir", estimated_award_ceiling=750000.0)
            r3 = await _make_foa(db, org_id=org_id, agency="NIH", grant_type="sttr", estimated_award_ceiling=250000.0)
            p1 = await _make_proposal(db, owner_id="user-1", agency="NSF", foa_id=r1.id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            await fi_engine.update_pipeline_stage(db, r1.id, "awarded", "user-1")
            await fi_engine.update_pipeline_stage(db, r3.id, "declined", "user-1")
            await db.commit()
        async with AsyncSessionLocal() as db:
            # Ground-truth award value tied to r1 via foa_id — the engine
            # should report this instead of r1's pre-award estimated ceiling.
            db.add(Award(
                id=new_uuid(), proposal_id=p1.id, foa_id=r1.id,
                funding_agency="NSF", total_award_value=60000.0, created_by="user-1",
            ))
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await perf_engine.get_performance(db, org_id=org_id)

    report = _run(_body())
    assert report["total_opportunities"] == 3
    assert report["total_awarded_funding"] == 60000.0

    by_agency = {g["label"]: g for g in report["by_agency"]}
    assert by_agency["NSF"]["total"] == 2
    assert by_agency["NSF"]["awarded"] == 1
    assert by_agency["NIH"]["lost"] == 1
    assert by_agency["NIH"]["win_rate"] == 0.0

    by_range = {g["label"]: g for g in report["by_funding_range"]}
    assert by_range["Under $100K"]["total"] == 1
    assert by_range["$100K–$500K"]["total"] == 1
    assert by_range["$500K–$1M"]["total"] == 1

    by_program = {g["label"]: g for g in report["by_program_type"]}
    assert by_program["sbir"]["total"] == 2
    assert by_program["sttr"]["total"] == 1


def test_historical_performance_unknown_funding_range_for_missing_ceiling(client):
    org_id = _id("org")
    perf_engine = HistoricalFundingPerformanceEngine()

    async def _body():
        async with AsyncSessionLocal() as db:
            await _make_foa(db, org_id=org_id, estimated_award_ceiling=None)
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await perf_engine.get_performance(db, org_id=org_id)

    report = _run(_body())
    by_range = {g["label"]: g for g in report["by_funding_range"]}
    assert by_range["Unknown"]["total"] == 1
