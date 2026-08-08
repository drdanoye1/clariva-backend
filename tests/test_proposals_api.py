"""
Proposals router — create/list/get/update/delete.
Only covers paths that don't call OpenAI (generation/scoring/review are
exercised at the engine level in their own test files, not here).
"""
from __future__ import annotations

from database import AsyncSessionLocal
from models.db_models import FOARecord, new_uuid


def _org_context() -> dict:
    return {
        "organization_name": "Acme Research",
        "industry": "Biotech",
        "core_technologies": ["gene editing"],
        "prior_sbir_experience": False,
    }


def _proposal_payload(**overrides) -> dict:
    payload = {
        "title": "Novel Gene Therapy Platform",
        "agency": "NSF",
        "phase": "phase_i",
        "grant_type": "sbir",
        "org_context": _org_context(),
        "research_focus": "CRISPR-based delivery mechanisms",
        "innovation_description": "A new lipid nanoparticle delivery system",
    }
    payload.update(overrides)
    return payload


def test_create_proposal_scaffolds_sections(client, registered_user):
    resp = client.post(
        "/api/v1/proposals/",
        json=_proposal_payload(),
        headers=registered_user["headers"],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "Novel Gene Therapy Platform"
    assert body["status"] == "draft"
    assert len(body["sections"]) > 0


def test_create_proposal_requires_auth(client):
    resp = client.post("/api/v1/proposals/", json=_proposal_payload())
    assert resp.status_code == 401


async def _insert_foa(**overrides) -> str:
    defaults = dict(
        id=new_uuid(), agency="NSF", program_title="Test Opportunity", phase="phase_i",
        grant_type="sbir",
    )
    defaults.update(overrides)
    async with AsyncSessionLocal() as db:
        db.add(FOARecord(**defaults))
        await db.commit()
    return defaults["id"]


def _insert_foa_sync(**overrides) -> str:
    import asyncio
    return asyncio.run(_insert_foa(**overrides))


def test_create_proposal_from_foa_with_no_parsed_template(client, registered_user):
    """Regression test: FOARecord.parsed_template is None for any FOA that
    didn't come through the upload/parse-text/parse-url flow — e.g. rows
    created by the Grants.gov/SAM.gov sync (funding_intelligence_engine.py),
    award renewals (award_engine.py), or the public API (routers/public_api.py).
    create_proposal used to call `.get()` directly on parsed_template and
    500'd with AttributeError for every one of those FOAs; it must fall back
    to treating a missing template as empty instead."""
    foa_id = _insert_foa_sync()  # parsed_template left unset -> None
    resp = client.post(
        "/api/v1/proposals/",
        json=_proposal_payload(foa_id=foa_id),
        headers=registered_user["headers"],
    )
    assert resp.status_code == 201, resp.text
    assert len(resp.json()["sections"]) > 0


def test_list_proposals_only_returns_own_proposals(client, registered_user):
    other_user_email = f"other-{registered_user['email']}"
    client.post(
        "/api/v1/auth/register",
        json={
            "email": other_user_email,
            "password": "OtherPass123!",
            "full_name": "Other User",
            "organization": "Other Org",
        },
    )
    other_login = client.post(
        "/api/v1/auth/login",
        data={"username": other_user_email, "password": "OtherPass123!"},
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['access_token']}"}

    client.post("/api/v1/proposals/", json=_proposal_payload(), headers=registered_user["headers"])
    client.post("/api/v1/proposals/", json=_proposal_payload(title="Other User's Proposal"), headers=other_headers)

    mine = client.get("/api/v1/proposals/", headers=registered_user["headers"]).json()
    theirs = client.get("/api/v1/proposals/", headers=other_headers).json()

    assert all(p["title"] != "Other User's Proposal" for p in mine)
    assert any(p["title"] == "Other User's Proposal" for p in theirs)


def test_get_proposal_not_found_returns_404(client, registered_user):
    resp = client.get("/api/v1/proposals/does-not-exist", headers=registered_user["headers"])
    assert resp.status_code == 404


def test_update_and_delete_proposal(client, registered_user):
    created = client.post(
        "/api/v1/proposals/", json=_proposal_payload(), headers=registered_user["headers"]
    ).json()
    proposal_id = created["proposal_id"]

    updated = client.patch(
        f"/api/v1/proposals/{proposal_id}",
        json={"title": "Renamed Proposal"},
        headers=registered_user["headers"],
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Renamed Proposal"

    deleted = client.delete(f"/api/v1/proposals/{proposal_id}", headers=registered_user["headers"])
    assert deleted.status_code == 204

    gone = client.get(f"/api/v1/proposals/{proposal_id}", headers=registered_user["headers"])
    assert gone.status_code == 404
