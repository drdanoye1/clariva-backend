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


def test_export_pdf_produces_a_real_pdf_via_whichever_renderer_is_available(client, registered_user, monkeypatch):
    """Word/PDF Report Generation Development Specification
    (CLARIVA-DOCGEN-SPEC-001), Phase 8 — format=pdf now tries headless
    LibreOffice first (utils/pdf_convert.py) and falls back to the legacy
    reportlab renderer if no soffice binary is available in this test
    environment. Either way the response must be a real, valid PDF — this
    test doesn't assume which renderer produced it (see
    test_pdf_convert.py for a LibreOffice-specific, skip-if-unavailable
    test of the conversion itself)."""
    import storage

    captured = {}

    async def fake_upload_file(org_id, category, content, filename, content_type):
        captured["content"] = content
        captured["content_type"] = content_type
        return "fake/proposal_export/key.pdf"

    async def fake_get_download_url(storage_key, filename=None, expires_in=3600):
        return "https://example-bucket.r2.example.com/fake/proposal_export/key.pdf"

    monkeypatch.setattr(storage, "upload_file", fake_upload_file)
    monkeypatch.setattr(storage, "get_download_url", fake_get_download_url)

    proposal_id = _create_proposal(client, registered_user["headers"])
    resp = client.post(
        "/api/v1/documents/export",
        json={"proposal_id": proposal_id, "format": "pdf"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert captured["content_type"] == "application/pdf"
    assert captured["content"][:4] == b"%PDF", "export must produce a real PDF regardless of which renderer handled it"
    assert len(captured["content"]) > 500


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


def _inject_missing_placeholder(proposal_id: str) -> None:
    """Directly write a [MISSING: ...] gap into the proposal's first
    auto-created section — same "write straight to the DB via
    AsyncSessionLocal" pattern test_billing_wireup.py uses to set up state
    that isn't reachable through the API alone."""
    from database import AsyncSessionLocal
    from models.db_models import ProposalSection

    async def _do():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(ProposalSection).where(ProposalSection.proposal_id == proposal_id))
            sections = result.scalars().all()
            assert sections, "expected at least one auto-created section"
            sections[0].content = "Some real content with a [MISSING: budget figure] gap left in it."
            sections[0].word_count = 12
            await db.commit()
    asyncio.run(_do())


def test_export_blocked_when_require_clean_export_and_unresolved_placeholder(client, registered_user, monkeypatch):
    """Phase 5 export QA gate — require_clean_export=True must abort BEFORE
    any file is generated or uploaded when ComplianceEngine finds an
    error-severity violation (here, an unresolved [MISSING: ...] placeholder).
    storage.upload_file is monkeypatched to raise so the test fails loudly if
    the gate doesn't actually stop the export before that point."""
    import storage

    async def fail_if_called(*a, **kw):
        raise AssertionError("upload_file should not be called when the export QA gate blocks the export")

    monkeypatch.setattr(storage, "upload_file", fail_if_called)

    proposal_id = _create_proposal(client, registered_user["headers"])
    _inject_missing_placeholder(proposal_id)

    resp = client.post(
        "/api/v1/documents/export",
        json={"proposal_id": proposal_id, "format": "txt", "require_clean_export": True},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    report = detail["compliance_report"]
    assert report["passed"] is False
    assert any(v["rule"] == "Missing placeholder" and v["severity"] == "error" for v in report["violations"])

    # Nothing was persisted — the gate fired before any StoredFile row.
    from database import AsyncSessionLocal
    from models.db_models import StoredFile

    async def _fetch():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(StoredFile).where(StoredFile.object_id == proposal_id))
            return result.scalar_one_or_none()
    assert asyncio.run(_fetch()) is None


def test_export_succeeds_with_attached_compliance_report_when_not_strict(client, registered_user, monkeypatch):
    """Default behavior (require_clean_export omitted/False) is unchanged
    from before this phase: the export always succeeds. The only new thing
    is that ExportResponse.compliance_report now rides along so the caller
    can see the same findings without a second round trip."""
    import storage

    async def fake_upload_file(org_id, category, content, filename, content_type):
        return "fake/proposal_export/report.txt"

    async def fake_get_download_url(storage_key, filename=None, expires_in=3600):
        return f"https://example-bucket.r2.example.com/{storage_key}"

    monkeypatch.setattr(storage, "upload_file", fake_upload_file)
    monkeypatch.setattr(storage, "get_download_url", fake_get_download_url)

    proposal_id = _create_proposal(client, registered_user["headers"])
    _inject_missing_placeholder(proposal_id)

    resp = client.post(
        "/api/v1/documents/export",
        json={"proposal_id": proposal_id, "format": "txt"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()["compliance_report"]
    assert report is not None
    assert report["passed"] is False
    assert any(v["rule"] == "Missing placeholder" for v in report["violations"])


def test_local_disk_download_endpoint_no_longer_exists(client, registered_user):
    """The old GET /download/{filename} endpoint (and the local-disk file it
    served) was removed in this phase — downloads are presigned R2 URLs
    returned directly from POST /export now. See routers/documents.py's
    module docstring for the reliability bug this replaced."""
    resp = client.get("/api/v1/documents/download/whatever.txt", headers=registered_user["headers"])
    assert resp.status_code == 404
