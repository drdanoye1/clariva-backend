"""
Scope of Work Engine router — exercised through the real HTTP endpoints.

Only the CRUD/ownership/staleness/budget-sync surface is covered here — the
three AI-generation endpoints (generate-methodology, generate-evaluation-
plan, generate-work-breakdown) need a real OpenAI call and are intentionally
out of scope for this suite, same policy as proposals.py's generate-section
(see conftest.py's module docstring).
"""
from __future__ import annotations

import uuid


def _org_context() -> dict:
    return {"organization_name": "Acme Research", "industry": "Biotech"}


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


def _create_proposal(client, headers: dict) -> str:
    resp = client.post("/api/v1/proposals/", json=_proposal_payload(), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["proposal_id"]


def _register_and_login(client, label: str) -> dict:
    email = f"{label}-{uuid.uuid4().hex[:10]}@example.com"
    password = "TestPassword123!"
    resp = client.post("/api/v1/auth/register", json={
        "email": email, "password": password,
        "full_name": f"{label.title()} User", "organization": "Test Org",
    })
    assert resp.status_code == 201, resp.text
    login = client.post("/api/v1/auth/login", data={"username": email, "password": password})
    token = login.json()["access_token"]
    return {"email": email, "headers": {"Authorization": f"Bearer {token}"}}


# ── Project Knowledge ────────────────────────────────────────────────────────

def test_get_project_knowledge_lazily_creates_it(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    resp = client.get(f"/api/v1/proposals/{proposal_id}/knowledge", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["proposal_id"] == proposal_id
    assert body["objectives"] is None
    assert body["stale_flags"] == {}


def test_update_project_knowledge_persists_and_flags_stale(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    resp = client.patch(
        f"/api/v1/proposals/{proposal_id}/knowledge",
        json={"objectives": "Cut costs by 30%", "risks": [{"risk": "Supply delay", "mitigation": "Backup vendor"}]},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["objectives"] == "Cut costs by 30%"
    assert len(body["risks"]) == 1
    assert body["risks"][0]["risk"] == "Supply delay"
    assert body["risks"][0]["mitigation"] == "Backup vendor"
    assert body["stale_flags"].get("sections") is True


def test_knowledge_requires_ownership(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    other = _register_and_login(client, "other")
    resp = client.get(f"/api/v1/proposals/{proposal_id}/knowledge", headers=other["headers"])
    assert resp.status_code == 404


def test_clear_stale_flag(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    client.patch(f"/api/v1/proposals/{proposal_id}/knowledge", json={"objectives": "X"}, headers=registered_user["headers"])
    resp = client.delete(f"/api/v1/proposals/{proposal_id}/knowledge/stale-flags/sections", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert "sections" not in resp.json()["stale_flags"]


# ── Scope of Work aggregate view ─────────────────────────────────────────────

def test_get_scope_of_work_returns_empty_shell_for_new_proposal(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    resp = client.get(f"/api/v1/proposals/{proposal_id}/scope-of-work", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["work_packages"] == []
    assert body["tasks"] == []
    assert body["scope_of_work"]["project_knowledge_id"] == body["project_knowledge"]["id"]


def test_update_scope_of_work(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    resp = client.patch(
        f"/api/v1/proposals/{proposal_id}/scope-of-work",
        json={"period_of_performance_months": 18, "methodology_narrative": "Agile sprints."},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["period_of_performance_months"] == 18


# ── Work Packages / Tasks / Milestones / Deliverables ───────────────────────

def test_full_work_breakdown_crud_flow(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    headers = registered_user["headers"]

    wp_resp = client.post(
        f"/api/v1/proposals/{proposal_id}/scope-of-work/work-packages",
        json={"name": "Design Phase", "estimated_cost": 2500.0},
        headers=headers,
    )
    assert wp_resp.status_code == 201, wp_resp.text
    wp_id = wp_resp.json()["id"]

    task_resp = client.post(
        f"/api/v1/proposals/{proposal_id}/scope-of-work/work-packages/{wp_id}/tasks",
        json={"name": "Draft spec"},
        headers=headers,
    )
    assert task_resp.status_code == 201, task_resp.text
    task_id = task_resp.json()["id"]

    milestone_resp = client.post(
        f"/api/v1/proposals/{proposal_id}/scope-of-work/milestones",
        json={"name": "Design review", "work_package_id": wp_id, "due_month": 2},
        headers=headers,
    )
    assert milestone_resp.status_code == 201, milestone_resp.text

    deliverable_resp = client.post(
        f"/api/v1/proposals/{proposal_id}/scope-of-work/deliverables",
        json={"name": "Design doc", "work_package_id": wp_id, "deliverable_type": "report"},
        headers=headers,
    )
    assert deliverable_resp.status_code == 201, deliverable_resp.text

    full = client.get(f"/api/v1/proposals/{proposal_id}/scope-of-work", headers=headers).json()
    assert len(full["work_packages"]) == 1
    assert len(full["tasks"]) == 1
    assert len(full["milestones"]) == 1
    assert len(full["deliverables"]) == 1
    assert full["project_knowledge"]["stale_flags"].get("budget") is True

    update_resp = client.patch(
        f"/api/v1/proposals/{proposal_id}/scope-of-work/tasks/{task_id}",
        json={"status": "in_progress"},
        headers=headers,
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["status"] == "in_progress"

    delete_resp = client.delete(
        f"/api/v1/proposals/{proposal_id}/scope-of-work/work-packages/{wp_id}", headers=headers,
    )
    assert delete_resp.status_code == 204

    after = client.get(f"/api/v1/proposals/{proposal_id}/scope-of-work", headers=headers).json()
    assert after["work_packages"] == []
    assert after["tasks"] == []  # cascade-deleted with the work package


def test_task_under_nonexistent_work_package_404s(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/scope-of-work/work-packages/does-not-exist/tasks",
        json={"name": "Orphan"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 404


def test_work_package_endpoints_require_ownership(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    other = _register_and_login(client, "other2")
    resp = client.post(
        f"/api/v1/proposals/{proposal_id}/scope-of-work/work-packages",
        json={"name": "Should fail"},
        headers=other["headers"],
    )
    assert resp.status_code == 404


# ── Budget sync ──────────────────────────────────────────────────────────────

def test_sync_budget_merges_work_package_costs(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    headers = registered_user["headers"]

    client.post(
        f"/api/v1/proposals/{proposal_id}/scope-of-work/work-packages",
        json={"name": "Fabrication", "estimated_cost": 4000.0},
        headers=headers,
    )

    resp = client.post(f"/api/v1/proposals/{proposal_id}/scope-of-work/sync-budget", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["synced_work_packages"] == 1
    assert body["total_direct"] == 4000.0

    budget_resp = client.get(f"/api/v1/budget/{proposal_id}", headers=headers)
    assert budget_resp.status_code == 200
    other_direct = budget_resp.json()["other_direct"]
    assert any(item.get("source") == "scope_of_work" for item in other_direct)

    knowledge = client.get(f"/api/v1/proposals/{proposal_id}/knowledge", headers=headers).json()
    assert "budget" not in knowledge["stale_flags"]


def test_sync_budget_preserves_manually_added_line_items(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    headers = registered_user["headers"]

    # Add a manual line item via the existing Budget Builder endpoint first.
    save_resp = client.put(
        f"/api/v1/budget/{proposal_id}",
        json={"other_direct": [{"id": "manual1", "category": "Materials", "description": "Widgets", "cost": 150.0}]},
        headers=headers,
    )
    assert save_resp.status_code == 200, save_resp.text

    client.post(
        f"/api/v1/proposals/{proposal_id}/scope-of-work/work-packages",
        json={"name": "Testing", "estimated_cost": 800.0},
        headers=headers,
    )
    client.post(f"/api/v1/proposals/{proposal_id}/scope-of-work/sync-budget", headers=headers)

    budget = client.get(f"/api/v1/budget/{proposal_id}", headers=headers).json()
    categories = {item["category"] for item in budget["other_direct"]}
    assert "Materials" in categories
    assert "Scope of Work" in categories
