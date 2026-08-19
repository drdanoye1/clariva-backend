"""
Logic Model Chart Generator (Development Brief 2026-08-14) — exercised
through the real HTTP endpoints.

Same carve-out as test_supporting_documents_api.py: the actual structured-
JSON generation/regeneration paths need a real OpenAI call and are out of
scope here. What IS covered without any AI call:
  - publish_version's auto-flatten of structured_data -> content (Phase 6,
    routers/documents_library.py::publish_version).
  - the two editing endpoints' validation ordering (doc type / proposal
    link / structured-data presence / stage-name and framework-name
    validity), all of which run *before* any catalog charge or OpenAI call
    — same "cheap checks before spending credits" pattern generate_
    supporting_document uses for doc_type.
  - that doc_logic_model_stage_regen is a real, priced catalog entry: a
    freshly-registered org has no balance and no complimentary entitlement
    for it, so a *valid* regenerate-stage/switch-framework request reaches
    catalog_engine.consume() and 402s there — proving the whole request
    pipeline (permissions, doc lookup, proposal lookup, stage/framework
    validation, catalog lookup) runs cleanly up to the point an OpenAI call
    would actually happen.
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


def _invite_member(client, org_id: str, owner_headers: dict, email: str, role: str) -> None:
    resp = client.post(f"/api/v1/organizations/{org_id}/invite", json={"email": email, "role": role}, headers=owner_headers)
    assert resp.status_code == 200, resp.text


def _drain_balance(org_id: str) -> None:
    """A freshly created org actually starts with a $100 complimentary AI
    Services balance (credit_engine.DEFAULT_STARTING_BALANCE) — plenty to
    cover a single doc_logic_model_stage_regen charge ($8-$10). The
    "reaches billing gate" tests below want to observe the charge attempt
    itself (proving the catalog entry + permission/validation chain all
    work) without a real balance masking that behind a successful charge
    that would then go on to call OpenAI — so drain it first, same pattern
    test_service_catalog.py's test_consume_402_when_no_entitlement_and_
    insufficient_balance uses."""
    async def _body():
        async with AsyncSessionLocal() as db:
            credit_engine = CreditEngine()
            ledger = await credit_engine.get_or_create_ledger(db, org_id)
            ledger.balance = 0.0
            await db.commit()
    asyncio.run(_body())


def _create_proposal(client, headers: dict) -> str:
    resp = client.post("/api/v1/proposals/", json={
        "title": "Community Energy Resilience Initiative",
        "agency": "DOE",
        "phase": "phase_i",
        "grant_type": "sbir",
        "org_context": {"organization_name": "Acme Research", "industry": "Energy"},
        "research_focus": "Distributed microgrid resilience for rural cooperatives",
        "innovation_description": "A low-cost islanding controller for existing feeders",
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["proposal_id"]


def _create_document(client, org_id: str, headers: dict, **overrides) -> dict:
    payload = {"title": "Logic Model", "library_type": "logic_model"}
    payload.update(overrides)
    resp = client.post(f"/api/v1/organizations/{org_id}/documents", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


_SAMPLE_STANDARD_DATA = {
    "framework": "standard",
    "title": "Community Energy Resilience Initiative",
    "inputs": ["Grant funding", "Utility cooperative partners", "Engineering staff time"],
    "activities": ["Design islanding controller", "Field-test on 3 feeders", "Train co-op linemen"],
    "outputs": ["Working controller prototype", "3 completed field tests", "Trained lineman cohort"],
    "outcomes": ["Faster outage recovery on test feeders", "Increased co-op technical capacity"],
    "impact": ["More resilient rural electric grid"],
}


def _publish_structured_version(client, document_id: str, headers: dict, data: dict = None) -> dict:
    resp = client.post(
        f"/api/v1/documents/{document_id}/versions",
        json={"structured_data": data or _SAMPLE_STANDARD_DATA, "framework": (data or _SAMPLE_STANDARD_DATA)["framework"]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Phase 6: publish_version auto-flatten ───────────────────────────────────

def test_publish_structured_version_auto_derives_content(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"])

    version = _publish_structured_version(client, doc["id"], registered_user["headers"])
    assert version["structured_data"]["framework"] == "standard"
    assert version["framework"] == "standard"
    # No `content` was sent — the router should have derived it from
    # structured_data via flatten_logic_model_to_text().
    assert version["content"]
    assert "Community Energy Resilience Initiative" in version["content"]
    assert "Grant funding" in version["content"]
    assert "Faster outage recovery on test feeders" in version["content"]


def test_publish_structured_version_respects_explicit_content(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"])
    resp = client.post(
        f"/api/v1/documents/{doc['id']}/versions",
        json={"structured_data": _SAMPLE_STANDARD_DATA, "framework": "standard", "content": "Hand-typed override"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["content"] == "Hand-typed override"


# ── Editing endpoints: validation ordering (no AI, no charge reached) ───────

def test_regenerate_stage_rejects_non_logic_model_document(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"], library_type="document", title="Plain Doc")

    resp = client.post(
        f"/api/v1/documents/{doc['id']}/logic-model/regenerate-stage",
        json={"stage": "inputs"}, headers=registered_user["headers"],
    )
    assert resp.status_code == 400
    assert "Logic Model documents" in resp.json()["detail"]


def test_regenerate_stage_rejects_document_with_no_proposal_link(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"])
    _publish_structured_version(client, doc["id"], registered_user["headers"])

    resp = client.post(
        f"/api/v1/documents/{doc['id']}/logic-model/regenerate-stage",
        json={"stage": "inputs"}, headers=registered_user["headers"],
    )
    assert resp.status_code == 400
    assert "isn't linked to a proposal" in resp.json()["detail"]


def test_regenerate_stage_rejects_legacy_document_with_no_structured_data(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"], proposal_id=proposal_id, content="Five-paragraph legacy narrative.")

    resp = client.post(
        f"/api/v1/documents/{doc['id']}/logic-model/regenerate-stage",
        json={"stage": "inputs"}, headers=registered_user["headers"],
    )
    assert resp.status_code == 400
    assert "no structured chart data yet" in resp.json()["detail"]


def test_regenerate_stage_rejects_unknown_stage_name(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"], proposal_id=proposal_id)
    _publish_structured_version(client, doc["id"], registered_user["headers"])

    resp = client.post(
        f"/api/v1/documents/{doc['id']}/logic-model/regenerate-stage",
        json={"stage": "not_a_real_stage"}, headers=registered_user["headers"],
    )
    assert resp.status_code == 400
    assert "not a stage in the standard framework" in resp.json()["detail"]


def test_regenerate_stage_requires_manage_documents_permission(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"], proposal_id=proposal_id)
    _publish_structured_version(client, doc["id"], registered_user["headers"])

    viewer = _register_and_login(client, "viewer")
    _invite_member(client, org_id, registered_user["headers"], viewer["email"], "viewer")

    resp = client.post(
        f"/api/v1/documents/{doc['id']}/logic-model/regenerate-stage",
        json={"stage": "inputs"}, headers=viewer["headers"],
    )
    assert resp.status_code == 403


def test_regenerate_stage_with_valid_input_reaches_billing_gate(client, registered_user):
    """A fully valid request (real doc, real proposal, real structured_data,
    real stage name) sails through every validation check and reaches
    catalog_engine.consume() — proving doc_logic_model_stage_regen is a
    real, priced catalog entry. A freshly-registered org has no balance and
    no complimentary entitlement for it, so this 402s before ever calling
    OpenAI."""
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"], proposal_id=proposal_id)
    _publish_structured_version(client, doc["id"], registered_user["headers"])
    _drain_balance(org_id)

    resp = client.post(
        f"/api/v1/documents/{doc['id']}/logic-model/regenerate-stage",
        json={"stage": "inputs"}, headers=registered_user["headers"],
    )
    assert resp.status_code == 402, resp.text


def test_switch_framework_rejects_unknown_framework(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"], proposal_id=proposal_id)
    _publish_structured_version(client, doc["id"], registered_user["headers"])

    resp = client.post(
        f"/api/v1/documents/{doc['id']}/logic-model/switch-framework",
        json={"framework": "quantum"}, headers=registered_user["headers"],
    )
    assert resp.status_code == 400
    assert "Unknown framework" in resp.json()["detail"]


def test_switch_framework_rejects_same_framework(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"], proposal_id=proposal_id)
    _publish_structured_version(client, doc["id"], registered_user["headers"])  # framework="standard"

    resp = client.post(
        f"/api/v1/documents/{doc['id']}/logic-model/switch-framework",
        json={"framework": "standard"}, headers=registered_user["headers"],
    )
    assert resp.status_code == 400
    assert "already using the standard framework" in resp.json()["detail"]


def test_switch_framework_with_valid_input_reaches_billing_gate(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    doc = _create_document(client, org_id, registered_user["headers"], proposal_id=proposal_id)
    _publish_structured_version(client, doc["id"], registered_user["headers"])  # framework="standard"
    _drain_balance(org_id)

    resp = client.post(
        f"/api/v1/documents/{doc['id']}/logic-model/switch-framework",
        json={"framework": "extended"}, headers=registered_user["headers"],
    )
    assert resp.status_code == 402, resp.text
