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
