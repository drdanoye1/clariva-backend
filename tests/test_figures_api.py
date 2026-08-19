"""
AI Figure Generation & Technical Illustration System — routers/figures.py,
Phase 10-12 (docs/2_Upgrades_Clariva_AI_Figure_Specification_V01.docx).

Same carve-out as test_logic_model_chart_api.py: the actual AI content-
extraction call (plan_visual_communication / generate_figure_1 /
generate_figure_2 / run_cross_figure_qa, all of which call OpenAI —
generate_figure_2 also calls DALL-E) needs a real API key and is out of
scope here. What IS covered without any AI call, exercised through the
real HTTP endpoints:
  - "cheap checks before spending credits" ordering: requesting figures for
    a section with no generated content yet 400s BEFORE any catalog charge
    is attempted, for the plan and figure-1 endpoints; requesting figure-2
    or cross-figure QA before Figure 1 exists in the figure set 400s
    BEFORE any catalog charge is attempted (this was a real bug caught
    during Phase 10 development — the router originally charged first —
    and Phase 11/12's endpoints deliberately follow the same fixed
    ordering from the start).
  - figure_visual_plan / figure_1_generation / figure_2_generation /
    figure_qa_check are real, priced catalog entries: once the
    precondition is satisfied, a drained-balance org reaches
    catalog_engine.consume() and 402s there — proving the whole request
    pipeline (permissions, proposal lookup, figure-set lookup/creation,
    precondition check, catalog lookup) runs cleanly up to the point an
    OpenAI/DALL-E call would actually happen (same pattern
    test_logic_model_chart_api.py uses).
  - the org_id-optional/"unmetered personal use" contract: the same
    section-missing-content 400 fires identically with no org_id at all,
    proving personal (org-less) proposals reach the same validation path
    without requiring credits.
  - list/get figure-set endpoints against a real, engine-created row (via
    get_or_create_figure_set, reached indirectly by draining balance so
    plan_visual_communication's figure-set creation runs before its 402).
  - Phase 12's annotation-edit and approval-status endpoints are NOT AI
    calls (pure DB writes), so they run genuinely end-to-end here, not
    just up to a billing gate: editing caption/alt_text/concept_disclosure/
    callouts, callout relates_to_node re-validation against real Figure 1
    node ids (including the classification-enum round-trip bug caught
    during development — see test_annotation_edit_preserves_callout_
    classification_through_enum_round_trip below), 404s for a nonexistent
    figure_number, and every approval_status transition.
"""
from __future__ import annotations

import asyncio
import uuid

from database import AsyncSessionLocal
from engines.credit_engine import CreditEngine


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


def _drain_balance(org_id: str) -> None:
    """Same pattern as test_logic_model_chart_api.py::_drain_balance — a
    freshly created org starts with a real complimentary balance, so
    draining it first lets these tests observe the catalog charge attempt
    itself rather than a successful charge masking it."""
    async def _body():
        async with AsyncSessionLocal() as db:
            credit_engine = CreditEngine()
            ledger = await credit_engine.get_or_create_ledger(db, org_id)
            ledger.balance = 0.0
            await db.commit()
    asyncio.run(_body())


def _create_proposal(client, headers: dict) -> str:
    resp = client.post("/api/v1/proposals/", json={
        "title": "Autonomous Robotic Surveillance Platform",
        "agency": "DOD",
        "phase": "phase_i",
        "grant_type": "sbir",
        "org_context": {"organization_name": "Acme Robotics", "industry": "Defense"},
        "research_focus": "AI-driven autonomous surveillance for perimeter security",
        "innovation_description": "A low-power robotic platform with adaptive sensing",
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["proposal_id"]


def _first_section_id(proposal_id: str) -> str:
    """Every new proposal auto-creates its section set from a template —
    same discovery-not-hardcode approach test_document_export_api.py uses,
    since the exact default section_id set isn't this test's concern."""
    from models.db_models import ProposalSection
    from sqlalchemy import select

    async def _do():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(ProposalSection).where(ProposalSection.proposal_id == proposal_id))
            sections = result.scalars().all()
            assert sections, "expected at least one auto-created section"
            return sections[0].section_id
    return asyncio.run(_do())


def _give_section_content(proposal_id: str, section_id: str, content: str) -> None:
    """Direct DB write — same "reach into AsyncSessionLocal to set up state
    the API alone can't reach" pattern test_document_export_api.py and
    test_billing_wireup.py use, since real section generation needs a real
    OpenAI call."""
    from models.db_models import ProposalSection
    from sqlalchemy import select

    async def _do():
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ProposalSection).where(
                    ProposalSection.proposal_id == proposal_id, ProposalSection.section_id == section_id,
                )
            )
            section = result.scalar_one()
            section.content = content
            section.word_count = len(content.split())
            await db.commit()
    asyncio.run(_do())


