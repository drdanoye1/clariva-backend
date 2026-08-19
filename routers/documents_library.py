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

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.schemas import (
    ArchiveExpiredOut, DocumentCreate, DocumentOut, DocumentSearchResult,
    DocumentShareCreate, DocumentShareOut, DocumentVersionCreate, DocumentVersionOut,
    GenerateSupportingDocumentRequest, LOGIC_MODEL_PRESERVED_ON_SWITCH, LOGIC_MODEL_STAGE_KEYS,
    LogicModelRegenerateStageRequest, LogicModelSwitchFrameworkRequest,
    RetentionPolicyOut, RetentionPolicyRequest, SupportingDocumentTypeOut,
)
from models.db_models import DocumentShare, User
from routers.auth import get_current_user
from routers.organizations import _assert_member, _assert_permission
from routers.proposals import _get_proposal_or_404, _load_company_profile
from engines.document_library_engine import DocumentLibraryEngine
from engines.scope_of_work_engine import ScopeOfWorkEngine
from engines.supporting_documents_engine import SUPPORTING_DOCUMENT_TYPES, SupportingDocumentsEngine
from engines.credit_engine import InsufficientCreditsError
from engines.service_catalog_engine import ServiceCatalogEngine
from audit import log_action

router = APIRouter()
engine = DocumentLibraryEngine()
supporting_docs_engine = SupportingDocumentsEngine()
sow_engine = ScopeOfWorkEngine()
catalog_engine = ServiceCatalogEngine()


def _to_version_out(version) -> DocumentVersionOut:
    """Shared DocumentVersion -> DocumentVersionOut mapping — single place
    that knows about structured_data/framework (Logic Model Chart
    Generator, Development Brief 2026-08-14) so every endpoint returning a
    version (document fetch, version list, publish, and the new
    logic-model generate/regenerate endpoints below) stays in sync rather
    than re-listing the same fields at each call site, which is how the
    original two call sites here had already started to drift before this
    helper existed."""
    return DocumentVersionOut(
        id=version.id, document_id=version.document_id, version_number=version.version_number,
        content=version.content, file_url=version.file_url, format=version.format,
        change_note=version.change_note, has_embedding=version.embedding is not None,
        structured_data=version.structured_data, framework=version.framework,
        created_by=version.created_by, created_at=version.created_at,
    )


async def _to_document_out(db: AsyncSession, doc) -> DocumentOut:
    version_count = await engine.count_versions(db, doc.id)
    latest = await engine.get_latest_version(db, doc.id)
    latest_out = _to_version_out(latest) if latest else None
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


@router.get("/supporting-document-types", response_model=List[SupportingDocumentTypeOut])
async def list_supporting_document_types(current_user: User = Depends(get_current_user)):
    """Registry backing the "Generate Supporting Document" type dropdown —
    same pattern as GET /connectors/types (Phase 6)."""
    return [
        SupportingDocumentTypeOut(key=key, label=meta["label"], category=meta["category"])
        for key, meta in SUPPORTING_DOCUMENT_TYPES.items()
    ]


