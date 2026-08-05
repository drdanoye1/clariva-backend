"""
Engine 14 — Document Library Engine.

Covers versioned document/version CRUD, sharing (internal + external
token links with expiration), retention policy CRUD, and manual
archive-expired — all deterministic, DB-only logic.

Semantic search's embedding-generation half (`_embed`, and therefore
`_vector_search` when it's actually reachable) needs a real OpenAI call and
is excluded from this suite, same policy as memory_system.py's
semantic_search in test_memory_engine.py. What IS covered here is the
part that matters most for correctness without a live API key: that
`semantic_search()` gracefully falls back to `_keyword_search` (SQLite has
no pgvector, so `_vector_search` always raises here, and the dummy
"sk-test-not-a-real-key" API key makes `_embed` itself fail before that) —
so keyword search is exercised directly, and indirectly through the public
`semantic_search` entrypoint.

Like the other engine test files, methods are async and use asyncio.run()
directly rather than pytest-asyncio; the `client` fixture triggers app
lifespan startup so the Phase 3 document tables exist.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from database import AsyncSessionLocal
from engines.document_library_engine import DocumentLibraryEngine


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def engine():
    return DocumentLibraryEngine()


def _org_id() -> str:
    return f"test-org-{uuid.uuid4().hex[:12]}"


# ── Documents & Versions ─────────────────────────────────────────────────────

def test_create_document_creates_first_version(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            doc = await engine.create_document(db, org_id, "user-1", {
                "title": "Methodology Draft", "library_type": "methodology", "content": "We propose a phased approach.",
            })
            await db.commit()
        async with AsyncSessionLocal() as db:
            versions = await engine.list_versions(db, doc.id)
            return doc, versions

    doc, versions = _run(_body())
    assert doc.title == "Methodology Draft"
    assert doc.status == "active"
    assert len(versions) == 1
    assert versions[0].version_number == 1
    assert versions[0].change_note == "Initial version"


def test_publish_version_increments_version_number(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            doc = await engine.create_document(db, org_id, "user-1", {"title": "Budget Narrative", "content": "v1 text"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            v2 = await engine.publish_version(db, doc.id, "user-1", {"content": "v2 text", "change_note": "Revised numbers"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            latest = await engine.get_latest_version(db, doc.id)
            count = await engine.count_versions(db, doc.id)
            return v2, latest, count

    v2, latest, count = _run(_body())
    assert v2.version_number == 2
    assert latest.id == v2.id
    assert latest.content == "v2 text"
    assert count == 2


def test_publish_version_on_missing_document_raises_404(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.publish_version(db, "does-not-exist", "user-1", {"content": "x"})
            return exc_info.value.status_code

    assert _run(_body()) == 404


def test_list_documents_filters_by_library_type_and_status(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_document(db, org_id, "user-1", {"title": "Doc A", "library_type": "template"})
            doc_b = await engine.create_document(db, org_id, "user-1", {"title": "Doc B", "library_type": "graphics"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.set_document_status(db, doc_b.id, "archived")
            await db.commit()
        async with AsyncSessionLocal() as db:
            templates = await engine.list_documents(db, org_id, library_type="template")
            active_only = await engine.list_documents(db, org_id, status="active")
            all_docs = await engine.list_documents(db, org_id, status=None)
            return templates, active_only, all_docs

    templates, active_only, all_docs = _run(_body())
    assert [d.title for d in templates] == ["Doc A"]
    assert [d.title for d in active_only] == ["Doc A"]
    assert len(all_docs) == 2


def test_archive_and_restore_document(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            doc = await engine.create_document(db, org_id, "user-1", {"title": "Old Award"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            archived = await engine.set_document_status(db, doc.id, "archived")
            await db.commit()
        async with AsyncSessionLocal() as db:
            restored = await engine.set_document_status(db, doc.id, "active")
            return archived.status, restored.status

    archived_status, restored_status = _run(_body())
    assert archived_status == "archived"
    assert restored_status == "active"


def test_delete_document_cascades_to_versions(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            doc = await engine.create_document(db, org_id, "user-1", {"title": "Temp Doc", "content": "content"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.delete_document(db, doc.id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.get_document_or_404(db, doc.id)
            return exc_info.value.status_code

    assert _run(_body()) == 404


# ── Sharing ──────────────────────────────────────────────────────────────────

def test_create_internal_share_has_no_token(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            doc = await engine.create_document(db, org_id, "user-1", {"title": "Shared Internally"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            share = await engine.create_share(db, doc.id, "user-1", {"shared_with_user_id": "user-2", "permission": "comment"})
            return share

    share = _run(_body())
    assert share.shared_with_user_id == "user-2"
    assert share.share_token is None


def test_create_external_share_generates_token_and_expiry(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            doc = await engine.create_document(db, org_id, "user-1", {"title": "Shared Externally"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            share = await engine.create_share(db, doc.id, "user-1", {"external_email": "partner@example.org", "expires_in_days": 7})
            await db.commit()
        async with AsyncSessionLocal() as db:
            resolved = await engine.resolve_share_token(db, share.share_token)
            return share, resolved

    share, resolved = _run(_body())
    assert share.share_token is not None
    assert share.expires_at is not None
    assert resolved.id == share.id


def test_create_share_without_target_raises_400(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            doc = await engine.create_document(db, org_id, "user-1", {"title": "No Target"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.create_share(db, doc.id, "user-1", {})
            return exc_info.value.status_code

    assert _run(_body()) == 400


def test_resolve_expired_share_token_raises_410(client, engine):
    from datetime import datetime, timedelta

    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            doc = await engine.create_document(db, org_id, "user-1", {"title": "Expiring Doc"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            share = await engine.create_share(db, doc.id, "user-1", {"external_email": "late@example.org"})
            share.expires_at = datetime.utcnow() - timedelta(days=1)
            await db.commit()
            token = share.share_token
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.resolve_share_token(db, token)
            return exc_info.value.status_code

    assert _run(_body()) == 410


def test_revoke_share(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            doc = await engine.create_document(db, org_id, "user-1", {"title": "Doc"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            share = await engine.create_share(db, doc.id, "user-1", {"shared_with_user_id": "user-2"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.revoke_share(db, share.id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.list_shares(db, doc.id)

    assert _run(_body()) == []


# ── Semantic Search (keyword fallback only — no OpenAI calls in tests) ──────

def test_keyword_search_matches_title_and_content(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_document(db, org_id, "user-1", {"title": "Nanoparticle Delivery Systems", "content": "lipid encapsulation"})
            await engine.create_document(db, org_id, "user-1", {"title": "Unrelated Budget Sheet", "content": "line items and totals"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine._keyword_search("nanoparticle", org_id, db, 10)

    results = _run(_body())
    assert len(results) == 1
    assert results[0]["title"] == "Nanoparticle Delivery Systems"
    assert results[0]["similarity"] is None


def test_semantic_search_falls_back_to_keyword_when_embeddings_unavailable(client, engine):
    """No real OpenAI key in the test environment (conftest.py sets a dummy
    key), so _embed always fails and semantic_search() must transparently
    fall back to _keyword_search rather than raising."""
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_document(db, org_id, "user-1", {"title": "CRISPR Delivery Mechanism", "content": "gene editing platform"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.semantic_search(db, org_id, "CRISPR", limit=5)

    results = _run(_body())
    assert len(results) == 1
    assert results[0]["title"] == "CRISPR Delivery Mechanism"


def test_keyword_search_scoped_to_org(client, engine):
    org_a, org_b = _org_id(), _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_document(db, org_a, "user-1", {"title": "Shared Keyword Doc", "content": "text"})
            await engine.create_document(db, org_b, "user-1", {"title": "Shared Keyword Doc", "content": "text"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine._keyword_search("shared", org_a, db, 10)

    results = _run(_body())
    assert len(results) == 1


# ── Retention Policies & Archive-Expired ─────────────────────────────────────

def test_set_and_update_retention_policy(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            policy = await engine.set_retention_policy(db, org_id, "document", 90)
            await db.commit()
        async with AsyncSessionLocal() as db:
            updated = await engine.set_retention_policy(db, org_id, "document", 180)
            await db.commit()
        async with AsyncSessionLocal() as db:
            policies = await engine.list_retention_policies(db, org_id)
            return policy, updated, policies

    policy, updated, policies = _run(_body())
    assert policy.id == updated.id  # same row updated, not duplicated
    assert updated.retention_days == 180
    assert len(policies) == 1


def test_archive_expired_archives_only_old_documents_of_that_type(client, engine):
    from datetime import datetime, timedelta

    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.set_retention_policy(db, org_id, "document", 30)
            old_doc = await engine.create_document(db, org_id, "user-1", {"title": "Old", "content": "x"})
            new_doc = await engine.create_document(db, org_id, "user-1", {"title": "New", "content": "y"})
            other_type_doc = await engine.create_document(db, org_id, "user-1", {"title": "Other", "library_type": "template", "content": "z"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            old_version = await engine.get_latest_version(db, old_doc.id)
            old_version.created_at = datetime.utcnow() - timedelta(days=60)
            await db.commit()
        async with AsyncSessionLocal() as db:
            archived_count = await engine.archive_expired(db, org_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            old_reloaded = await engine.get_document_or_404(db, old_doc.id)
            new_reloaded = await engine.get_document_or_404(db, new_doc.id)
            other_reloaded = await engine.get_document_or_404(db, other_type_doc.id)
            return archived_count, old_reloaded.status, new_reloaded.status, other_reloaded.status

    archived_count, old_status, new_status, other_status = _run(_body())
    assert archived_count == 1
    assert old_status == "archived"
    assert new_status == "active"       # too recent to expire
    assert other_status == "active"     # different library_type, no policy set for it


def test_archive_expired_no_policies_archives_nothing(client, engine):
    org_id = _org_id()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_document(db, org_id, "user-1", {"title": "Doc"})
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.archive_expired(db, org_id)

    assert _run(_body()) == 0
