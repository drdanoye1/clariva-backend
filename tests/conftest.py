"""
Shared pytest fixtures for the Clariva backend test suite.

Points the app at an isolated, throwaway SQLite database BEFORE any
application module is imported, so running the suite never touches the real
dev database in .env (and definitely never touches production). This is
Phase 0 — Foundation Hardening from the Clariva Enterprise™ PRD: these tests
exist to make every later phase (RBAC, Scope of Work Engine, funding
intelligence, ...) safe to build on top of without breaking what already
works.

No network calls are made anywhere in this suite — tests that exercise
engines with OpenAI calls only touch their pure/deterministic helper
methods (see docs/ARCHITECTURE.md, "What to test / what not to test").
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# --- Must run before any `from config import settings`, `from database
# import ...`, or `from main import app` anywhere in the test suite, so
# pydantic-settings picks up these values instead of .env / real defaults.
_TMP_DIR = tempfile.mkdtemp(prefix="clariva_test_")
_TEST_DB_PATH = Path(_TMP_DIR) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB_PATH}"
os.environ["SYNC_DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("SUPERADMIN_PASSWORD", "")  # empty => seeding is skipped on startup
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-a-real-key")
os.environ.setdefault("APP_ENV", "test")

import uuid  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def app():
    """Import the FastAPI app only after the env vars above are in place."""
    from main import app as fastapi_app
    return fastapi_app


@pytest.fixture()
def client(app):
    """
    A TestClient using the `with` form so FastAPI's lifespan (startup/
    shutdown) runs — this is what actually creates the test database tables
    via database.py::create_tables() on the first request.
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def registered_user(client):
    """Register + log in a throwaway user; returns creds and an auth header."""
    email = f"test-{uuid.uuid4().hex[:12]}@example.com"
    password = "TestPassword123!"

    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": password,
            "full_name": "Test User",
            "organization": "Test Org",
        },
    )
    assert resp.status_code == 201, resp.text

    login = client.post(
        "/api/v1/auth/login",
        data={"username": email, "password": password},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]

    return {
        "email": email,
        "password": password,
        "user_id": resp.json()["id"],
        "headers": {"Authorization": f"Bearer {token}"},
    }
