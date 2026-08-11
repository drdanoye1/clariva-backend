"""
Manual-add source grouping — the fallback path for funding sources Sync
(Grants.gov/SAM.gov) can't reach: state/local government, private
foundations, and international organizations (World Bank, UN, UNESCO,
etc.). See routers/foa.py's `_resolve_manual_source` docstring for why
these are added manually rather than synced.

Exercises upload/parse-text/parse-url through real HTTP endpoints. Per
this suite's existing convention (see test_funding_intelligence_api.py's
module docstring), the only I/O actually mocked out is the OpenAI call
(`parser.parse`) and, for parse-url, the outbound HTTP fetch — everything
else (extract_text, builder.build, the DB writes, RBAC) runs for real.
"""
from __future__ import annotations

import io
import uuid

import pytest

from database import AsyncSessionLocal
from models.db_models import FOARecord, new_uuid


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


FAKE_PARSED = {
    "agency": "OTHER", "program_title": "Community Resilience Grant", "solicitation_number": None,
    "phase": "phase_i", "total_page_limit": None, "deadline": None,
    "ordered_sections": [], "compliance_rules": [], "weights": {},
    "summary": None, "eligibility_summary": None,
}


async def _fake_parse(raw_text: str):
    return dict(FAKE_PARSED)


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200, content_type: str = "text/plain"):
        self.text = text
        self.content = text.encode()
        self.status_code = status_code
        self.headers = {"content-type": content_type}


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient in parse_foa_url — no real network
    call, matching this suite's "no network calls anywhere" rule."""
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        return _FakeResponse("A state grants portal solicitation with enough text to pass the length check. " * 3)


@pytest.fixture()
def mock_parser(monkeypatch):
    import routers.foa as foa_router
    monkeypatch.setattr(foa_router.parser, "parse", _fake_parse)
    monkeypatch.setattr(foa_router.httpx, "AsyncClient", _FakeAsyncClient)
    return foa_router


async def _get_foa(foa_id: str) -> FOARecord:
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(FOARecord).where(FOARecord.id == foa_id))
        return result.scalar_one()


def _get_foa_sync(foa_id: str) -> FOARecord:
    import asyncio
    return asyncio.run(_get_foa(foa_id))


# ── parse-text ────────────────────────────────────────────────────────────────

def test_parse_text_defaults_to_manual_source_personal(client, registered_user, mock_parser):
    """Backward compatibility: omitting org_id/source_type behaves exactly
    as before this feature existed — personal, source='manual'."""
    resp = client.post(
        "/api/v1/foa/parse-text",
        json={"text": "A" * 60},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    record = _get_foa_sync(resp.json()["foa_id"])
    assert record.org_id is None
    assert record.source == "manual"


def test_parse_text_tags_source_type_personal(client, registered_user, mock_parser):
    resp = client.post(
        "/api/v1/foa/parse-text",
        json={"text": "A" * 60, "source_type": "state_local"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    record = _get_foa_sync(resp.json()["foa_id"])
    assert record.org_id is None
    assert record.source == "state_local"


def test_parse_text_invalid_source_type_rejected(client, registered_user, mock_parser):
    resp = client.post(
        "/api/v1/foa/parse-text",
        json={"text": "A" * 60, "source_type": "not_a_real_source"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 400
    assert "source_type" in resp.json()["detail"]


def test_parse_text_org_share_requires_manage_watchlists(client, registered_user, mock_parser):
    org_id = _create_org(client, registered_user["headers"])
    viewer = _register_and_login(client, "viewer")
    _invite_member(client, org_id, registered_user["headers"], viewer["email"], "viewer")

    resp = client.post(
        "/api/v1/foa/parse-text",
        json={"text": "A" * 60, "org_id": org_id, "source_type": "foundation"},
        headers=viewer["headers"],
    )
    assert resp.status_code == 403


def test_parse_text_org_share_succeeds_for_editor(client, registered_user, mock_parser):
    org_id = _create_org(client, registered_user["headers"])
    editor = _register_and_login(client, "editor")
    _invite_member(client, org_id, registered_user["headers"], editor["email"], "editor")

    resp = client.post(
        "/api/v1/foa/parse-text",
        json={"text": "A" * 60, "org_id": org_id, "source_type": "foundation"},
        headers=editor["headers"],
    )
    assert resp.status_code == 200, resp.text
    record = _get_foa_sync(resp.json()["foa_id"])
    assert record.org_id == org_id
    assert record.source == "foundation"

    # Shows up in this org's pipeline, filterable by the new source group —
    # exercises the exact query pipeline.tsx's new source dropdown drives.
    list_resp = client.get(
        "/api/v1/foa/pipeline", params={"org_id": org_id, "source": "foundation"}, headers=registered_user["headers"],
    )
    assert list_resp.status_code == 200, list_resp.text
    assert any(r["id"] == record.id for r in list_resp.json())


# ── parse-url ─────────────────────────────────────────────────────────────────

def test_parse_url_tags_source_type_and_org(client, registered_user, mock_parser):
    org_id = _create_org(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/foa/parse-url",
        json={"url": "https://example-state-portal.gov/rfp/123", "org_id": org_id, "source_type": "state_local"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    record = _get_foa_sync(resp.json()["foa_id"])
    assert record.org_id == org_id
    assert record.source == "state_local"


# ── upload ────────────────────────────────────────────────────────────────────

def test_upload_tags_source_type_via_form_fields(client, registered_user, mock_parser):
    file_content = b"A" * 200  # extract_text handles plain bytes with no OpenAI call
    resp = client.post(
        "/api/v1/foa/upload",
        files={"file": ("solicitation.txt", io.BytesIO(file_content), "text/plain")},
        data={"source_type": "international"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    record = _get_foa_sync(resp.json()["foa_id"])
    assert record.org_id is None
    assert record.source == "international"
