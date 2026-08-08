"""
Supporting Documents router (post-Phase-3 addendum — Communication/
Partnership document generators, see engines/supporting_documents_engine.py)
— exercised through the real HTTP endpoints.

Only the type registry and the validation paths that short-circuit before
any OpenAI call are covered here (unknown doc_type, permission gating,
proposal ownership) — the actual generation path needs a real OpenAI call
and is intentionally out of scope for this suite, same policy as every
other AI-generation endpoint in this codebase (see conftest.py's module
docstring and test_scope_of_work_api.py's identical carve-out).
"""
from __future__ import annotations

import uuid


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


def _proposal_payload(**overrides) -> dict:
    payload = {
        "title": "Rural Broadband Expansion Initiative",
        "agency": "USDA",
        "phase": "phase_i",
        "grant_type": "sbir",
        "org_context": {"organization_name": "Acme Research", "industry": "Telecom"},
        "research_focus": "Fixed-wireless broadband delivery in low-density rural areas",
        "innovation_description": "A mesh-relay antenna array reducing per-household equipment cost",
    }
    payload.update(overrides)
    return payload


def _create_proposal(client, headers: dict) -> str:
    resp = client.post("/api/v1/proposals/", json=_proposal_payload(), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["proposal_id"]


# ── Type registry ────────────────────────────────────────────────────────────

def test_list_supporting_document_types(client, registered_user):
    resp = client.get("/api/v1/supporting-document-types", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    types = {t["key"]: t for t in resp.json()}
    assert set(types) == {
        "cover_letter", "letter_of_inquiry", "concept_paper",
        "letter_of_support", "letter_of_commitment", "mou",
        "logic_model", "me_plan", "sustainability_plan",
        "risk_management_plan", "data_management_plan", "project_management_plan",
        "capability_statement",
    }
    assert types["cover_letter"]["category"] == "Communication"
    assert types["letter_of_inquiry"]["category"] == "Communication"
    assert types["concept_paper"]["category"] == "Communication"
    assert types["letter_of_support"]["category"] == "Partnership"
    assert types["letter_of_commitment"]["category"] == "Partnership"
    assert types["mou"]["category"] == "Partnership"
    assert types["mou"]["label"] == "Memorandum of Understanding (MOU)"
    assert types["logic_model"]["category"] == "Planning"
    assert types["me_plan"]["category"] == "Planning"
    assert types["sustainability_plan"]["category"] == "Planning"
    assert types["risk_management_plan"]["category"] == "Planning"
    assert types["data_management_plan"]["category"] == "Planning"
    assert types["project_management_plan"]["category"] == "Planning"
    assert types["capability_statement"]["category"] == "Organizational"


def test_supporting_document_types_requires_auth(client):
    resp = client.get("/api/v1/supporting-document-types")
    assert resp.status_code == 401


# ── Generation validation (no real AI call) ─────────────────────────────────

def test_generate_supporting_document_rejects_unknown_type(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(
        f"/api/v1/organizations/{org_id}/documents/generate-supporting",
        json={"doc_type": "not_a_real_type", "proposal_id": "does-not-matter"},
        headers=registered_user["headers"],
    )
    # doc_type is validated before the proposal is ever looked up.
    assert resp.status_code == 400
    assert "Unknown supporting document type" in resp.json()["detail"]


def test_generate_supporting_document_requires_manage_documents_permission(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    viewer = _register_and_login(client, "viewer")
    _invite_member(client, org_id, registered_user["headers"], viewer["email"], "viewer")

    resp = client.post(
        f"/api/v1/organizations/{org_id}/documents/generate-supporting",
        json={"doc_type": "cover_letter", "proposal_id": "does-not-matter"},
        headers=viewer["headers"],
    )
    # Permission is checked before the (nonexistent) proposal is looked up.
    assert resp.status_code == 403


def test_generate_supporting_document_requires_proposal_ownership(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    other = _register_and_login(client, "other")
    other_proposal_id = _create_proposal(client, other["headers"])

    resp = client.post(
        f"/api/v1/organizations/{org_id}/documents/generate-supporting",
        json={"doc_type": "cover_letter", "proposal_id": other_proposal_id},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 404
