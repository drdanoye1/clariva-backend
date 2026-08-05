"""
Document Library router — versioned documents, internal/external sharing,
AI semantic search, and per-org retention policies (Clariva Enterprise™
PRD §14). Mounted at /api/v1.

Access model: unlike collaboration.py's tasks/comments/approvals (which
can live on a personal, unshared proposal via workspace_access.py),
Document.org_id is NOT NULL by design (see document_library_engine.py's
module docstring) — the document library is an org-level concept. All
document management endpoints are therefore gated the same way
organizations.py/credits.py already gate org-level features: via
rbac.py's ROLE_PERMISSIONS through _assert_member/_assert_permission,
reused directly rather than reimplemented.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.schemas import (
    ArchiveExpiredOut, DocumentCreate, DocumentOut, DocumentSearchResult,
    DocumentShareCreate, DocumentShareOut, DocumentVersionCreate, DocumentVersionOut,
    RetentionPolicyOut, RetentionPolicyRequest,
)
from models.db_models import DocumentShare, User
from routers.auth import get_current_user
from routers.organizations import _assert_member, _assert_permission
from engines.document_library_engine import DocumentLibraryEngine
from audit import log_action

router = APIRouter()
engine = DocumentLibraryEngine()


async def _to_document_out(db: AsyncSession, doc) -> DocumentOut:
    version_count = await engine.count_versions(db, doc.id)
    latest = await engine.get_latest_version(db, doc.id)
    latest_out = None
    if latest:
        latest_out = DocumentVersionOut(
            id=latest.id, document_id=latest.document_id, version_number=latest.version_number,
            content=latest.content, file_url=latest.file_url, format=latest.format,
            change_note=latest.change_note, has_embedding=latest.embedding is not None,
            created_by=latest.created_by, created_at=latest.created_at,
        )
    return DocumentOut(
        id=doc.id, org_id=doc.org_id, proposal_id=doc.proposal_id, library_type=doc.library_type,
        title=doc.title, status=doc.status, version_count=version_count, latest_version=latest_out,
        created_by=doc.created_by, created_at=doc.created_at, updated_at=doc.updated_at,
    )


# ── Documents ────────────────────────────────────────────────────────────────

@router.post("/organizations/{org_id}/documents", response_model=DocumentOut, status_code=201)
async def create_document(
    org_id: str, body: DocumentCreate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _assert_permission(org_id, current_user.id, "manage_documents", db)
    doc = await engine.create_document(db, org_id, current_user.id, body.model_dump())
    await log_action(db, actor_id=current_user.id, action="document.created", org_id=org_id,
                      object_type="document", object_id=doc.id, detail={"title": doc.title})
    return await _to_document_out(db, doc)


@router.get("/organizations/{org_id}/documents", response_model=List[DocumentOut])
async def list_documents(
    org_id: str, proposal_id: Optional[str] = None, library_type: Optional[str] = None,
    status: Optional[str] = "active",
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _assert_member(org_id, current_user.id, db)
    docs = await engine.list_documents(db, org_id, proposal_id=proposal_id, library_type=library_type, status=status)
    return [await _to_document_out(db, d) for d in docs]


@router.get("/organizations/{org_id}/documents/search", response_model=List[DocumentSearchResult])
async def search_documents(
    org_id: str, q: str = Query(..., min_length=1), library_type: Optional[str] = None, limit: int = 10,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Real AI semantic search (OpenAI embeddings) with automatic keyword
    fallback — see document_library_engine.py::semantic_search."""
    await _assert_member(org_id, current_user.id, db)
    results = await engine.semantic_search(db, org_id, q, library_type=library_type, limit=min(limit, 50))
    return [DocumentSearchResult(**r) for r in results]


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    doc = await engine.get_document_or_404(db, document_id)
    await _assert_member(doc.org_id, current_user.id, db)
    return await _to_document_out(db, doc)


