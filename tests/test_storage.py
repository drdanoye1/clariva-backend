"""
storage.py — Cloudflare R2 abstraction (Version 3.0 upgrade, "Real File
Storage" scope, Phase A). Pure unit tests, no app/HTTP client needed and no
real R2 network calls — see test_document_export_api.py for HTTP-level
coverage of the export flow that calls into this module.
"""
from __future__ import annotations

import hashlib

import pytest
from fastapi import HTTPException


def test_get_client_raises_503_when_unconfigured(monkeypatch):
    from config import settings
    import storage

    monkeypatch.setattr(settings, "R2_ACCOUNT_ID", "")
    monkeypatch.setattr(settings, "R2_ACCESS_KEY_ID", "")
    monkeypatch.setattr(settings, "R2_SECRET_ACCESS_KEY", "")
    monkeypatch.setattr(settings, "R2_BUCKET_NAME", "")
    monkeypatch.setattr(storage, "_client", None)

    with pytest.raises(HTTPException) as exc_info:
        storage._get_client()
    assert exc_info.value.status_code == 503


def test_new_key_namespaces_by_org_and_category_and_sanitizes_filename():
    import storage

    key = storage._new_key("org-123", "proposal_export", "My Report (final).docx")
    assert key.startswith("org-123/proposal_export/")
    assert key.endswith(".docx")
    # Unsafe characters (spaces, parens) are sanitized, not passed through raw —
    # R2/S3 object keys tolerate them, but sanitizing avoids surprises in
    # Content-Disposition headers and URL encoding downstream.
    assert " " not in key
    assert "(" not in key


def test_new_key_uses_personal_prefix_when_orgless():
    import storage

    key = storage._new_key(None, "proposal_export", "report.txt")
    assert key.startswith("personal/proposal_export/")


def test_new_key_is_unique_per_call():
    import storage

    key1 = storage._new_key("org-123", "proposal_export", "report.txt")
    key2 = storage._new_key("org-123", "proposal_export", "report.txt")
    assert key1 != key2  # new_uuid() per call — same filename never collides


def test_sha256_hex_matches_hashlib_and_is_deterministic():
    import storage

    content = b"Clariva file storage Phase A/B checksum test"
    expected = hashlib.sha256(content).hexdigest()
    assert storage.sha256_hex(content) == expected
    assert storage.sha256_hex(content) == storage.sha256_hex(content)
    assert storage.sha256_hex(b"different content") != expected
