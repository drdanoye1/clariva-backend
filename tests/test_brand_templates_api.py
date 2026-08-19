"""
Customer co-brand / white-label template registry endpoints on
organizations.py (CLARIVA-DOCGEN-SPEC-001, Phase 13) — exercised through
real HTTP endpoints. Same helper pattern as test_branding_api.py.
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


def _key() -> str:
    return f"testtemplate-{uuid.uuid4().hex[:8]}"


def test_resolve_with_no_templates_returns_hardcoded_system_default(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.get(f"/api/v1/organizations/{org_id}/brand-templates/resolve", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["template_key"] == "clariva_standard"
    assert body["name"] == "Clariva Standard"
    # This project's test DB is shared/run-persistent (see
    # test_agency_profile_engine.py's module docstring for the convention).
    # test_brand_template_engine.py::
    # test_seed_defaults_creates_and_activates_system_template idempotently
    # seeds a real, permanent "clariva_standard" SYSTEM template (org_id
    # None, version=1) into that same DB — so depending on whether that test
    # has already run in this DB's lifetime, resolve() here legitimately
    # sees either zero rows (hardcoded baseline, version None) or the
    # seeded system row (version 1). Both are correct per resolve()'s
    # "resolvers never crash on unknown keys" contract; this test only
    # pins the fields that are invariant either way (template_key, name —
    # no other test ever creates an org-scoped or system-scoped row keyed
    # "clariva_standard" with a different name).
    assert body["version"] in (None, 1)


def test_owner_can_create_and_activate_template_member_can_view(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    member = _register_and_login(client, "member")
    _invite_member(client, org_id, registered_user["headers"], member["email"], "editor")
    key = _key()

    resp = client.post(
        f"/api/v1/organizations/{org_id}/brand-templates",
        json={
            "template_key": key, "name": "Acme Co-Brand",
            "logo_url": "https://acme.example/logo.png", "primary_color": "#1d4ed8",
            "header_text": "Powered by Acme", "footer_text": "Confidential",
            "activate": True,
        },
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["version"] == 1
    assert created["is_active"] is True

    # Member can view it in the org's active-templates list
    resp = client.get(f"/api/v1/organizations/{org_id}/brand-templates", headers=member["headers"])
    assert resp.status_code == 200, resp.text
    keys = [t["template_key"] for t in resp.json()]
    assert key in keys

    # And resolve() reflects it
    resp = client.get(
        f"/api/v1/organizations/{org_id}/brand-templates/resolve",
        params={"template_key": key}, headers=member["headers"],
    )
    assert resp.status_code == 200, resp.text
    resolved = resp.json()
    assert resolved["name"] == "Acme Co-Brand"
    assert resolved["logo_url"] == "https://acme.example/logo.png"
    assert resolved["header_text"] == "Powered by Acme"

    # Member cannot create a new version — manage_branding is owner-only
    resp = client.post(
        f"/api/v1/organizations/{org_id}/brand-templates",
        json={"template_key": key, "name": "Hijacked"},
        headers=member["headers"],
    )
    assert resp.status_code == 403


def test_non_member_cannot_view_or_resolve_brand_templates(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    stranger = _register_and_login(client, "stranger")

    resp = client.get(f"/api/v1/organizations/{org_id}/brand-templates", headers=stranger["headers"])
    assert resp.status_code == 403

    resp = client.get(f"/api/v1/organizations/{org_id}/brand-templates/resolve", headers=stranger["headers"])
    assert resp.status_code == 403


def test_second_org_cannot_resolve_or_activate_first_orgs_private_template(client, registered_user):
    """SECURITY — org isolation. Org A creates a private white-label
    template; org B (a completely separate owner/org) must never be able to
    resolve it by key, and activating it by id must 404, not leak that it
    exists."""
    org_a = _create_org(client, registered_user["headers"])
    key = _key()
    resp = client.post(
        f"/api/v1/organizations/{org_a}/brand-templates",
        json={"template_key": key, "name": "Org A Private", "logo_url": "https://a.example/logo.png", "activate": True},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    template_id = resp.json()["id"]

    owner_b = _register_and_login(client, "ownerb")
    org_b = _create_org(client, owner_b["headers"])

    # Org B resolving the same key sees no version, no logo — org A's row
    # never leaks across the org boundary.
    resp = client.get(
        f"/api/v1/organizations/{org_b}/brand-templates/resolve",
        params={"template_key": key}, headers=owner_b["headers"],
    )
    assert resp.status_code == 200, resp.text
    resolved = resp.json()
    assert resolved["version"] is None
    assert resolved["logo_url"] is None

    # Org B activating org A's template_id by id gets a 404, not a 403 —
    # it must never confirm the row exists.
    resp = client.post(
        f"/api/v1/organizations/{org_b}/brand-templates/{template_id}/activate",
        headers=owner_b["headers"],
    )
    assert resp.status_code == 404


def test_set_default_rejects_unknown_key_and_accepts_valid_key(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    key = _key()

    resp = client.patch(
        f"/api/v1/organizations/{org_id}/brand-templates/default",
        json={"template_key": "totally-made-up-key-no-such-thing"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 400

    resp = client.post(
        f"/api/v1/organizations/{org_id}/brand-templates",
        json={"template_key": key, "name": "Default Candidate", "activate": True},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text

    resp = client.patch(
        f"/api/v1/organizations/{org_id}/brand-templates/default",
        json={"template_key": key},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text

    # resolve() with no explicit key now picks up the org's own default
    resp = client.get(f"/api/v1/organizations/{org_id}/brand-templates/resolve", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Default Candidate"


def test_member_cannot_set_default_brand_template(client, registered_user):
    org_id = _create_org(client, registered_user["headers"])
    member = _register_and_login(client, "member2")
    _invite_member(client, org_id, registered_user["headers"], member["email"], "editor")

    resp = client.patch(
        f"/api/v1/organizations/{org_id}/brand-templates/default",
        json={"template_key": "clariva_standard"},
        headers=member["headers"],
    )
    assert resp.status_code == 403
