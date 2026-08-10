"""
Company Profile router (backend/routers/profile.py) — exercised through the
real HTTP endpoints.

Covers the default-empty-profile shape and a full round-trip save/reload,
including the Firm Identity/Address and Business Official/Authorized
Contract Negotiator fields added after reviewing NASA's SBIR/STTR ProSAMS
forms (see docs/ARCHITECTURE.md's Company Profile addendum) — those fields
previously didn't exist anywhere in the schema, so a regression here would
silently drop them again.
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
    return {"email": email, "user_id": resp.json()["id"], "headers": {"Authorization": f"Bearer {token}"}}


def _create_org(client, owner_headers: dict) -> str:
    resp = client.post("/api/v1/organizations/", json={"name": f"Org {uuid.uuid4().hex[:8]}"}, headers=owner_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _invite_member(client, org_id: str, owner_headers: dict, email: str, role: str) -> None:
    resp = client.post(f"/api/v1/organizations/{org_id}/invite", json={"email": email, "role": role}, headers=owner_headers)
    assert resp.status_code == 200, resp.text


def test_get_profile_before_any_save_returns_empty_defaults(client, registered_user):
    resp = client.get("/api/v1/profile/", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pi_name"] is None
    assert body["ein_tax_id"] is None
    assert body["bo_name"] is None
    assert body["acn_name"] is None
    assert body["team_members"] == []
    assert body["partners"] == []


def test_save_and_reload_profile_round_trips_all_new_fields(client, registered_user):
    payload = {
        "organization_name": "NanoResearch Inc.",
        "industry": "Biomedical Technology",
        "uei_number": "ABC123DEF456",
        "cage_code": "1A2B3",
        "ein_tax_id": "12-3456789",
        "duns_number": "123456789",
        "firm_street": "123 Innovation Drive",
        "firm_apt_suite": "Suite 200",
        "firm_city": "Atlanta",
        "firm_state": "GA",
        "firm_zip": "30301-1234",
        "firm_phone": "555-123-4567",
        "pi_name": "Dr. Jane Smith",
        "pi_credentials": "Ph.D. Biomedical Engineering",
        "pi_email": "jane.smith@example.com",
        "pi_phone": "555-987-6543",
        "bo_name": "Jenn Eng",
        "bo_title": "Chief Financial Officer",
        "bo_email": "jenn.eng@example.com",
        "bo_phone": "555-111-2222",
        "acn_name": "Atticus Eng",
        "acn_title": "Contracts Manager",
        "acn_email": "atticus.eng@example.com",
        "acn_phone": "555-333-4444",
        "team_members": [{
            "name": "Dr. Robert Chen", "title": "Senior Scientist", "role": "Co-PI",
            "labor_category": "Senior Scientist", "education_level": "doctorate",
        }],
        "partners": [{
            "name": "MIT Lincoln Laboratory", "type": "research_institution",
            "contact_phone": "555-555-5555", "contact_email": "contact@mit.example.edu",
            "has_letter_of_commitment": True, "include_in_ga": False, "is_foreign_vendor": False,
        }],
    }
    resp = client.put("/api/v1/profile/", json=payload, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    saved = resp.json()
    assert saved["ein_tax_id"] == "12-3456789"
    assert saved["firm_city"] == "Atlanta"
    assert saved["bo_name"] == "Jenn Eng"
    assert saved["acn_email"] == "atticus.eng@example.com"
    assert saved["pi_email"] == "jane.smith@example.com"
    assert saved["team_members"][0]["labor_category"] == "Senior Scientist"
    assert saved["team_members"][0]["education_level"] == "doctorate"
    assert saved["partners"][0]["has_letter_of_commitment"] is True
    assert saved["partners"][0]["is_foreign_vendor"] is False

    reloaded = client.get("/api/v1/profile/", headers=registered_user["headers"])
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["bo_title"] == "Chief Financial Officer"
    assert reloaded.json()["duns_number"] == "123456789"


def test_profile_requires_auth(client):
    resp = client.get("/api/v1/profile/")
    assert resp.status_code == 401


# --- Funding Opportunity Intelligence, Phase 2 — org-scoped profile -------
# OrgContextDB.user_id is no longer unique (see models/db_models.py), and
# routers/profile.py now accepts an `org_id` query param to read/write the
# org-shared Funding Intelligence Profile instead of the caller's personal
# one. These tests cover: the new Phase 2 fields round-trip, personal vs.
# org profiles staying independent, view-requires-membership, and
# edit-requires-manage_company_profile (owner/editor, not viewer).

def test_save_and_reload_funding_intelligence_profile_fields(client, registered_user):
    payload = {
        "organization_name": "NanoResearch, Inc",
        "mission_statement": "Advancing nanomaterial sensor technology for industrial applications.",
        "industries": ["Nanotechnology", "Sensors"],
        "certifications": ["Small Business", "8(a)"],
        "naics_codes": ["541715"],
        "service_geography": ["Nationwide"],
        "funding_preferences": {"min_award": 50000, "max_award": 300000, "preferred_agencies": ["NSF"]},
        "entity_type": "small business",
    }
    resp = client.put("/api/v1/profile/", json=payload, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    saved = resp.json()
    assert saved["mission_statement"] == payload["mission_statement"]
    assert saved["industries"] == payload["industries"]
    assert saved["certifications"] == payload["certifications"]
    assert saved["naics_codes"] == payload["naics_codes"]
    assert saved["service_geography"] == payload["service_geography"]
    assert saved["funding_preferences"] == payload["funding_preferences"]
    assert saved["entity_type"] == "small business"
    assert saved["org_id"] is None

    reloaded = client.get("/api/v1/profile/", headers=registered_user["headers"])
    assert reloaded.json()["mission_statement"] == payload["mission_statement"]


def test_org_profile_is_independent_of_personal_profile(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])

    client.put("/api/v1/profile/", json={"organization_name": "My Personal Org"}, headers=registered_user["headers"])
    client.put(f"/api/v1/profile/?org_id={org_id}", json={"organization_name": "The Shared Org Profile"}, headers=registered_user["headers"])

    personal = client.get("/api/v1/profile/", headers=registered_user["headers"]).json()
    shared = client.get(f"/api/v1/profile/?org_id={org_id}", headers=registered_user["headers"]).json()

    assert personal["organization_name"] == "My Personal Org"
    assert personal["org_id"] is None
    assert shared["organization_name"] == "The Shared Org Profile"
    assert shared["org_id"] == org_id


def test_any_member_can_view_org_profile(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    viewer = _register_and_login(client, "viewer")
    _invite_member(client, org_id, registered_user["headers"], viewer["email"], "viewer")

    client.put(f"/api/v1/profile/?org_id={org_id}", json={"organization_name": "Shared"}, headers=registered_user["headers"])

    resp = client.get(f"/api/v1/profile/?org_id={org_id}", headers=viewer["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["organization_name"] == "Shared"


def test_viewing_org_profile_requires_membership(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    outsider = _register_and_login(client, "outsider")

    resp = client.get(f"/api/v1/profile/?org_id={org_id}", headers=outsider["headers"])
    assert resp.status_code == 403


def test_viewer_cannot_edit_org_profile(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    viewer = _register_and_login(client, "viewer")
    _invite_member(client, org_id, registered_user["headers"], viewer["email"], "viewer")

    resp = client.put(f"/api/v1/profile/?org_id={org_id}", json={"organization_name": "Hacked"}, headers=viewer["headers"])
    assert resp.status_code == 403


def test_editor_can_edit_org_profile(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    editor = _register_and_login(client, "editor")
    _invite_member(client, org_id, registered_user["headers"], editor["email"], "editor")

    resp = client.put(f"/api/v1/profile/?org_id={org_id}", json={"organization_name": "Edited by Editor"}, headers=editor["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["organization_name"] == "Edited by Editor"