@router.post("/organizations/{org_id}/documents/generate-supporting", response_model=DocumentOut, status_code=201)
async def generate_supporting_document(
    org_id: str, body: GenerateSupportingDocumentRequest, db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """AI-drafts one of the Communication/Partnership supporting documents
    (Cover Letter, Letter of Inquiry, Concept Paper, Letter of Support,
    Letter of Commitment, MOU — see engines/supporting_documents_engine.py)
    grounded in the given proposal's Project Knowledge Base and the user's
    org profile, then saves it as a normal versioned Document in this org's
    library — no separate storage path. Requires the same `manage_documents`
    permission as a manual document creation.

    Charging (Phase 3 §4.7 billing wire-up): each doc_type maps 1:1 to a
    `doc_*` service catalog entry (e.g. "cover_letter" -> "doc_cover_letter")
    — priced individually ($5-$85, see SERVICE_CATALOG_SEED) rather than the
    flat GENERATION_COST every other AI call in this app defaults to, since
    the catalog's whole point is per-service pricing for exactly these
    supporting documents. `service_catalog_engine.consume()` applies the
    org's complimentary allowance first (doc_* entries share one bucket, see
    `_find_available_entitlement`'s docstring) before charging the paid AI
    Services balance, same as every other catalog-priced service."""
    if body.doc_type not in SUPPORTING_DOCUMENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown supporting document type '{body.doc_type}'.")
    await _assert_permission(org_id, current_user.id, "manage_documents", db)
    proposal = await _get_proposal_or_404(body.proposal_id, current_user.id, db)
    service_key = f"doc_{body.doc_type}"
    try:
        txn = await catalog_engine.consume(
            db, org_id, current_user.id, service_key,
            reference={"proposal_id": body.proposal_id, "doc_type": body.doc_type},
        )
    except InsufficientCreditsError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    project_knowledge = await sow_engine.get_or_create_project_knowledge(db, body.proposal_id)
    company_profile = await _load_company_profile(current_user.id, db)
    meta = SUPPORTING_DOCUMENT_TYPES[body.doc_type]
    title = f"{meta['label']} — {proposal.title}"
    doc_data: Dict[str, Any] = {
        "title": title, "library_type": body.doc_type, "proposal_id": body.proposal_id,
        "format": "txt", "change_note": "AI-generated draft",
    }
    if body.doc_type == "logic_model":
        # Logic Model Chart Generator (Development Brief 2026-08-14) —
        # structured-JSON generation path (engines/supporting_documents_
        # engine.py::generate_logic_model), not the shared prose generate().
        # `content` is derived from structured_data so the existing
        # embedding/search/display pipeline in document_library_engine.py
        # keeps working unchanged (brief §18).
        structured_data = await supporting_docs_engine.generate_logic_model(
            proposal, project_knowledge, company_profile, framework=body.framework or "standard",
            additional_context=body.additional_context,
            db=db, org_id=org_id, user_id=current_user.id,
            price_cents_charged=txn.price_cents,
        )
        doc_data["structured_data"] = structured_data
        doc_data["framework"] = structured_data.get("framework")
        doc_data["content"] = supporting_docs_engine.flatten_logic_model_to_text(structured_data)
    else:
        doc_data["content"] = await supporting_docs_engine.generate(
            proposal, project_knowledge, company_profile, body.doc_type,
            recipient_name=body.recipient_name, recipient_organization=body.recipient_organization,
            additional_context=body.additional_context,
            db=db, org_id=org_id, user_id=current_user.id,
            price_cents_charged=txn.price_cents,
        )
    doc = await engine.create_document(db, org_id, current_user.id, doc_data)
    await log_action(db, actor_id=current_user.id, action="document.created", org_id=org_id,
                      object_type="document", object_id=doc.id, detail={"title": doc.title, "generated": True})
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
    return [_to_version_out(v) for v in versions]


@router.post("/documents/{document_id}/versions", response_model=DocumentVersionOut, status_code=201)
async def publish_version(
    document_id: str, body: DocumentVersionCreate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    doc = await engine.get_document_or_404(db, document_id)
    await _assert_permission(doc.org_id, current_user.id, "manage_documents", db)
    data = body.model_dump()
    # Logic Model bullet editing (frontend LogicModelChart's "Edit" mode)
    # publishes structured_data without a hand-typed `content` — auto-derive
    # it here via the same flatten used by generation/regeneration, so the
    # embedding/search/display pipeline (document_library_engine.py) never
    # sees a null `content` for an edited chart.
    if doc.library_type == "logic_model" and data.get("structured_data") and not data.get("content"):
        data["content"] = supporting_docs_engine.flatten_logic_model_to_text(data["structured_data"])
    version = await engine.publish_version(db, document_id, current_user.id, data)
    await log_action(db, actor_id=current_user.id, action="document.version_published", org_id=doc.org_id,
                      object_type="document", object_id=document_id, detail={"version_number": version.version_number})
    return _to_version_out(version)


async def _logic_model_edit_context(document_id: str, current_user: User, db: AsyncSession):
    """Shared setup for the two Logic Model editing endpoints below: loads
    the document + its latest version, checks it's actually a Logic Model
    with structured_data to edit (not a legacy prose version — brief §19),
    and loads the same proposal/project-knowledge/company-profile context
    generate_supporting_document used originally, so regeneration prompts
    stay grounded in the same source material."""
    doc = await engine.get_document_or_404(db, document_id)
    await _assert_permission(doc.org_id, current_user.id, "manage_documents", db)
    if doc.library_type != "logic_model":
        raise HTTPException(status_code=400, detail="This editing action only applies to Logic Model documents.")
    if not doc.proposal_id:
        raise HTTPException(status_code=400, detail="This Logic Model isn't linked to a proposal, so it can't be regenerated.")
    latest = await engine.get_latest_version(db, doc.id)
    if not latest or not latest.structured_data:
        raise HTTPException(status_code=400, detail="This Logic Model has no structured chart data yet — regenerate the whole document first to get one.")
    proposal = await _get_proposal_or_404(doc.proposal_id, current_user.id, db)
    project_knowledge = await sow_engine.get_or_create_project_knowledge(db, doc.proposal_id)
    company_profile = await _load_company_profile(current_user.id, db)
    return doc, latest, proposal, project_knowledge, company_profile


@router.post("/documents/{document_id}/logic-model/regenerate-stage", response_model=DocumentVersionOut, status_code=201)
async def regenerate_logic_model_stage(
    document_id: str, body: LogicModelRegenerateStageRequest,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Regenerates one stage of an existing Logic Model chart in place —
    Development Brief 2026-08-14's "Regenerate Stage" editing action.
    Publishes the result as a new document version (full version history is
    preserved, same as any other edit in this library)."""
    doc, latest, proposal, project_knowledge, company_profile = await _logic_model_edit_context(document_id, current_user, db)
    # Validate the stage name is real (structural, no AI needed) before
    # charging — same "cheap checks before spending credits" ordering
    # generate_supporting_document uses for doc_type.
    valid_stages = LOGIC_MODEL_STAGE_KEYS.get(latest.framework or "standard", LOGIC_MODEL_STAGE_KEYS["standard"])
    if body.stage not in valid_stages:
        raise HTTPException(status_code=400, detail=f"'{body.stage}' is not a stage in the {latest.framework} framework.")
    try:
        txn = await catalog_engine.consume(
            db, doc.org_id, current_user.id, "doc_logic_model_stage_regen",
            reference={"document_id": document_id, "stage": body.stage},
        )
    except InsufficientCreditsError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    updated_data = await supporting_docs_engine.regenerate_stage(
        proposal, project_knowledge, company_profile, latest.structured_data, body.stage,
        db=db, org_id=doc.org_id, user_id=current_user.id, price_cents_charged=txn.price_cents,
    )
    new_content = supporting_docs_engine.flatten_logic_model_to_text(updated_data)
    version = await engine.publish_version(db, doc.id, current_user.id, {
        "content": new_content, "structured_data": updated_data, "framework": updated_data.get("framework"),
        "change_note": f"Regenerated the {body.stage} stage",
    })
    await log_action(db, actor_id=current_user.id, action="document.version_published", org_id=doc.org_id,
                      object_type="document", object_id=doc.id, detail={"version_number": version.version_number, "stage_regenerated": body.stage})
    return _to_version_out(version)


@router.post("/documents/{document_id}/logic-model/switch-framework", response_model=DocumentVersionOut, status_code=201)
async def switch_logic_model_framework(
    document_id: str, body: LogicModelSwitchFrameworkRequest,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Switches a Logic Model between the standard/extended frameworks —
    Inputs/Activities/Outputs are preserved unchanged; only the outcome-
    stage(s) that differ between frameworks are regenerated (brief §10/§16).
    Billed once per new outcome-stage (2 for standard, 3 for extended) at
    the same doc_logic_model_stage_regen price as a single-stage regen,
    since that's exactly the AI work being done."""
    doc, latest, proposal, project_knowledge, company_profile = await _logic_model_edit_context(document_id, current_user, db)
    if body.framework not in LOGIC_MODEL_STAGE_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown framework '{body.framework}'.")
    if body.framework == latest.framework:
        raise HTTPException(status_code=400, detail=f"This Logic Model is already using the {body.framework} framework.")

    num_new_stages = len([k for k in LOGIC_MODEL_STAGE_KEYS[body.framework] if k not in LOGIC_MODEL_PRESERVED_ON_SWITCH])
    total_price_cents = 0
    try:
        for _ in range(num_new_stages):
            txn = await catalog_engine.consume(
                db, doc.org_id, current_user.id, "doc_logic_model_stage_regen",
                reference={"document_id": document_id, "framework_switch_to": body.framework},
            )
            total_price_cents += txn.price_cents
    except InsufficientCreditsError as exc:
        raise HTTPException(status_code=402, detail=str(exc))

    updated_data = await supporting_docs_engine.switch_framework(
        proposal, project_knowledge, company_profile, latest.structured_data, body.framework,
        db=db, org_id=doc.org_id, user_id=current_user.id, price_cents_charged=total_price_cents,
    )
    new_content = supporting_docs_engine.flatten_logic_model_to_text(updated_data)
    version = await engine.publish_version(db, doc.id, current_user.id, {
        "content": new_content, "structured_data": updated_data, "framework": updated_data.get("framework"),
        "change_note": f"Switched to the {body.framework} framework",
    })
    await log_action(db, actor_id=current_user.id, action="document.version_published", org_id=doc.org_id,
                      object_type="document", object_id=doc.id, detail={"version_number": version.version_number, "framework_switch": body.framework})
    return _to_version_out(version)


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