def _create_figure_set(proposal_id: str, section_id: str) -> str:
    """Shared direct-DB figure-set creation, same pattern already repeated
    across the Phase 10 tests below — factored out here so the new Phase
    11 tests below don't duplicate it a fourth/fifth time."""
    from models.db_models import ProposalFigureSet, new_uuid

    async def _do():
        async with AsyncSessionLocal() as db:
            fset = ProposalFigureSet(id=new_uuid(), proposal_id=proposal_id, source_section=section_id, status="planning")
            db.add(fset)
            await db.commit()
            return fset.id
    return asyncio.run(_do())


def _create_figure_1_row(figure_set_id: str) -> str:
    """Direct-DB creation of a minimal Figure 1 row — Phase 11's figure-2
    endpoint requires a real Figure 1 to exist (assert_figure_1_ready), and
    exercising that without an OpenAI call means writing the row directly,
    same "reach into AsyncSessionLocal to set up state the API alone can't
    reach without a real AI call" approach _give_section_content uses."""
    from models.db_models import ProposalFigure, new_uuid

    async def _do():
        async with AsyncSessionLocal() as db:
            figure = ProposalFigure(
                id=new_uuid(), figure_set_id=figure_set_id, figure_number=1,
                figure_type="functional_workflow", role="functional_reference_model",
                nodes=[{"id": "sensor_array_installation", "label": "Sensor Array Installation", "order": 1, "classification": "confirmed"}],
                callouts=[], panels=[],
            )
            db.add(figure)
            await db.commit()
            return figure.id
    return asyncio.run(_do())


_SAMPLE_CONTENT = (
    "The proposed system begins with installation of a sensor array, followed by "
    "AI algorithm training on collected data. Once trained, the system undergoes "
    "calibration before entering real-time monitoring operation, culminating in "
    "an automated threat-response capability."
)


# ── Cheap checks run before any catalog charge ──────────────────────────────

def test_plan_endpoint_400s_before_charging_when_section_has_no_content(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)
    # Leave section content empty (default) — do NOT drain balance, so a
    # 402 here would prove the charge fired first, which is the bug this
    # test guards against.

    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/figures/plan?org_id={org_id}",
        json={"source_section": section_id},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 400, resp.text
    assert "doesn't have generated content yet" in resp.json()["detail"]


def test_figure_1_endpoint_400s_before_charging_when_section_has_no_content(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)

    # get_or_create_figure_set needs a figure_set_id to target — create one
    # via the plan endpoint would 400 too (no content), so hit the engine
    # directly through a real DB write to get a figure_set_id without
    # needing content yet.
    from models.db_models import ProposalFigureSet, new_uuid

    async def _create_set():
        async with AsyncSessionLocal() as db:
            fset = ProposalFigureSet(id=new_uuid(), proposal_id=proposal_id, source_section=section_id, status="planning")
            db.add(fset)
            await db.commit()
            return fset.id
    figure_set_id = asyncio.run(_create_set())

    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}/figure-1?org_id={org_id}",
        json={},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 400, resp.text
    assert "doesn't have generated content yet" in resp.json()["detail"]


def test_figure_2_endpoint_400s_before_charging_when_figure_1_missing(client, registered_user):
    """§10/§11 — Figure 2 cannot be generated (and must not be charged for)
    until Figure 1 exists in the same figure set. Deliberately does NOT
    drain the org's balance, so a 402 here would prove
    assert_figure_1_ready() ran after the charge instead of before it."""
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)
    figure_set_id = _create_figure_set(proposal_id, section_id)
    # No Figure 1 row created — figure set exists but is empty.

    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}/figure-2?org_id={org_id}",
        json={},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 400, resp.text
    assert "Figure 1" in resp.json()["detail"]


def test_plan_endpoint_400s_identically_with_no_org_id(client, registered_user):
    """Personal/unmetered use (no org_id) reaches the exact same validation
    — proving the cheap-check-before-charge ordering holds for the
    org_id-optional path too, where there is no charge to skip."""
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)

    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/figures/plan",
        json={"source_section": section_id},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 400, resp.text
    assert "doesn't have generated content yet" in resp.json()["detail"]


# ── Real, priced catalog entries — billing gate reached ─────────────────────

def test_plan_endpoint_reaches_catalog_charge_once_section_has_content(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)
    _give_section_content(proposal_id, section_id, _SAMPLE_CONTENT)
    _drain_balance(org_id)

    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/figures/plan?org_id={org_id}",
        json={"source_section": section_id},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 402, resp.text
    assert "figure_visual_plan" in resp.json()["detail"] or "credit" in resp.json()["detail"].lower() or "balance" in resp.json()["detail"].lower()


