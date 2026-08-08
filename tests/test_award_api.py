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

Phase D's POST /extract-intelligence also needs a real OpenAI call (twice,
in fact — award/scope extraction and budget extraction) and gets the same
treatment: only its pre-AI-call validation paths (no documents uploaded yet;
access control) are covered below. POST /apply-intelligence makes no AI call
at all — it just persists a given draft — so it's covered fully.
"""
from __future__ import annotations

import uuid

import pytest

import storage


@pytest.fixture(autouse=True)
def _fake_r2(monkeypatch):
    """Phase C added a real storage.upload_file()/get_download_url() call
    into intake_award_document() and list_award_files(). Autouse + module-
    scoped to every test here (not just the intake-document ones) so this
    file never depends on — or hits — real R2, honoring the same "no network
    calls in tests" policy as test_document_export_api.py, regardless of
    whether a developer's local .env happens to have real R2 credentials in
    it (as it will, once Cloudflare R2 setup is complete)."""
    async def fake_upload_file(org_id, category, content, filename, content_type):
        return f"fake/{category}/{filename}"

    async def fake_get_download_url(storage_key, filename=None, expires_in=3600):
        return f"https://example-bucket.r2.example.com/{storage_key}"

    monkeypatch.setattr(storage, "upload_file", fake_upload_file)
    monkeypatch.setattr(storage, "get_download_url", fake_get_download_url)


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


# ── Persisted, human-reviewed reports ───────────────────────────────────────
# POST .../reports (create_report_draft) makes the same real OpenAI call as
# POST .../report/narrative above, so — same policy as every other
# AI-generation endpoint in this suite — the AI call itself is monkeypatched
# at the shared router-module engine instance (see
# test_funding_intelligence_api.py for the identical pattern) rather than
# skipped outright: everything downstream of that call (persistence, edit,
# submit-for-approval, the existing generic approvals/decide endpoint,
# and the export gate) is this phase's actual new code and needs coverage.

def _fake_narrative(monkeypatch, text: str = "A fine report narrative.") -> None:
    import routers.awards as awards_router

    async def fake_generate_report_narrative(report, proposal, additional_context=None):
        return text

    monkeypatch.setattr(awards_router.engine, "generate_report_narrative", fake_generate_report_narrative)


def test_report_draft_edit_submit_approve_export_lifecycle(client, registered_user, monkeypatch):
    _fake_narrative(monkeypatch, "Draft narrative from the AI.")

    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    _share_proposal(client, org_id, proposal_id, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    approver = _register_and_login(client, "report-approver")
    _invite_member(client, org_id, registered_user["headers"], approver["email"], "owner")

    # 1. Generating a report persists a draft, not an ephemeral response.
    create_resp = client.post(
        f"/api/v1/awards/{award['id']}/reports", json={"report_type": "progress"}, headers=registered_user["headers"],
    )
    assert create_resp.status_code == 201, create_resp.text
    report = create_resp.json()
    assert report["status"] == "draft"
    assert report["narrative"] == "Draft narrative from the AI."
    assert report["ai_generated_narrative"] == "Draft narrative from the AI."

    # 2. Human-in-the-loop: nothing is exportable while still a draft.
    export_resp = client.post(f"/api/v1/awards/{award['id']}/reports/{report['id']}/export", headers=registered_user["headers"])
    assert export_resp.status_code == 400

    # 3. A human can edit the AI's narrative before submitting it.
    edit_resp = client.patch(
        f"/api/v1/awards/{award['id']}/reports/{report['id']}", json={"narrative": "Human-edited narrative."},
        headers=registered_user["headers"],
    )
    assert edit_resp.status_code == 200, edit_resp.text
    assert edit_resp.json()["narrative"] == "Human-edited narrative."
    assert edit_resp.json()["ai_generated_narrative"] == "Draft narrative from the AI."  # original preserved for audit

    # 4. Submit for approval — routes through the same generic ApprovalRequest
    # workflow amendments use, not a parallel system.
    submit_resp = client.post(
        f"/api/v1/awards/{award['id']}/reports/{report['id']}/submit",
        json={"approver_id": approver["user_id"]}, headers=registered_user["headers"],
    )
    assert submit_resp.status_code == 200, submit_resp.text
    submitted = submit_resp.json()
    assert submitted["status"] == "pending_approval"
    approval_request_id = submitted["approval_request_id"]
    assert approval_request_id

    # A pending report's narrative can no longer be edited.
    late_edit_resp = client.patch(
        f"/api/v1/awards/{award['id']}/reports/{report['id']}", json={"narrative": "Too late."},
        headers=registered_user["headers"],
    )
    assert late_edit_resp.status_code == 400

    # Still not exportable while pending.
    export_resp = client.post(f"/api/v1/awards/{award['id']}/reports/{report['id']}/export", headers=registered_user["headers"])
    assert export_resp.status_code == 400

    # 5. Decide the approval through the existing generic endpoint.
    decide_resp = client.post(
        f"/api/v1/approvals/{approval_request_id}/decide",
        json={"approved": True, "decision_notes": "Looks good."}, headers=approver["headers"],
    )
    assert decide_resp.status_code == 200, decide_resp.text

    record_resp = client.get(f"/api/v1/awards/{award['id']}/reports/{report['id']}", headers=registered_user["headers"])
    assert record_resp.json()["status"] == "approved"

    # 6. Only now can it be exported.
    export_resp = client.post(f"/api/v1/awards/{award['id']}/reports/{report['id']}/export", headers=registered_user["headers"])
    assert export_resp.status_code == 200, export_resp.text
    exported = export_resp.json()
    assert exported["download_url"]
    assert exported["file_size"] > 0

    reports_resp = client.get(f"/api/v1/awards/{award['id']}/reports", headers=registered_user["headers"])
    assert len(reports_resp.json()) == 1


def test_report_rejected_via_approvals_endpoint_stays_unexportable(client, registered_user, monkeypatch):
    _fake_narrative(monkeypatch)

    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    _share_proposal(client, org_id, proposal_id, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    approver = _register_and_login(client, "report-rejector")
    _invite_member(client, org_id, registered_user["headers"], approver["email"], "owner")

    report = client.post(
        f"/api/v1/awards/{award['id']}/reports", json={"report_type": "final"}, headers=registered_user["headers"],
    ).json()
    submitted = client.post(
        f"/api/v1/awards/{award['id']}/reports/{report['id']}/submit",
        json={"approver_id": approver["user_id"]}, headers=registered_user["headers"],
    ).json()

    decide_resp = client.post(
        f"/api/v1/approvals/{submitted['approval_request_id']}/decide",
        json={"approved": False, "decision_notes": "Needs more detail."}, headers=approver["headers"],
    )
    assert decide_resp.status_code == 200, decide_resp.text

    record_resp = client.get(f"/api/v1/awards/{award['id']}/reports/{report['id']}", headers=registered_user["headers"])
    assert record_resp.json()["status"] == "rejected"

    export_resp = client.post(f"/api/v1/awards/{award['id']}/reports/{report['id']}/export", headers=registered_user["headers"])
    assert export_resp.status_code == 400


def test_report_draft_creation_requires_edit_access(client, registered_user, monkeypatch):
    _fake_narrative(monkeypatch)

    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    _share_proposal(client, org_id, proposal_id, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    viewer = _register_and_login(client, "report-viewer")
    _invite_member(client, org_id, registered_user["headers"], viewer["email"], "viewer")

    resp = client.post(f"/api/v1/awards/{award['id']}/reports", json={"report_type": "progress"}, headers=viewer["headers"])
    assert resp.status_code == 403


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


# ── Renewal provenance (Version 3.0 upgrade, Phase 14 — Renewal Loop Closure) ──

def test_renewal_response_carries_provenance_and_notes(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    resp = client.post(
        f"/api/v1/awards/{award['id']}/renewal",
        json={"notes": "Renewing for a Phase II continuation."},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["originating_award_id"] == award["id"]
    assert body["originating_proposal_id"] == proposal_id
    assert "NSF-9999" in body["originating_award_label"]
    assert body["renewal_notes"] == "Renewing for a Phase II continuation."


def test_pipeline_listing_resolves_renewal_provenance(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)
    created = client.post(
        f"/api/v1/awards/{award['id']}/renewal",
        json={"notes": "Track for next cycle."},
        headers=registered_user["headers"],
    ).json()

    resp = client.get("/api/v1/foa/pipeline", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    records = {r["id"]: r for r in resp.json()}
    renewal_record = records[created["id"]]
    assert renewal_record["originating_award_id"] == award["id"]
    assert renewal_record["originating_proposal_id"] == proposal_id
    assert renewal_record["renewal_notes"] == "Track for next cycle."
    assert "NSF-9999" in renewal_record["originating_award_label"]

    # A pipeline entry never created via the renewal endpoint reports every
    # provenance field as null — this must stay purely additive. Inserted
    # directly via the ORM (same "no real OpenAI calls in tests" pattern as
    # test_funding_intelligence_api.py::_insert_foa) rather than through
    # upload/parse-text/parse-url.
    import asyncio
    from database import AsyncSessionLocal
    from models.db_models import FOARecord, new_uuid

    async def _insert_plain_foa() -> str:
        plain_id = new_uuid()
        async with AsyncSessionLocal() as db:
            db.add(FOARecord(
                id=plain_id, agency="NSF", program_title="Unrelated Opportunity",
                phase="phase_i", grant_type="sbir", uploaded_by=registered_user["user_id"],
            ))
            await db.commit()
        return plain_id

    plain_id = asyncio.run(_insert_plain_foa())
    resp2 = client.get("/api/v1/foa/pipeline", headers=registered_user["headers"])
    assert resp2.status_code == 200, resp2.text
    plain_record = next(r for r in resp2.json() if r["id"] == plain_id)
    assert plain_record["originating_award_id"] is None
    assert plain_record["originating_proposal_id"] is None
    assert plain_record["originating_award_label"] is None
    assert plain_record["renewal_notes"] is None


# ── Award Received: activation & baselines (Version 3.0 upgrade, Phase 8) ──

def test_new_award_starts_received_and_can_be_activated(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id, total_award_value=50000.0)
    assert award["award_status"] == "received"

    resp = client.post(f"/api/v1/awards/{award['id']}/activate", json={"notes": "Kickoff"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    baseline = resp.json()
    assert baseline["version"] == 1
    assert baseline["is_current"] is True
    assert baseline["total_award_value"] == 50000.0
    assert baseline["notes"] == "Kickoff"

    resp = client.get(f"/api/v1/awards/{award['id']}", headers=registered_user["headers"])
    assert resp.json()["award_status"] == "active"


def test_activate_award_twice_returns_400(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    resp = client.post(f"/api/v1/awards/{award['id']}/activate", json={}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text

    resp = client.post(f"/api/v1/awards/{award['id']}/activate", json={}, headers=registered_user["headers"])
    assert resp.status_code == 400


def test_current_baseline_404_before_activation(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    resp = client.get(f"/api/v1/awards/{award['id']}/baselines/current", headers=registered_user["headers"])
    assert resp.status_code == 404


def test_baseline_reversion_increments_version(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    resp = client.post(f"/api/v1/awards/{award['id']}/activate", json={}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text

    resp = client.post(f"/api/v1/awards/{award['id']}/baselines/reversion", json={"notes": "Scope amendment"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2
    assert resp.json()["is_current"] is True

    resp = client.get(f"/api/v1/awards/{award['id']}/baselines", headers=registered_user["headers"])
    assert [b["version"] for b in resp.json()] == [2, 1]

    resp = client.get(f"/api/v1/awards/{award['id']}/baselines/current", headers=registered_user["headers"])
    assert resp.json()["version"] == 2


def test_baseline_reversion_before_activation_returns_400(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    resp = client.post(f"/api/v1/awards/{award['id']}/baselines/reversion", json={}, headers=registered_user["headers"])
    assert resp.status_code == 400


def test_award_activation_requires_edit_access(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    proposal_id = _create_proposal(client, registered_user["headers"])
    _share_proposal(client, org_id, proposal_id, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    viewer = _register_and_login(client, "baseline-viewer")
    _invite_member(client, org_id, registered_user["headers"], viewer["email"], "viewer")

    resp = client.post(f"/api/v1/awards/{award['id']}/activate", json={}, headers=viewer["headers"])
    assert resp.status_code == 403


# ── Award Received: sponsor conditions (Version 3.0 upgrade, Phase 8) ──────

def test_award_condition_crud(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    resp = client.post(
        f"/api/v1/awards/{award['id']}/conditions",
        json={"description": "Submit revised budget justification before first drawdown.", "category": "financial"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    condition = resp.json()
    assert condition["status"] == "open"
    condition_id = condition["id"]

    resp = client.patch(
        f"/api/v1/awards/conditions/{condition_id}", json={"status": "resolved"}, headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "resolved"
    assert resp.json()["resolved_at"] is not None
    assert resp.json()["resolved_by"] == registered_user["user_id"]

    resp = client.get(f"/api/v1/awards/{award['id']}/conditions", headers=registered_user["headers"])
    assert len(resp.json()) == 1


# ── Planned vs. Actual (Version 3.0 upgrade, Phase 10) ──────────────────────

def test_planned_vs_actual_requires_activation(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id)

    resp = client.get(f"/api/v1/awards/{award['id']}/planned-vs-actual", headers=registered_user["headers"])
    assert resp.status_code == 400


def test_planned_vs_actual_reflects_expenditures_after_activation(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    award = _create_award(client, registered_user["headers"], proposal_id, total_award_value=40000.0)

    resp = client.post(f"/api/v1/awards/{award['id']}/activate", json={}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text

    resp = client.post(
        f"/api/v1/awards/{award['id']}/expenditures",
        json={"category": "personnel", "amount": 10000.0},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text

    resp = client.get(f"/api/v1/awards/{award['id']}/planned-vs-actual", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["baseline_version"] == 1
    assert body["total_expended"] == 10000.0
    assert body["scope_drift"] is False


# ── Quick Award Intake (Version 3.0 upgrade, Phase 15) ──────────────────────
# Lets a customer who already has a signed/funded award get it into the
# system without ever having used Pre-Award — no existing proposal_id, no
# organization required. See QuickAwardIntakeRequest's docstring in
# models/schemas.py and AwardEngine.create_award_from_intake().

def test_quick_intake_creates_award_with_no_proposal_or_org(client, registered_user):
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Rural Broadband Expansion", "funding_agency": "USDA", "award_number": "USDA-4471", "total_award_value": 250000.0},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    award = resp.json()
    assert award["funding_agency"] == "USDA"
    assert award["award_number"] == "USDA-4471"
    assert award["total_award_value"] == 250000.0
    assert award["proposal_title"] == "Rural Broadband Expansion"
    # Every award created via AwardEngine.create_award() (intake reuses it
    # unchanged) starts "received" and requires an explicit Activate Project.
    assert award["award_status"] == "received"

    # The shell proposal is real and owned by the importing user, so the
    # award is fully usable through every other award endpoint afterward.
    resp = client.get(f"/api/v1/awards/{award['id']}", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text


def test_quick_intake_award_can_be_activated_like_any_other(client, registered_user):
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Community Health Outreach", "funding_agency": "HHS"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    award = resp.json()

    resp = client.post(f"/api/v1/awards/{award['id']}/activate", json={"notes": "Imported and kicked off"}, headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_current"] is True


def test_quick_intake_unrelated_user_cannot_view(client, registered_user):
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Water Infrastructure Study", "funding_agency": "EPA"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    award = resp.json()

    other = _register_and_login(client, "intake-outsider")
    resp = client.get(f"/api/v1/awards/{award['id']}", headers=other["headers"])
    assert resp.status_code == 403


def test_quick_intake_with_org_requires_manage_awards_permission(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    viewer = _register_and_login(client, "intake-viewer")
    _invite_member(client, org_id, registered_user["headers"], viewer["email"], "viewer")

    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Blocked Import", "funding_agency": "NSF", "org_id": org_id},
        headers=viewer["headers"],
    )
    assert resp.status_code == 403

    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Owner Import", "funding_agency": "NSF", "org_id": org_id},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["org_id"] == org_id


def test_quick_intake_document_extracts_text_and_tags_award_notice(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Advanced Materials Research", "funding_agency": "DOE", "org_id": org_id},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    award = resp.json()

    resp = client.post(
        f"/api/v1/awards/{award['id']}/intake/document",
        data={"org_id": org_id},
        files={"file": ("award-notice.txt", b"This award is hereby granted in the amount of $500,000.", "text/plain")},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["library_type"] == "award_notice"
    assert doc["proposal_id"] == award["proposal_id"]
    assert doc["latest_version"]["content"] == "This award is hereby granted in the amount of $500,000."


def test_quick_intake_document_accepts_funded_proposal_doc_type(client, registered_user):
    # Same endpoint, the other doc_type — a customer skipping Pre-Award may
    # have their own approved/funded proposal on hand instead of (or as well
    # as) the funder's award notice.
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Marine Debris Cleanup Program", "funding_agency": "EPA", "org_id": org_id},
        headers=registered_user["headers"],
    )
    award = resp.json()

    resp = client.post(
        f"/api/v1/awards/{award['id']}/intake/document",
        data={"org_id": org_id, "doc_type": "funded_proposal"},
        files={"file": ("proposal.txt", b"Our approach to marine debris cleanup...", "text/plain")},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["library_type"] == "funded_proposal"
    assert "Approved/Funded Proposal" in doc["title"]


def test_quick_intake_document_rejects_invalid_doc_type(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Invalid Doc Type Test", "funding_agency": "NSF", "org_id": org_id},
        headers=registered_user["headers"],
    )
    award = resp.json()

    resp = client.post(
        f"/api/v1/awards/{award['id']}/intake/document",
        data={"org_id": org_id, "doc_type": "something_else"},
        files={"file": ("notice.txt", b"content", "text/plain")},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 422


def test_quick_intake_document_rejects_unsupported_file_type(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Unsupported Upload Test", "funding_agency": "NIH", "org_id": org_id},
        headers=registered_user["headers"],
    )
    award = resp.json()

    resp = client.post(
        f"/api/v1/awards/{award['id']}/intake/document",
        data={"org_id": org_id},
        files={"file": ("notice.exe", b"binary junk", "application/octet-stream")},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 422


def test_quick_intake_document_requires_edit_access_to_award(client, registered_user):
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Access Control Check", "funding_agency": "NSF"},
        headers=registered_user["headers"],
    )
    award = resp.json()

    other = _register_and_login(client, "intake-doc-outsider")
    resp = client.post(
        f"/api/v1/awards/{award['id']}/intake/document",
        data={"org_id": "does-not-matter"},
        files={"file": ("notice.txt", b"content", "text/plain")},
        headers=other["headers"],
    )
    assert resp.status_code == 403


# ── Phase C: original file preservation + GET /awards/{id}/files ──────────────

def test_intake_document_persists_original_file_and_is_listed(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Original File Preservation Test", "funding_agency": "NSF", "org_id": org_id},
        headers=registered_user["headers"],
    )
    award = resp.json()

    resp = client.post(
        f"/api/v1/awards/{award['id']}/intake/document",
        data={"org_id": org_id, "doc_type": "award_notice"},
        files={"file": ("award-notice.pdf", b"%PDF-1.4 fake pdf bytes", "application/pdf")},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text

    resp = client.get(f"/api/v1/awards/{award['id']}/files", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    files = resp.json()
    assert len(files) == 1
    assert files[0]["original_filename"] == "award-notice.pdf"
    assert files[0]["content_type"] == "application/pdf"
    assert files[0]["size_bytes"] == len(b"%PDF-1.4 fake pdf bytes")
    assert files[0]["download_url"].startswith("https://example-bucket.r2.example.com/")


def test_intake_document_two_doc_types_both_listed(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Two Doc Types Test", "funding_agency": "EPA", "org_id": org_id},
        headers=registered_user["headers"],
    )
    award = resp.json()

    for doc_type, fname in (("award_notice", "notice.txt"), ("funded_proposal", "proposal.txt")):
        resp = client.post(
            f"/api/v1/awards/{award['id']}/intake/document",
            data={"org_id": org_id, "doc_type": doc_type},
            files={"file": (fname, b"content", "text/plain")},
            headers=registered_user["headers"],
        )
        assert resp.status_code == 200, resp.text

    resp = client.get(f"/api/v1/awards/{award['id']}/files", headers=registered_user["headers"])
    assert resp.status_code == 200
    filenames = {f["original_filename"] for f in resp.json()}
    assert filenames == {"notice.txt", "proposal.txt"}


def test_intake_document_degrades_gracefully_when_r2_unconfigured(client, registered_user, monkeypatch):
    """If R2 isn't configured, storage.upload_file raises HTTPException(503)
    — the endpoint should still succeed (Document Library text extraction
    doesn't depend on R2) and simply not have a file to list afterward."""
    async def unconfigured_upload_file(org_id, category, content, filename, content_type):
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="File storage is not configured. Contact support.")

    monkeypatch.setattr(storage, "upload_file", unconfigured_upload_file)

    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "R2 Unconfigured Test", "funding_agency": "NSF", "org_id": org_id},
        headers=registered_user["headers"],
    )
    award = resp.json()

    resp = client.post(
        f"/api/v1/awards/{award['id']}/intake/document",
        data={"org_id": org_id},
        files={"file": ("notice.txt", b"content", "text/plain")},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text  # Document Library save still succeeds

    resp = client.get(f"/api/v1/awards/{award['id']}/files", headers=registered_user["headers"])
    assert resp.status_code == 200
    assert resp.json() == []  # nothing to preserve/list — degraded gracefully, not a 500


def test_list_award_files_empty_for_new_award(client, registered_user):
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "No Files Yet Test", "funding_agency": "NIH"},
        headers=registered_user["headers"],
    )
    award = resp.json()
    resp = client.get(f"/api/v1/awards/{award['id']}/files", headers=registered_user["headers"])
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_award_files_requires_view_access(client, registered_user):
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Files Access Control Test", "funding_agency": "NSF"},
        headers=registered_user["headers"],
    )
    award = resp.json()

    other = _register_and_login(client, "files-outsider")
    resp = client.get(f"/api/v1/awards/{award['id']}/files", headers=other["headers"])
    assert resp.status_code == 403


