"""
Document export (Version 3.0 upgrade, "Real File Storage" scope, Phase B) —
exercised through the real HTTP endpoint.

No real R2/network calls are made anywhere in this suite (same "no network
calls in tests" policy as every other engine here — see conftest.py's
module docstring). The success path monkeypatches storage.upload_file/
get_download_url directly; the R2-unconfigured path is tested by forcing
config.settings' R2_* fields empty via monkeypatch rather than relying on
whatever might already be sitting in a real local .env — a developer who
has since configured real R2 credentials for their own dev environment
should not see this test start failing.
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select


def _proposal_payload(**overrides) -> dict:
    payload = {
        "title": "Rural Water Infrastructure Modernization",
        "agency": "EPA", "phase": "phase_i", "grant_type": "federal_other",
        "org_context": {"organization_name": "Acme Water Co", "industry": "Infrastructure"},
        "research_focus": "Smart water metering",
        "innovation_description": "IoT-based leak detection network",
    }
    payload.update(overrides)
    return payload


def _create_proposal(client, headers: dict) -> str:
    resp = client.post("/api/v1/proposals/", json=_proposal_payload(), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["proposal_id"]


def test_export_returns_503_when_storage_not_configured(client, registered_user, monkeypatch):
    from config import settings
    import storage

    monkeypatch.setattr(settings, "R2_ACCOUNT_ID", "")
    monkeypatch.setattr(settings, "R2_ACCESS_KEY_ID", "")
    monkeypatch.setattr(settings, "R2_SECRET_ACCESS_KEY", "")
    monkeypatch.setattr(settings, "R2_BUCKET_NAME", "")
    monkeypatch.setattr(storage, "_client", None)  # don't reuse a cached client from another test

    proposal_id = _create_proposal(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/documents/export",
        json={"proposal_id": proposal_id, "format": "txt"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"].lower()


def test_export_success_persists_stored_file_and_returns_presigned_url(client, registered_user, monkeypatch):
    import storage

    captured = {}

    async def fake_upload_file(org_id, category, content, filename, content_type):
        captured["org_id"] = org_id
        captured["category"] = category
        captured["content_len"] = len(content)
        captured["filename"] = filename
        return "fake/proposal_export/key.txt"

    async def fake_get_download_url(storage_key, filename=None, expires_in=3600):
        assert storage_key == "fake/proposal_export/key.txt"
        return "https://example-bucket.r2.example.com/fake/proposal_export/key.txt?X-Amz-Signature=abc"

    monkeypatch.setattr(storage, "upload_file", fake_upload_file)
    monkeypatch.setattr(storage, "get_download_url", fake_get_download_url)

    proposal_id = _create_proposal(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/documents/export",
        json={"proposal_id": proposal_id, "format": "txt"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["download_url"] == "https://example-bucket.r2.example.com/fake/proposal_export/key.txt?X-Amz-Signature=abc"
    assert body["file_size_bytes"] > 0

    assert captured["category"] == "proposal_export"
    assert captured["org_id"] is None
    assert captured["content_len"] > 0

    # A StoredFile row was actually created and committed, not just returned
    # in the response.
    from database import AsyncSessionLocal
    from models.db_models import StoredFile

    async def _fetch():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(StoredFile).where(StoredFile.object_id == proposal_id))
            return result.scalar_one_or_none()

    stored = asyncio.run(_fetch())
    assert stored is not None
    assert stored.object_type == "proposal_export"
    assert stored.storage_key == "fake/proposal_export/key.txt"
    assert stored.org_id is None
    assert stored.size_bytes == body["file_size_bytes"]
    assert stored.checksum is not None


def test_export_requires_ownership(client, registered_user):
    resp = client.post(
        "/api/v1/documents/export",
        json={"proposal_id": uuid.uuid4().hex, "format": "txt"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 404


def test_list_proposal_exports_empty_before_any_export(client, registered_user):
    proposal_id = _create_proposal(client, registered_user["headers"])
    resp = client.get(f"/api/v1/documents/exports/{proposal_id}", headers=registered_user["headers"])
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_proposal_exports_returns_history_after_export(client, registered_user, monkeypatch):
    """Phase C — export_proposal() only ever returned a one-time download_url
    in its response body; this is the first way to come back later and find
    a file already exported."""
    import storage

    async def fake_upload_file(org_id, category, content, filename, content_type):
        return "fake/proposal_export/report.txt"

    async def fake_get_download_url(storage_key, filename=None, expires_in=3600):
        return f"https://example-bucket.r2.example.com/{storage_key}"

    monkeypatch.setattr(storage, "upload_file", fake_upload_file)
    monkeypatch.setattr(storage, "get_download_url", fake_get_download_url)

    proposal_id = _create_proposal(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/documents/export",
        json={"proposal_id": proposal_id, "format": "txt"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text

    resp = client.get(f"/api/v1/documents/exports/{proposal_id}", headers=registered_user["headers"])
    assert resp.status_code == 200
    files = resp.json()
    assert len(files) == 1
    assert files[0]["download_url"] == "https://example-bucket.r2.example.com/fake/proposal_export/report.txt"
    assert files[0]["size_bytes"] > 0


def test_list_proposal_exports_requires_ownership(client, registered_user):
    resp = client.get(f"/api/v1/documents/exports/{uuid.uuid4().hex}", headers=registered_user["headers"])
    assert resp.status_code == 404


def test_local_disk_download_endpoint_no_longer_exists(client, registered_user):
    """The old GET /download/{filename} endpoint (and the local-disk file it
    served) was removed in this phase — downloads are presigned R2 URLs
    returned directly from POST /export now. See routers/documents.py's
    module docstring for the reliability bug this replaced."""
    resp = client.get("/api/v1/documents/download/whatever.txt", headers=registered_user["headers"])
    assert resp.status_code == 404