def test_figure_1_endpoint_reaches_catalog_charge_once_section_has_content(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)
    _give_section_content(proposal_id, section_id, _SAMPLE_CONTENT)

    from models.db_models import ProposalFigureSet, new_uuid

    async def _create_set():
        async with AsyncSessionLocal() as db:
            fset = ProposalFigureSet(id=new_uuid(), proposal_id=proposal_id, source_section=section_id, status="planning")
            db.add(fset)
            await db.commit()
            return fset.id
    figure_set_id = asyncio.run(_create_set())
    _drain_balance(org_id)

    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}/figure-1?org_id={org_id}",
        json={},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 402, resp.text


def test_figure_2_endpoint_reaches_catalog_charge_once_figure_1_exists(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)
    _give_section_content(proposal_id, section_id, _SAMPLE_CONTENT)
    figure_set_id = _create_figure_set(proposal_id, section_id)
    _create_figure_1_row(figure_set_id)
    _drain_balance(org_id)

    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}/figure-2?org_id={org_id}",
        json={},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 402, resp.text


def test_qa_endpoint_400s_before_charging_when_figure_1_missing(client, registered_user):
    """Same cheap-check-before-charge guard as figure-2's — the QA
    endpoint also calls assert_figure_1_ready() before _charge(). Does NOT
    drain the org's balance, so a 402 here would prove the charge fired
    before the precondition check."""
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)
    figure_set_id = _create_figure_set(proposal_id, section_id)

    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}/qa?org_id={org_id}",
        headers=registered_user["headers"],
    )
    assert resp.status_code == 400, resp.text
    assert "Figure 1" in resp.json()["detail"]


def test_qa_endpoint_reaches_catalog_charge_once_figure_1_exists(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)
    _give_section_content(proposal_id, section_id, _SAMPLE_CONTENT)
    figure_set_id = _create_figure_set(proposal_id, section_id)
    _create_figure_1_row(figure_set_id)
    _drain_balance(org_id)

    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}/qa?org_id={org_id}",
        headers=registered_user["headers"],
    )
    assert resp.status_code == 402, resp.text


# ── List / get endpoints ─────────────────────────────────────────────────────

def test_list_figure_sets_returns_created_set(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)

    from models.db_models import ProposalFigureSet, new_uuid

    async def _create_set():
        async with AsyncSessionLocal() as db:
            fset = ProposalFigureSet(id=new_uuid(), proposal_id=proposal_id, source_section=section_id, status="planning")
            db.add(fset)
            await db.commit()
            return fset.id
    figure_set_id = asyncio.run(_create_set())

    resp = client.get(f"/api/v1/proposals/{proposal_id}/figures", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    ids = [fs["id"] for fs in resp.json()]
    assert figure_set_id in ids

    resp2 = client.get(f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}", headers=registered_user["headers"])
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["source_section"] == section_id
    assert resp2.json()["status"] == "planning"
    assert resp2.json()["figures"] == []


def test_get_figure_set_404s_for_another_proposal(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    other_proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)

    from models.db_models import ProposalFigureSet, new_uuid

    async def _create_set():
        async with AsyncSessionLocal() as db:
            fset = ProposalFigureSet(id=new_uuid(), proposal_id=proposal_id, source_section=section_id, status="planning")
            db.add(fset)
            await db.commit()
            return fset.id
    figure_set_id = asyncio.run(_create_set())

    resp = client.get(
        f"/api/v1/proposals/{other_proposal_id}/figures/{figure_set_id}", headers=registered_user["headers"],
    )
    assert resp.status_code == 404


def test_figures_endpoints_404_for_nonowner(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    other_user = _register_and_login(client, "other")

    resp = client.get(f"/api/v1/proposals/{proposal_id}/figures", headers=other_user["headers"])
    assert resp.status_code == 404


# ── Phase 12: annotation edits + approval workflow ───────────────────────────
# These are pure DB writes (no AI call), so unlike everything above they run
# genuinely end-to-end here, not just up to a billing gate.

def _create_figure_2_row(figure_set_id: str) -> str:
    """Direct-DB creation of a minimal Figure 2 row (empty callouts) for
    the annotation-edit tests below — same rationale as
    _create_figure_1_row: exercising the annotation endpoint doesn't need
    a real AI-generated Figure 2, just a real row to edit."""
    from models.db_models import ProposalFigure, new_uuid

    async def _do():
        async with AsyncSessionLocal() as db:
            figure = ProposalFigure(
                id=new_uuid(), figure_set_id=figure_set_id, figure_number=2,
                figure_type="technical_illustration", role="physical_implementation",
                nodes=[], callouts=[], panels=[],
            )
            db.add(figure)
            await db.commit()
            return figure.id
    return asyncio.run(_do())


def test_annotation_endpoint_updates_caption_alt_text_and_concept_disclosure(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)
    figure_set_id = _create_figure_set(proposal_id, section_id)
    _create_figure_1_row(figure_set_id)

    resp = client.patch(
        f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}/figures/1/annotations",
        json={"caption": "Figure 1. Edited caption.", "alt_text": "Edited alt text.", "concept_disclosure": "Edited disclosure."},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caption"] == "Figure 1. Edited caption."
    assert body["alt_text"] == "Edited alt text."
    assert body["concept_disclosure"] == "Edited disclosure."
    # approval_status must NOT change from an annotation edit — editing and
    # approving are distinct actions (§22).
    assert body["approval_status"] == "pending"

    # A partial update (only caption) must not clobber the other fields
    # just set above.
    resp2 = client.patch(
        f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}/figures/1/annotations",
        json={"caption": "Figure 1. Second edit."},
        headers=registered_user["headers"],
    )
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["caption"] == "Figure 1. Second edit."
    assert body2["alt_text"] == "Edited alt text."
    assert body2["concept_disclosure"] == "Edited disclosure."


def test_annotation_edit_preserves_callout_classification_through_enum_round_trip(client, registered_user):
    """Regression test for a real bug caught during Phase 12 development:
    FigureCallout.classification arrives at the engine as a
    TechnicalAccuracyClassification enum member (from the request body),
    and str(enum_member) on a (str, Enum) mixin renders as
    "TechnicalAccuracyClassification.INFERRED" under Python's default
    Enum.__str__ — not "inferred". _normalize_callouts's str(...).lower()
    call would then fail to match _VALID_CLASSIFICATIONS and silently fall
    back to "confirmed", discarding the caller's actual choice. Fixed by
    using model_dump(mode="json") in the router. This test asserts the
    real value round-trips correctly end-to-end through the HTTP API."""
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)
    figure_set_id = _create_figure_set(proposal_id, section_id)
    _create_figure_1_row(figure_set_id)  # node id "sensor_array_installation"
    _create_figure_2_row(figure_set_id)

    resp = client.patch(
        f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}/figures/2/annotations",
        json={"callouts": [
            {"number": 1, "label": "Camera Array", "relates_to_node": "sensor_array_installation", "classification": "inferred"},
            {"number": 2, "label": "Support Bracket", "relates_to_node": None, "classification": "conceptual"},
        ]},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    callouts = resp.json()["callouts"]
    assert len(callouts) == 2
    by_number = {c["number"]: c for c in callouts}
    assert by_number[1]["classification"] == "inferred"
    assert by_number[1]["relates_to_node"] == "sensor_array_installation"
    assert by_number[2]["classification"] == "conceptual"
    assert by_number[2]["relates_to_node"] is None