# ── Phase D: Award Intake Intelligence ───────────────────────────────────────

def test_extract_intelligence_400s_with_no_documents_uploaded(client, registered_user):
    """Short-circuits before any OpenAI call — safe to test without
    mocking anything (see module docstring)."""
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "No Documents Yet Test", "funding_agency": "NSF"},
        headers=registered_user["headers"],
    )
    award = resp.json()
    resp = client.post(f"/api/v1/awards/{award['id']}/extract-intelligence", headers=registered_user["headers"])
    assert resp.status_code == 400
    assert "no documents" in resp.json()["detail"].lower()


def test_extract_intelligence_requires_edit_access(client, registered_user):
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Extract Access Control Test", "funding_agency": "NSF"},
        headers=registered_user["headers"],
    )
    award = resp.json()

    other = _register_and_login(client, "extract-outsider")
    resp = client.post(f"/api/v1/awards/{award['id']}/extract-intelligence", headers=other["headers"])
    assert resp.status_code == 403  # access denied before the (missing-documents) 400 would even apply


def test_apply_intelligence_fills_blank_award_value_and_dates_only(client, registered_user):
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Blank Fields Test", "funding_agency": "NSF"},  # no total_award_value / dates
        headers=registered_user["headers"],
    )
    award = resp.json()
    assert award["total_award_value"] is None

    resp = client.post(
        f"/api/v1/awards/{award['id']}/apply-intelligence",
        json={
            "total_award_value": 500000,
            "period_of_performance_start": "2026-01-01T00:00:00",
            "period_of_performance_end": "2026-12-31T00:00:00",
            "work_packages": [],
        },
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["total_award_value"] == 500000
    assert updated["period_of_performance_start"] is not None

    # Calling it again with a different value must NOT overwrite what's now set.
    resp = client.post(
        f"/api/v1/awards/{award['id']}/apply-intelligence",
        json={"total_award_value": 999999, "work_packages": []},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_award_value"] == 500000


def test_apply_intelligence_never_overwrites_a_prefilled_award_value(client, registered_user):
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Prefilled Value Test", "funding_agency": "NSF", "total_award_value": 250000},
        headers=registered_user["headers"],
    )
    award = resp.json()
    assert award["total_award_value"] == 250000

    resp = client.post(
        f"/api/v1/awards/{award['id']}/apply-intelligence",
        json={"total_award_value": 999999, "work_packages": []},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_award_value"] == 250000


def test_apply_intelligence_creates_work_packages_via_scope_engine(client, registered_user):
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Work Breakdown Apply Test", "funding_agency": "DOE"},
        headers=registered_user["headers"],
    )
    award = resp.json()

    resp = client.post(
        f"/api/v1/awards/{award['id']}/apply-intelligence",
        json={"work_packages": [
            {
                "name": "Phase 1: Requirements", "description": "Initial requirements gathering",
                "start_month": 1, "end_month": 3,
                "tasks": ["Stakeholder interviews"], "milestones": ["Requirements sign-off"],
                "deliverables": ["Requirements document"],
            },
        ]},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text

    resp = client.get(f"/api/v1/proposals/{award['proposal_id']}/scope-of-work", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    sow = resp.json()
    assert len(sow["work_packages"]) == 1
    assert sow["work_packages"][0]["name"] == "Phase 1: Requirements"
    assert len(sow["milestones"]) == 1
    assert len(sow["deliverables"]) == 1


def test_apply_intelligence_creates_budget_and_links_it_to_the_award(client, registered_user):
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Budget Apply Test", "funding_agency": "EPA"},
        headers=registered_user["headers"],
    )
    award = resp.json()
    assert award["budget_record_id"] is None

    resp = client.post(
        f"/api/v1/awards/{award['id']}/apply-intelligence",
        json={"work_packages": [], "budget": {"extracted": {
            "budget_months": 12, "indirect_rate": 25.0, "indirect_base": "mtdc", "fee_rate": 7.0,
            "personnel": [{"name": "Dr. Smith", "role": "PI", "annual_salary": 120000, "fringe_rate": 30, "effort_pct": 20}],
            "consultants": [], "equipment": [], "travel": [], "other_direct": [], "subcontracts": [],
        }}},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["budget_record_id"] is not None

    resp = client.get(f"/api/v1/budget/{award['proposal_id']}", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    budget = resp.json()
    assert len(budget["personnel"]) == 1
    assert budget["personnel"][0]["name"] == "Dr. Smith"
    assert budget["total_direct"] > 0


def test_apply_intelligence_requires_edit_access(client, registered_user):
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Apply Access Control Test", "funding_agency": "NSF"},
        headers=registered_user["headers"],
    )
    award = resp.json()

    other = _register_and_login(client, "apply-outsider")
    resp = client.post(
        f"/api/v1/awards/{award['id']}/apply-intelligence",
        json={"work_packages": []},
        headers=other["headers"],
    )
    assert resp.status_code == 403


def test_apply_intelligence_fills_blank_project_knowledge_fields_only(client, registered_user):
    """Phase D.3: objectives/need_statement/outputs/outcomes/kpis extracted
    from uploaded documents apply the same 'never overwrite what's already
    there' rule as award value/dates — see the two award-value tests above."""
    resp = client.post(
        "/api/v1/awards/intake",
        json={"title": "Project Knowledge Apply Test", "funding_agency": "DOE"},
        headers=registered_user["headers"],
    )
    award = resp.json()

    resp = client.post(
        f"/api/v1/awards/{award['id']}/apply-intelligence",
        json={
            "work_packages": [],
            "objectives": "Train 250 participants in clean energy careers.",
            "need_statement": "The region lacks technical workforce training capacity.",
            "outputs": "Workforce training curriculum and virtual laboratory.",
            "outcomes": "A highly skilled regional clean energy workforce.",
            "kpis": [{"name": "Participants Trained", "target": "250", "unit": "participants"}],
        },
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text

    resp = client.get(f"/api/v1/proposals/{award['proposal_id']}/knowledge", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    pk = resp.json()
    assert pk["objectives"] == "Train 250 participants in clean energy careers."
    assert pk["need_statement"] == "The region lacks technical workforce training capacity."
    assert pk["outcomes"] == "A highly skilled regional clean energy workforce."
    assert len(pk["kpis"]) == 1
    assert pk["kpis"][0]["name"] == "Participants Trained"

    # Calling it again with different content must NOT overwrite what's
    # already there — same blank-only-fill rule as award value/dates, and
    # kpis is all-or-nothing rather than merged/duplicated.
    resp = client.post(
        f"/api/v1/awards/{award['id']}/apply-intelligence",
        json={
            "work_packages": [],
            "objectives": "A completely different objective.",
            "kpis": [{"name": "Different KPI", "target": "999", "unit": "widgets"}],
        },
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    resp = client.get(f"/api/v1/proposals/{award['proposal_id']}/knowledge", headers=registered_user["headers"])
    pk = resp.json()
    assert pk["objectives"] == "Train 250 participants in clean energy careers."
    assert len(pk["kpis"]) == 1
    assert pk["kpis"][0]["name"] == "Participants Trained"
