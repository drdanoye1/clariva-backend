"""
Application-level smoke tests: the app boots, the DB schema migration path
runs cleanly (this is what actually exercises database.py::create_tables()
and migrations.py end-to-end), and every router is wired up with its
expected auth requirement.
"""
from __future__ import annotations


def test_health_check(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["platform"] == "Clariva Intelligent Grant Writing Platform"


def test_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["docs"] == "/docs"


def test_openapi_schema_loads_with_every_router_mounted(client):
    """
    If a router failed to import or mount correctly, /openapi.json generation
    itself would raise — this is a cheap way to catch a broken router before
    it reaches production.
    """
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    for prefix in [
        "/api/v1/auth", "/api/v1/foa", "/api/v1/proposals", "/api/v1/scoring",
        "/api/v1/reviewer", "/api/v1/documents", "/api/v1/memory",
        "/api/v1/organizations", "/api/v1/profile", "/api/v1/budget",
        "/api/v1/payments", "/api/v1/admin", "/api/v1/suggest",
    ]:
        assert any(p.startswith(prefix) for p in paths), f"no routes registered under {prefix}"


def test_public_grant_types_endpoint_requires_no_auth(client):
    resp = client.get("/api/v1/proposals/grant-types")
    assert resp.status_code == 200
    assert len(resp.json()) > 0


def test_public_payment_plans_endpoint_requires_no_auth(client):
    resp = client.get("/api/v1/payments/plans")
    assert resp.status_code == 200


def test_protected_endpoints_reject_unauthenticated_requests(client):
    """
    Every one of these should require a bearer token. If RBAC/auth wiring
    ever regresses on one of these routers, this is the test that catches it.
    """
    protected_get_endpoints = [
        "/api/v1/auth/me",
        "/api/v1/proposals/",
        "/api/v1/organizations/",
        "/api/v1/memory/kpi",
        "/api/v1/profile/",
        "/api/v1/admin/stats",
    ]
    for path in protected_get_endpoints:
        resp = client.get(path)
        assert resp.status_code == 401, f"{path} did not require auth (got {resp.status_code})"


def test_admin_stats_rejects_non_superadmin_user(client, registered_user):
    """A regular registered user must not be able to reach admin endpoints."""
    resp = client.get("/api/v1/admin/stats", headers=registered_user["headers"])
    assert resp.status_code == 403