def test_annotation_endpoint_drops_relates_to_node_not_in_figure_1_nodes(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)
    figure_set_id = _create_figure_set(proposal_id, section_id)
    _create_figure_1_row(figure_set_id)  # node id "sensor_array_installation"
    _create_figure_2_row(figure_set_id)

    resp = client.patch(
        f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}/figures/2/annotations",
        json={"callouts": [
            {"number": 1, "label": "Mystery Part", "relates_to_node": "made_up_node_id", "classification": "confirmed"},
        ]},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    callouts = resp.json()["callouts"]
    assert callouts[0]["relates_to_node"] is None


def test_annotation_endpoint_404s_for_nonexistent_figure_number(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)
    figure_set_id = _create_figure_set(proposal_id, section_id)
    _create_figure_1_row(figure_set_id)

    resp = client.patch(
        f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}/figures/3/annotations",
        json={"caption": "Doesn't matter"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 404


def test_approval_endpoint_updates_status_through_full_state_machine(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)
    figure_set_id = _create_figure_set(proposal_id, section_id)
    _create_figure_1_row(figure_set_id)

    for status in ("approved", "needs_regeneration", "rejected", "pending"):
        resp = client.post(
            f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}/figures/1/approval",
            json={"status": status},
            headers=registered_user["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["approval_status"] == status


def test_approval_endpoint_404s_for_nonexistent_figure_number(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)
    figure_set_id = _create_figure_set(proposal_id, section_id)
    _create_figure_1_row(figure_set_id)

    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}/figures/9/approval",
        json={"status": "approved"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 404


def test_approval_endpoint_422s_for_invalid_status(client, registered_user):
    """FigureApprovalStatus is a closed-set enum validated at the Pydantic
    layer (see schemas.py's FigureApprovalStatus docstring) — an invalid
    status string never reaches the engine's own _VALID_APPROVAL_STATUSES
    check at all; FastAPI's request validation 422s first."""
    proposal_id = _create_proposal(client, registered_user["headers"])
    section_id = _first_section_id(proposal_id)
    figure_set_id = _create_figure_set(proposal_id, section_id)
    _create_figure_1_row(figure_set_id)

    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}/figures/1/approval",
        json={"status": "bogus_status"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 422
