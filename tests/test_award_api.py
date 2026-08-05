"""
Award & Project Management router — exercised through real HTTP endpoints.

The AI report-narrative endpoint needs a real OpenAI call and is
intentionally out of scope here, same policy as every other AI-generation
endpoint in this suite (see conftest.py's module docstring). Everything
else — award creation/access control, budget burn-rate, compliance
checklist, amendments decided through the existing generic
/api/v1/approvals/{id}/decide endpoint, issues, execution status, KPI
performance, the deterministic report view, closeout, and renewal — is
covered through the real router.
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


def _share_proposal(client, org_id: str, proposal_id: str, owner_headers: dict) -> None:
    resp = client.post(f"/api/v1/organizations/{org_id}/proposals", json={"proposal_id": proposal_id}, headers=owner_headers)
    assert resp.status_code == 200, resp.text


def _proposal_payload(**overrides) -> dict:
    payload = {
        "title": "Precision Agriculture Sensor Network",
        "agency": "NSF", "phase": "phase_i", "grant_type": "sbir",
        "org_context": {"organization_name": "Acme Research", "industry": "AgTech"},
        "research_focus": "Low-power soil sensors",
        "innovation_description": "A mesh-networked sensor array",
    }
    payload.update(overrides)
    return payload


def _create_proposal(client, headers: dict) -> str:
    resp = client.post("/api/v1/proposals/", json=_proposal_payload(), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["proposal_id"]


def _create_award(client, headers: dict, proposal_id: str, **overrides) -> dict:
    body = {"proposal_id": proposal_id, "funding_agency": "NSF", "award_number": "NSF-9999", "link_budget": False}
    body.update(overrides)
    resp = client.post("/api/v1/awards", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── Access control ───────────────────────────────────────────────────────────

def test_owner_can_create_and_get_award(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)
    assert award["proposal_id"] == proposal_id
    assert award["status"] == "active"

    resp = client.get(f"/api/v1/awards/{award['id']}", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["award_number"] == "NSF-9999"


def test_duplicate_award_for_same_proposal_rejected(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    _create_award(client, registered_user["headers"], proposal_id)
    resp = client.post(
        "/api/v1/awards",
        json={"proposal_id": proposal_id, "funding_agency": "NSF", "link_budget": False},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 400


def test_unrelated_user_cannot_view_unshared_award(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)
    other = _register_and_login(client, "outsider")

    resp = client.get(f"/api/v1/awards/{award['id']}", headers=other["headers"])
    assert resp.status_code == 403


def test_org_shared_award_editor_can_edit_viewer_cannot(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    _share_proposal(client, org_id, proposal_id, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    editor = _register_and_login(client, "editor")
    _invite_member(client, org_id, registered_user["headers"], editor["email"], "editor")
    viewer = _register_and_login(client, "viewer")
    _invite_member(client, org_id, registered_user["headers"], viewer["email"], "viewer")

    resp = client.patch(f"/api/v1/awards/{award['id']}", json={"award_number": "NSF-EDITED"}, headers=editor["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["award_number"] == "NSF-EDITED"

    resp = client.patch(f"/api/v1/awards/{award['id']}", json={"award_number": "NSF-BLOCKED"}, headers=viewer["headers"])
    assert resp.status_code == 403

    resp = client.get(f"/api/v1/awards/{award['id']}", headers=viewer["headers"])
    assert resp.status_code == 200


# ── Budget administration / burn-rate ───────────────────────────────────────

def test_expenditures_and_budget_status(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    budget_resp = client.put(
        f"/api/v1/budget/{proposal_id}",
        json={"other_direct": [{"category": "Materials", "description": "Sensors", "cost": 20000}]},
        headers=registered_user["headers"],
    )
    assert budget_resp.status_code == 200, budget_resp.text

    award = _create_award(client, registered_user["headers"], proposal_id, link_budget=True)
    assert award["budget_record_id"] is not None

    resp = client.post(
        f"/api/v1/awards/{award['id']}/expenditures",
        json={"category": "other_direct", "description": "Sensor batch 1", "amount": 5000.0},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text

    status_resp = client.get(f"/api/v1/awards/{award['id']}/budget-status", headers=registered_user["headers"])
    assert status_resp.status_code == 200, status_resp.text
    body = status_resp.json()
    assert body["baseline_total_cost"] == 20000.0
    assert body["total_expended"] == 5000.0
    assert body["burn_rate_pct"] == 25.0


# ── Compliance checklist ─────────────────────────────────────────────────────

def test_compliance_item_crud(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    resp = client.post(
        f"/api/v1/awards/{award['id']}/compliance",
        json={"obligation": "Submit quarterly progress report", "category": "reporting"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    item_id = resp.json()["id"]

    resp = client.patch(f"/api/v1/awards/compliance/{item_id}", json={"status": "complete"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "complete"
    assert resp.json()["completed_at"] is not None

    resp = client.get(f"/api/v1/awards/{award['id']}/compliance", headers=registered_user["headers"])
    assert len(resp.json()) == 1

    resp = client.delete(f"/api/v1/awards/compliance/{item_id}", headers=registered_user["headers"])
    assert resp.status_code == 200

    resp = client.get(f"/api/v1/awards/{award['id']}/compliance", headers=registered_user["headers"])
    assert resp.json() == []


# ── Amendments (decided through the existing generic approvals endpoint) ───

def test_amendment_decided_via_existing_approvals_endpoint_applies_changes(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    _share_proposal(client, org_id, proposal_id, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id, total_award_value=100000.0)

    approver = _register_and_login(client, "approver")
    _invite_member(client, org_id, registered_user["headers"], approver["email"], "owner")

    resp = client.post(
        f"/api/v1/awards/{award['id']}/amendments",
        json={
            "amendment_type": "budget", "description": "Increase award value to cover added scope",
            "effective_changes": {"total_award_value": 150000.0}, "approver_id": approver["user_id"],
        },
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    amendment = resp.json()
    assert amendment["status"] == "pending"

    decide_resp = client.post(
        f"/api/v1/approvals/{amendment['approval_request_id']}/decide",
        json={"approved": True, "decision_notes": "Approved"},
        headers=approver["headers"],
    )
    assert decide_resp.status_code == 200, decide_resp.text

    award_resp = client.get(f"/api/v1/awards/{award['id']}", headers=registered_user["headers"])
    assert award_resp.json()["total_award_value"] == 150000.0

    amendments_resp = client.get(f"/api/v1/awards/{award['id']}/amendments", headers=registered_user["headers"])
    assert amendments_resp.json()[0]["status"] == "approved"


# ── Issues ───────────────────────────────────────────────────────────────────

def test_issue_crud(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    resp = client.post(f"/api/v1/awards/{award['id']}/issues", json={"title": "Subcontractor delay", "severity": "high"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    issue_id = resp.json()["id"]

    resp = client.patch(f"/api/v1/awards/issues/{issue_id}", json={"status": "resolved"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "resolved"


# ── Project execution status ────────────────────────────────────────────────

def test_execution_status_endpoint(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    resp = client.get(f"/api/v1/awards/{award['id']}/execution-status", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_work_packages"] == 0


# ── Performance ──────────────────────────────────────────────────────────────

def test_performance_record_crud(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    resp = client.post(
        f"/api/v1/awards/{award['id']}/performance",
        json={"kpi_name": "Farms enrolled", "target": 20, "actual_value": 12, "unit": "farms"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text

    resp = client.get(f"/api/v1/awards/{award['id']}/performance", headers=registered_user["headers"])
    assert len(resp.json()) == 1


# ── Reports ──────────────────────────────────────────────────────────────────

def test_report_endpoint_returns_structured_view(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    resp = client.get(f"/api/v1/awards/{award['id']}/report", params={"report_type": "progress"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["report_type"] == "progress"
    assert body["award"]["id"] == award["id"]
    assert body["narrative"] is None


# ── Closeout ─────────────────────────────────────────────────────────────────

def test_closeout_lifecycle(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    resp = client.get(f"/api/v1/awards/{award['id']}/closeout", headers=registered_user["headers"])
    assert resp.status_code == 404

    resp = client.post(
        f"/api/v1/awards/{award['id']}/closeout",
        json={"deliverables_reconciled": True, "final_report_submitted": True, "lessons_learned": ["Plan procurement earlier"], "outcome_score": 88.0},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deliverables_reconciled"] is True

    award_resp = client.get(f"/api/v1/awards/{award['id']}", headers=registered_user["headers"])
    assert award_resp.json()["status"] == "closed"


# ── Renewal ──────────────────────────────────────────────────────────────────

def test_renewal_creates_pipeline_entry(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    resp = client.post(f"/api/v1/awards/{award['id']}/renewal", json={}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "Renewal" in body["program_title"]
    assert body["pipeline_stage"] == "identified"
    assert body["agency"] == "NSF"