@router.get("/documents/{document_id}/versions", response_model=List[DocumentVersionOut])
async def list_document_versions(
    document_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    doc = await engine.get_document_or_404(db, document_id)
    await _assert_member(doc.org_id, current_user.id, db)
    versions = await engine.list_versions(db, document_id)
    return [
        DocumentVersionOut(
            id=v.id, document_id=v.document_id, version_number=v.version_number,
            content=v.content, file_url=v.file_url, format=v.format, change_note=v.change_note,
            has_embedding=v.embedding is not None, created_by=v.created_by, created_at=v.created_at,
        )
        for v in versions
    ]


@router.post("/documents/{document_id}/versions", response_model=DocumentVersionOut, status_code=201)
async def publish_version(
    document_id: str, body: DocumentVersionCreate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    doc = await engine.get_document_or_404(db, document_id)
    await _assert_permission(doc.org_id, current_user.id, "manage_documents", db)
    version = await engine.publish_version(db, document_id, current_user.id, body.model_dump())
    await log_action(db, actor_id=current_user.id, action="document.version_published", org_id=doc.org_id,
                      object_type="document", object_id=document_id, detail={"version_number": version.version_number})
    return DocumentVersionOut(
        id=version.id, document_id=version.document_id, version_number=version.version_number,
        content=version.content, file_url=version.file_url, format=version.format,
        change_note=version.change_note, has_embedding=version.embedding is not None,
        created_by=version.created_by, created_at=version.created_at,
    )


@router.patch("/documents/{document_id}/archive", response_model=DocumentOut)
async def archive_document(
    document_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    doc = await engine.get_document_or_404(db, document_id)
    await _assert_permission(doc.org_id, current_user.id, "manage_documents", db)
    doc = await engine.set_document_status(db, document_id, "archived")
    await log_action(db, actor_id=current_user.id, action="document.archived", org_id=doc.org_id,
                      object_type="document", object_id=document_id)
    return await _to_document_out(db, doc)


@router.patch("/documents/{document_id}/restore", response_model=DocumentOut)
async def restore_document(
    document_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    doc = await engine.get_document_or_404(db, document_id)
    await _assert_permission(doc.org_id, current_user.id, "manage_documents", db)
    doc = await engine.set_document_status(db, document_id, "active")
    return await _to_document_out(db, doc)


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    doc = await engine.get_document_or_404(db, document_id)
    await _assert_permission(doc.org_id, current_user.id, "manage_documents", db)
    await log_action(db, actor_id=current_user.id, action="document.deleted", org_id=doc.org_id,
                      object_type="document", object_id=document_id, detail={"title": doc.title})
    await engine.delete_document(db, document_id)


# ── Sharing ──────────────────────────────────────────────────────────────────

@router.post("/documents/{document_id}/shares", response_model=DocumentShareOut, status_code=201)
async def create_share(
    document_id: str, body: DocumentShareCreate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    doc = await engine.get_document_or_404(db, document_id)
    await _assert_permission(doc.org_id, current_user.id, "manage_document_sharing", db)
    share = await engine.create_share(db, document_id, current_user.id, body.model_dump())
    await log_action(db, actor_id=current_user.id, action="document.shared", org_id=doc.org_id,
                      object_type="document", object_id=document_id,
                      detail={"external_email": body.external_email} if body.external_email else None)
    return share


@router.get("/documents/{document_id}/shares", response_model=List[DocumentShareOut])
async def list_shares(
    document_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    doc = await engine.get_document_or_404(db, document_id)
    await _assert_member(doc.org_id, current_user.id, db)
    return await engine.list_shares(db, document_id)


@router.delete("/shares/{share_id}", status_code=204)
async def revoke_share(
    share_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(DocumentShare).where(DocumentShare.id == share_id))
    share = result.scalar_one_or_none()
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    doc = await engine.get_document_or_404(db, share.document_id)
    await _assert_permission(doc.org_id, current_user.id, "manage_document_sharing", db)
    await engine.revoke_share(db, share_id)


@router.get("/shared/{share_token}")
async def view_shared_document(share_token: str, db: AsyncSession = Depends(get_db)):
    """Unauthenticated external access via share link — deliberately has no
    get_current_user dependency, matching PRD §14's "external-share links
    for partner/funder distribution" use case."""
    share = await engine.resolve_share_token(db, share_token)
    doc = await engine.get_document_or_404(db, share.document_id)
    latest = await engine.get_latest_version(db, doc.id)
    return {
        "title": doc.title,
        "library_type": doc.library_type,
        "permission": share.permission,
        "version_number": latest.version_number if latest else None,
        "content": latest.content if latest else None,
        "file_url": latest.file_url if latest else None,
        "format": latest.format if latest else None,
    }


# ── Retention Policies ───────────────────────────────────────────────────────

@router.put("/organizations/{org_id}/retention-policies", response_model=RetentionPolicyOut)
async def set_retention_policy(
    org_id: str, body: RetentionPolicyRequest,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _assert_permission(org_id, current_user.id, "manage_retention", db)
    policy = await engine.set_retention_policy(db, org_id, body.library_type, body.retention_days)
    await log_action(db, actor_id=current_user.id, action="retention_policy.updated", org_id=org_id,
                      object_type="retention_policy", object_id=policy.id,
                      detail={"library_type": body.library_type, "retention_days": body.retention_days})
    return policy


@router.get("/organizations/{org_id}/retention-policies", response_model=List[RetentionPolicyOut])
async def list_retention_policies(
    org_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _assert_member(org_id, current_user.id, db)
    return await engine.list_retention_policies(db, org_id)


@router.post("/organizations/{org_id}/documents/archive-expired", response_model=ArchiveExpiredOut)
async def archive_expired(
    org_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Manual archive action (no background scheduler in this environment,
    per Phase 3 retention scoping) — an org admin triggers this on demand."""
    await _assert_permission(org_id, current_user.id, "manage_retention", db)
    count = await engine.archive_expired(db, org_id)
    await log_action(db, actor_id=current_user.id, action="documents.archived_expired", org_id=org_id,
                      object_type="organization", object_id=org_id, detail={"archived_count": count})
    return ArchiveExpiredOut(archived_count=count)
