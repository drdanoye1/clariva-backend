"""
Engine 14 — Document Library Engine
Versioned document library with internal/external sharing, AI semantic
search, and per-org retention policies (Clariva Enterprise™ PRD §14).

Design notes (same discipline as engines/credit_engine.py,
engines/scope_of_work_engine.py, engines/collaboration_engine.py):
- Every method takes an already-open AsyncSession and only flushes, never
  commits — the caller's request-scoped session commits once, after the
  router handler returns (see database.py::get_db).
- No permission checks live here — routers/documents_library.py gates
  access via workspace_access.py (proposal-scoped documents) or
  rbac.py's ROLE_PERMISSIONS (org-level document/retention management).
- `Document` never carries a `current_version_id` pointer (avoids a
  circular FK under SQLite) — "current version" is always the
  `DocumentVersion` row with the highest `version_number` for that
  document_id, resolved on demand by `get_latest_version`.

Semantic search (explicit product decision — real AI search, not just
keyword matching, per Phase 3 scoping): mirrors engines/memory_system.py's
`_embed` / `_vector_search` / `_keyword_search` split exactly.
- `_embed` calls OpenAI's embeddings API (settings.OPENAI_EMBEDDING_MODEL).
- Embedding generation during `create_document`/`publish_version` is
  best-effort and non-blocking: wrapped in try/except so an API outage (or
  a dummy test API key) never prevents a version from saving. `embedding`
  stays null on failure, and search transparently falls back to keyword
  matching for that version — same "degrades gracefully" behavior the
  memory system already relies on in dev/test (SQLite has no pgvector).
- `semantic_search()` itself also wraps `_vector_search` in try/except and
  falls back to `_keyword_search` on any exception (missing pgvector
  extension, no embeddings yet, etc.) — so this always returns *something*
  for a query, never just raises.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import openai
from fastapi import HTTPException
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.db_models import Document, DocumentShare, DocumentVersion, RetentionPolicy, new_uuid


class DocumentLibraryEngine:
    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    # ── Documents & Versions ─────────────────────────────────────────────────

    async def create_document(
        self, db: AsyncSession, org_id: str, created_by: str, data: Dict[str, Any],
    ) -> Document:
        doc = Document(
            id=new_uuid(), org_id=org_id, proposal_id=data.get("proposal_id"),
            library_type=data.get("library_type", "document"), title=data["title"],
            created_by=created_by,
        )
        db.add(doc)
        await db.flush()
        await db.refresh(doc)

        await self._add_version(
            db, doc.id, created_by, content=data.get("content"),
            file_url=data.get("file_url"), format=data.get("format"),
            change_note=data.get("change_note") or "Initial version",
        )
        return doc

    async def publish_version(
        self, db: AsyncSession, document_id: str, created_by: str, data: Dict[str, Any],
    ) -> DocumentVersion:
        await self.get_document_or_404(db, document_id)
        return await self._add_version(
            db, document_id, created_by, content=data.get("content"),
            file_url=data.get("file_url"), format=data.get("format"),
            change_note=data.get("change_note"),
        )

    async def _add_version(
        self, db: AsyncSession, document_id: str, created_by: str,
        content: Optional[str], file_url: Optional[str],
        format: Optional[str], change_note: Optional[str],
    ) -> DocumentVersion:
        next_number = await self._next_version_number(db, document_id)
        version = DocumentVersion(
            id=new_uuid(), document_id=document_id, version_number=next_number,
            content=content, file_url=file_url, format=format, change_note=change_note,
            created_by=created_by,
        )
        db.add(version)
        await db.flush()
        await db.refresh(version)

        # Best-effort, non-blocking — see module docstring.
        if content:
            try:
                embedding = await self._embed(content)
                version.embedding = embedding
                await db.flush()
                await db.refresh(version)
            except Exception:
                pass

        doc = await self.get_document_or_404(db, document_id)
        doc.updated_at = datetime.utcnow()
        await db.flush()
        return version

    async def _next_version_number(self, db: AsyncSession, document_id: str) -> int:
        result = await db.execute(
            select(sa_func.max(DocumentVersion.version_number)).where(DocumentVersion.document_id == document_id)
        )
        current_max = result.scalar()
        return (current_max or 0) + 1

    async def get_document_or_404(self, db: AsyncSession, document_id: str) -> Document:
        result = await db.execute(select(Document).where(Document.id == document_id))
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        return doc

    async def list_documents(
        self, db: AsyncSession, org_id: str, proposal_id: Optional[str] = None,
        library_type: Optional[str] = None, status: Optional[str] = "active",
    ) -> List[Document]:
        query = select(Document).where(Document.org_id == org_id)
        if proposal_id:
            query = query.where(Document.proposal_id == proposal_id)
        if library_type:
            query = query.where(Document.library_type == library_type)
        if status:
            query = query.where(Document.status == status)
        result = await db.execute(query.order_by(Document.created_at.desc()))
        return list(result.scalars().all())

    async def get_latest_version(self, db: AsyncSession, document_id: str) -> Optional[DocumentVersion]:
        result = await db.execute(
            select(DocumentVersion).where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version_number.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def list_versions(self, db: AsyncSession, document_id: str) -> List[DocumentVersion]:
        result = await db.execute(
            select(DocumentVersion).where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version_number.desc())
        )
        return list(result.scalars().all())

    async def count_versions(self, db: AsyncSession, document_id: str) -> int:
        result = await db.execute(
            select(sa_func.count(DocumentVersion.id)).where(DocumentVersion.document_id == document_id)
        )
        return result.scalar() or 0

    async def set_document_status(self, db: AsyncSession, document_id: str, status: str) -> Document:
        doc = await self.get_document_or_404(db, document_id)
        doc.status = status
        doc.updated_at = datetime.utcnow()
        await db.flush()
        await db.refresh(doc)
        return doc

    async def delete_document(self, db: AsyncSession, document_id: str) -> None:
        doc = await self.get_document_or_404(db, document_id)
        await db.delete(doc)  # cascades to versions/shares
        await db.flush()

    # ── Sharing (internal + external token links, PRD §14) ──────────────────

    async def create_share(
        self, db: AsyncSession, document_id: str, created_by: str, data: Dict[str, Any],
    ) -> DocumentShare:
        await self.get_document_or_404(db, document_id)
        if not data.get("shared_with_user_id") and not data.get("external_email"):
            raise HTTPException(status_code=400, detail="Must specify either shared_with_user_id or external_email.")

        expires_at = None
        if data.get("expires_in_days"):
            expires_at = datetime.utcnow() + timedelta(days=data["expires_in_days"])

        share = DocumentShare(
            id=new_uuid(), document_id=document_id,
            shared_with_user_id=data.get("shared_with_user_id"),
            external_email=data.get("external_email"),
            share_token=secrets.token_urlsafe(32) if data.get("external_email") else None,
            permission=data.get("permission", "view"),
            expires_at=expires_at, created_by=created_by,
        )
        db.add(share)
        await db.flush()
        await db.refresh(share)
        return share

    async def list_shares(self, db: AsyncSession, document_id: str) -> List[DocumentShare]:
        result = await db.execute(select(DocumentShare).where(DocumentShare.document_id == document_id))
        return list(result.scalars().all())

    async def revoke_share(self, db: AsyncSession, share_id: str) -> None:
        result = await db.execute(select(DocumentShare).where(DocumentShare.id == share_id))
        share = result.scalar_one_or_none()
        if not share:
            raise HTTPException(status_code=404, detail="Share not found")
        await db.delete(share)
        await db.flush()

    async def resolve_share_token(self, db: AsyncSession, share_token: str) -> DocumentShare:
        """Unauthenticated external access — used by the public share-link endpoint."""
        result = await db.execute(select(DocumentShare).where(DocumentShare.share_token == share_token))
        share = result.scalar_one_or_none()
        if not share:
            raise HTTPException(status_code=404, detail="Invalid or expired share link.")
        if share.expires_at and share.expires_at < datetime.utcnow():
            raise HTTPException(status_code=410, detail="This share link has expired.")
        return share

    # ── Semantic Search (real AI search, per explicit product decision) ────

    async def semantic_search(
        self, db: AsyncSession, org_id: str, query: str,
        library_type: Optional[str] = None, limit: int = 10,
    ) -> List[Dict[str, Any]]:
        try:
            embedding = await self._embed(query)
            results = await self._vector_search(embedding, org_id, db, limit, library_type)
            if results:
                return results
            # No pgvector matches (e.g. no embeddings indexed yet) — still
            # worth trying keyword matching rather than returning nothing.
            return await self._keyword_search(query, org_id, db, limit, library_type)
        except Exception:
            return await self._keyword_search(query, org_id, db, limit, library_type)

    async def _embed(self, text: str) -> List[float]:
        response = await self.client.embeddings.create(
            model=settings.OPENAI_EMBEDDING_MODEL,
            input=text[:8000],
        )
        return response.data[0].embedding

    async def _vector_search(
        self, embedding: List[float], org_id: str, db: AsyncSession,
        limit: int, library_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """pgvector cosine similarity search — Postgres only, requires the
        pgvector extension. Raises on SQLite (dev/test), which
        semantic_search() catches and falls back from."""
        from sqlalchemy import text as sql_text

        type_filter = "AND d.library_type = :library_type" if library_type else ""
        query = sql_text(f"""
            SELECT dv.id AS version_id, dv.document_id, d.title, d.library_type,
                   dv.content, 1 - (dv.embedding <=> :embedding) AS similarity
            FROM document_versions dv
            JOIN documents d ON d.id = dv.document_id
            WHERE d.org_id = :org_id
              AND d.status = 'active'
              AND dv.embedding IS NOT NULL
              {type_filter}
            ORDER BY similarity DESC
            LIMIT :limit
        """)
        params: Dict[str, Any] = {"embedding": json.dumps(embedding), "org_id": org_id, "limit": limit}
        if library_type:
            params["library_type"] = library_type
        result = await db.execute(query, params)
        return [
            {
                "document_id": row.document_id, "version_id": row.version_id,
                "title": row.title, "library_type": row.library_type,
                "snippet": (row.content or "")[:280], "similarity": row.similarity,
            }
            for row in result
        ]

    async def _keyword_search(
        self, query: str, org_id: str, db: AsyncSession,
        limit: int, library_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        keywords = [k for k in query.lower().split() if k][:5]
        q = select(Document, DocumentVersion).join(
            DocumentVersion, DocumentVersion.document_id == Document.id
        ).where(Document.org_id == org_id, Document.status == "active")
        if library_type:
            q = q.where(Document.library_type == library_type)
        if keywords:
            from sqlalchemy import or_
            conditions = []
            for kw in keywords:
                conditions.append(Document.title.ilike(f"%{kw}%"))
                conditions.append(DocumentVersion.content.ilike(f"%{kw}%"))
            q = q.where(or_(*conditions))
        q = q.order_by(DocumentVersion.version_number.desc()).limit(limit * 3)  # dedupe below
        result = await db.execute(q)

        seen_documents: set = set()
        out: List[Dict[str, Any]] = []
        for doc, version in result.all():
            if doc.id in seen_documents:
                continue
            seen_documents.add(doc.id)
            out.append({
                "document_id": doc.id, "version_id": version.id,
                "title": doc.title, "library_type": doc.library_type,
                "snippet": (version.content or "")[:280], "similarity": None,
            })
            if len(out) >= limit:
                break
        return out

    # ── Retention Policies (schema + manual archive action) ─────────────────

    async def set_retention_policy(
        self, db: AsyncSession, org_id: str, library_type: str, retention_days: Optional[int],
    ) -> RetentionPolicy:
        result = await db.execute(
            select(RetentionPolicy).where(RetentionPolicy.org_id == org_id, RetentionPolicy.library_type == library_type)
        )
        policy = result.scalar_one_or_none()
        if policy:
            policy.retention_days = retention_days
        else:
            policy = RetentionPolicy(id=new_uuid(), org_id=org_id, library_type=library_type, retention_days=retention_days)
            db.add(policy)
        await db.flush()
        await db.refresh(policy)
        return policy

    async def list_retention_policies(self, db: AsyncSession, org_id: str) -> List[RetentionPolicy]:
        result = await db.execute(select(RetentionPolicy).where(RetentionPolicy.org_id == org_id))
        return list(result.scalars().all())

    async def archive_expired(self, db: AsyncSession, org_id: str) -> int:
        """
        Manual archive action (no background scheduler in this environment)
        — an org admin triggers this on demand. For each library_type with a
        retention_days policy set, archive active documents whose most
        recent version predates the retention window.
        """
        policies = await self.list_retention_policies(db, org_id)
        archived_count = 0
        for policy in policies:
            if not policy.retention_days:
                continue
            cutoff = datetime.utcnow() - timedelta(days=policy.retention_days)
            docs = await self.list_documents(db, org_id, library_type=policy.library_type, status="active")
            for doc in docs:
                latest = await self.get_latest_version(db, doc.id)
                reference_date = latest.created_at if latest else doc.created_at
                if reference_date and reference_date.replace(tzinfo=None) < cutoff:
                    doc.status = "archived"
                    doc.updated_at = datetime.utcnow()
                    archived_count += 1
        if archived_count:
            await db.flush()
        return archived_count
