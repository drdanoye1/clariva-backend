"""
Funding Opportunity Intelligence, Phase 3 §4.6 — Funding Strategy
Intelligence. Covers engines/funding_strategy_engine.py's pure/
deterministic helpers directly (same "_parse_json"/"_normalize" testing
convention as test_foa_parser.py) plus the two new endpoints on
routers/funding_intelligence.py: GET /funding/strategy (free, read-only)
and POST /funding/strategy/generate (paid).

Per this codebase's standing policy (see conftest.py's module docstring
and test_supporting_documents_api.py's identical carve-out), the actual
GPT-4o call is out of scope for network-free tests — EXCEPT for one
end-to-end test below that monkeypatches
`strategy_engine.client.chat.completions.create` directly (no network
call actually happens), the same "mock the AI-calling method, exercise
everything around it for real" approach test_bulk_qualification_api.py
used to verify bulk-analyze's charge-then-generate flow. That one test is
what actually proves the upsert-not-duplicate persistence behavior and
the "disclaimer/human_in_the_loop_note are always attached even if the
model omits them" normalization guarantee — those can't be verified by
testing _normalize_plan() in isolation alone, since the router/engine
wiring between the OpenAI response and the saved row is exactly what
could silently break.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select

from database import AsyncSessionLocal
from models.db_models import AIServiceTransaction, FundingStrategyPlan, Organization


def _run(coro):
    return asyncio.run(coro)


def _register_and_login(client, label: str) -> dict:
    email = f"{label}-{uuid.uuid4().hex[:10]}@example.com"
    password = "TestPassword123!"
    resp = client.post("/api/v1/auth/register", json={
        "email": email, "password": password,
        "full_name": f"{label.title()} User", "organization": "Test Org",
    })
    assert resp.status_code == 201, resp.text
    login = client.post("/api/v1/auth/login", data={"username": email, "password": password})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return {"email": email, "headers": {"Authorization": f"Bearer {token}"}}


def _create_org(client, owner_headers: dict) -> str:
    resp = client.post("/api/v1/organizations/", json={"name": f"Org {uuid.uuid4().hex[:8]}"}, headers=owner_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _set_org_plan(org_id: str, plan: str) -> None:
    async def _body():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Organization).where(Organization.id == org_id))
            org = result.scalar_one()
            org.plan = plan
            await db.commit()
    _run(_body())


# ── engines/funding_strategy_engine.py pure helpers ─────────────────────────

@pytest.fixture()
def engine():
    from engines.funding_strategy_engine import FundingStrategyEngine
    return FundingStrategyEngine()


def test_parse_json_direct(engine):
    assert engine._parse_json('{"executive_summary": "x"}') == {"executive_summary": "x"}


def test_parse_json_strips_markdown_fence(engine):
    raw = '```json\n{"executive_summary": "x"}\n```'
    assert engine._parse_json(raw) == {"executive_summary": "x"}


def test_parse_json_extracts_embedded_object(engine):
    raw = 'Here is the plan:\n{"executive_summary": "x"}\nHope that helps!'
    assert engine._parse_json(raw) == {"executive_summary": "x"}


def test_parse_json_returns_empty_dict_on_garbage(engine):
    assert engine._parse_json("not json at all") == {}


def test_normalize_plan_fills_in_missing_defaults(engine):
    result = engine._normalize_plan({})
    assert result["executive_summary"] is None
    assert result["priority_agencies_programs"] == []
    assert result["target_funding"] == {"annual_target": None, "rationale": None}
    assert result["quarterly_pursuit_calendar"] == []
    assert result["capability_gaps"] == []
    assert result["partnership_strategy"] == []
    assert result["proposal_resource_plan"] == []
    # Always attached, never trusted to the model.
    assert result["disclaimer"]
    assert result["human_in_the_loop_note"]


def test_normalize_plan_preserves_provided_values(engine):
    result = engine._normalize_plan({
        "executive_summary": "Focus on NSF.",
        "priority_agencies_programs": [{"agency": "NSF", "program_area": "AI", "rationale": "Strong win rate."}],
    })
    assert result["executive_summary"] == "Focus on NSF."
    assert result["priority_agencies_programs"] == [{"agency": "NSF", "program_area": "AI", "rationale": "Strong win rate."}]


def test_normalize_plan_always_overwrites_disclaimer_even_if_model_supplied_one(engine):
    """The disclaimer/human-in-the-loop text is fixed, non-AI-generated
    copy — if the model hallucinates its own version of either field, this
    engine's normalization must still win, same discipline as
    foa_parser.py::_normalize_intelligence_report."""
    result = engine._normalize_plan({
        "disclaimer": "Trust me, this is definitely accurate.",
        "human_in_the_loop_note": "No review needed.",
    })
    assert result["disclaimer"] != "Trust me, this is definitely accurate."
    assert result["human_in_the_loop_note"] != "No review needed."
    assert "AI-generated" in result["disclaimer"]


def test_profile_block_handles_missing_profile(engine):
    assert "No Funding Intelligence Profile" in engine._profile_block(None)


def test_performance_block_is_valid_json(engine):
    block = engine._performance_block({
        "total_opportunities": 10, "win_rate": 0.4, "avg_cycle_time_days": 30,
        "pipeline_value": 500000.0, "total_awarded_funding": 200000.0,
        "by_agency": [{"label": "NSF", "total": 5, "awarded": 2, "lost": 1, "win_rate": 0.67}],
        "by_program_type": [], "by_funding_range": [],
    })
    parsed = json.loads(block)
    assert parsed["total_opportunities"] == 10
    assert parsed["by_agency"][0]["label"] == "NSF"


def test_portfolio_block_is_valid_json(engine):
    block = engine._portfolio_block({
        "recommended_portfolio": [{"foa_id": "abc", "program_title": "X"}],
        "by_stage": {"identified": 3}, "pursuit_capacity": 5,
        "currently_pursuing": 2, "capacity_status": "under", "resource_conflicts": [],
    })
    parsed = json.loads(block)
    assert parsed["pursuit_capacity"] == 5
    assert parsed["recommended_portfolio"][0]["foa_id"] == "abc"


# ── GET /funding/strategy (free, read-only) ─────────────────────────────────

def test_get_strategy_requires_org_id_query_param(client):
    user = _register_and_login(client, "strategy-noorg")
    resp = client.get("/api/v1/funding/strategy", headers=user["headers"])
    assert resp.status_code == 422  # required query param, missing


def test_get_strategy_returns_null_before_generation(client):
    user = _register_and_login(client, "strategy-empty")
    org_id = _create_org(client, user["headers"])
    resp = client.get(f"/api/v1/funding/strategy?org_id={org_id}", headers=user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json() is None


def test_get_strategy_non_member_forbidden(client):
    user = _register_and_login(client, "strategy-owner")
    org_id = _create_org(client, user["headers"])
    stranger = _register_and_login(client, "strategy-stranger")
    resp = client.get(f"/api/v1/funding/strategy?org_id={org_id}", headers=stranger["headers"])
    assert resp.status_code == 403


# ── POST /funding/strategy/generate (paid) ──────────────────────────────────

def test_generate_strategy_requires_org_id_query_param(client):
    user = _register_and_login(client, "strategy-gen-noorg")
    resp = client.post("/api/v1/funding/strategy/generate", headers=user["headers"])
    assert resp.status_code == 422


def test_generate_strategy_non_member_forbidden(client):
    user = _register_and_login(client, "strategy-gen-owner")
    org_id = _create_org(client, user["headers"])
    stranger = _register_and_login(client, "strategy-gen-stranger")
    resp = client.post(f"/api/v1/funding/strategy/generate?org_id={org_id}", headers=stranger["headers"])
    assert resp.status_code == 403


def test_generate_strategy_402_when_balance_exhausted(client):
    """Insufficient-balance short-circuits BEFORE the GPT-4o call, so this
    exercises the router's charge-gating for real without needing to mock
    anything — mirrors test_service_catalog.py's identical drain-then-402
    pattern."""
    from engines.credit_engine import CreditEngine

    user = _register_and_login(client, "strategy-gen-poor")
    org_id = _create_org(client, user["headers"])
    _set_org_plan(org_id, "team")

    async def _drain():
        async with AsyncSessionLocal() as db:
            credit_engine = CreditEngine()
            ledger = await credit_engine.get_or_create_ledger(db, org_id)
            ledger.balance = 0.0
            await db.commit()
    _run(_drain())

    resp = client.post(f"/api/v1/funding/strategy/generate?org_id={org_id}", headers=user["headers"])
    assert resp.status_code == 402


def _fake_plan_json(**overrides) -> str:
    plan = {
        "executive_summary": "Prioritize NSF and NIH; close the IRB gap before Q3.",
        "priority_agencies_programs": [
            {"agency": "NSF", "program_area": "AI/ML", "rationale": "Highest historical win rate."},
        ],
        "target_funding": {"annual_target": "$1M-$2M", "rationale": "Matches current pipeline value."},
        "quarterly_pursuit_calendar": [
            {"quarter": "Q1 2027", "focus": "Submit 2 NSF SBIR Phase I proposals", "notes": "Deadlines in Jan/Feb."},
        ],
        "capability_gaps": [
            {"gap": "No institutional IRB", "impact": "Blocks NIH human-subjects work.", "recommended_action": "Establish an IRB or partner with one."},
        ],
        "partnership_strategy": [
            {"partner_type": "Research university", "rationale": "Adds credibility for NIH submissions.", "target_profile": "R1 university with an active AI lab."},
        ],
        "proposal_resource_plan": [
            {"period": "Q1 2027", "resource_need": "One additional technical writer.", "rationale": "Three concurrent submissions planned."},
        ],
    }
    plan.update(overrides)
    return json.dumps(plan)


def test_generate_strategy_persists_charges_and_normalizes(client, monkeypatch):
    """The one test in this file that mocks the OpenAI-calling method
    directly (strategy_engine.client.chat.completions.create) — see module
    docstring for why this specific integration path is worth the
    exception to the "generation is out of scope" convention."""
    import routers.funding_intelligence as fi_router

    user = _register_and_login(client, "strategy-gen-team")
    org_id = _create_org(client, user["headers"])
    _set_org_plan(org_id, "team")

    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _Msg(content)

    class _Resp:
        def __init__(self, content):
            self.choices = [_Choice(content)]

    # Deliberately omit disclaimer/human_in_the_loop_note from the fake
    # model output to prove _normalize_plan() (not the model) is what
    # attaches them.
    async def fake_create(**kwargs):
        return _Resp(_fake_plan_json())

    monkeypatch.setattr(fi_router.strategy_engine.client.chat.completions, "create", fake_create)

    resp = client.post(f"/api/v1/funding/strategy/generate?org_id={org_id}", headers=user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["org_id"] == org_id
    assert body["executive_summary"].startswith("Prioritize NSF")
    assert body["priority_agencies_programs"][0]["agency"] == "NSF"
    assert body["target_funding"]["annual_target"] == "$1M-$2M"
    assert len(body["quarterly_pursuit_calendar"]) == 1
    assert len(body["capability_gaps"]) == 1
    assert len(body["partnership_strategy"]) == 1
    assert len(body["proposal_resource_plan"]) == 1
    assert body["disclaimer"]
    assert body["human_in_the_loop_note"]

    # Charged once, at the team-plan (subscriber) price.
    async def _transactions():
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AIServiceTransaction).where(
                    AIServiceTransaction.org_id == org_id,
                    AIServiceTransaction.service_key == "funding_strategy_intelligence",
                )
            )
            return result.scalars().all()
    txns = _run(_transactions())
    assert len(txns) == 1
    assert txns[0].price_cents == 3500

    # GET now returns the persisted plan instead of null.
    get_resp = client.get(f"/api/v1/funding/strategy?org_id={org_id}", headers=user["headers"])
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["executive_summary"].startswith("Prioritize NSF")

    # Regenerating overwrites the existing row rather than creating a
    # second one — see FundingStrategyPlan's model docstring for why this
    # is "current state," not a versioned history.
    async def fake_create_v2(**kwargs):
        return _Resp(_fake_plan_json(executive_summary="Updated strategy: pivot to DOE."))
    monkeypatch.setattr(fi_router.strategy_engine.client.chat.completions, "create", fake_create_v2)

    resp2 = client.post(f"/api/v1/funding/strategy/generate?org_id={org_id}", headers=user["headers"])
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["executive_summary"] == "Updated strategy: pivot to DOE."

    async def _plan_rows():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(FundingStrategyPlan).where(FundingStrategyPlan.org_id == org_id))
            return result.scalars().all()
    rows = _run(_plan_rows())
    assert len(rows) == 1
    assert rows[0].plan["executive_summary"] == "Updated strategy: pivot to DOE."
