"""
Document export router — TXT, DOCX, PDF.

Version 3.0 upgrade, "Real File Storage" scope (Phase B; see docs/
Clariva_File_Storage_Scoping_Document.docx) — the old GET /download/
{filename} endpoint (and the local-disk file it read from) is gone.
export_proposal() now persists the generated file to Cloudflare R2 via
DocumentOutputEngine.export()/storage.py and returns a real presigned
download URL directly in the response; nothing is served from this app's
own disk anymore. ExportResponse.download_url's shape is unchanged (still
a plain string), so no frontend change was required — pages/proposals/
[id].tsx's window.open(download_url) works identically whether the URL is
relative (old behavior) or an absolute presigned R2 URL (now).
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

import storage
from database import get_db
from models.db_models import Proposal, ProposalSection, StoredFile, User
from models.schemas import DocumentFormat, ExportRequest, ExportResponse, StoredFileOut
from routers.auth import get_current_user
from engines.document_output import DocumentOutputEngine, ExportBlockedError

router  = APIRouter()
doc_engine = DocumentOutputEngine()


@router.post("/export", response_model=ExportResponse)
async def export_proposal(
    body: ExportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Export a proposal to TXT, DOCX, or PDF."""
    result = await db.execute(
        select(Proposal).where(
            Proposal.id == body.proposal_id,
            Proposal.owner_id == current_user.id,
        )
    )
    proposal = result.scalar_one_or_none()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    sec_result = await db.execute(
        select(ProposalSection)
        .where(ProposalSection.proposal_id == body.proposal_id)
        .order_by(ProposalSection.order_index)
    )
    sections = sec_result.scalars().all()

    # org_id intentionally left None: proposal export has always been a
    # per-owner action gated on Proposal.owner_id above (not routed through
    # workspace_access.py's org-sharing resolver the way Award/Document
    # actions are), so the resulting StoredFile is scoped as a personal file
    # for now — StoredFile.org_id is nullable specifically to allow this
    # (see its docstring in models/db_models.py). Attributing proposal
    # exports to an org when the proposal is shared is a reasonable future
    # refinement, not required for this phase.
    try:
        export_info = await doc_engine.export(
            proposal=proposal,
            sections=sections,
            fmt=body.format,
            db=db,
            created_by=current_user.id,
            org_id=None,
            include_scoring=body.include_scoring_summary,
            include_reviewer=body.include_reviewer_feedback,
            include_compliance=body.include_compliance_status,
            generate_figures=body.generate_figures,
            format_options=body.format_options,
            require_clean_export=body.require_clean_export,
            brand_template_key=body.brand_template_key,
        )
    except ExportBlockedError as exc:
        # Phase 5 export QA gate — nothing was generated or uploaded; the
        # violations are the whole point of the error response, not just a
        # message, so the frontend can point the user at exactly what to fix.
        raise HTTPException(status_code=422, detail={
            "message": "Export blocked — resolve the issues below and try again, "
                       "or export without require_clean_export for a working draft.",
            "compliance_report": exc.report.model_dump(mode="json"),
        })
    await db.commit()

    return ExportResponse(
        proposal_id=body.proposal_id,
        format=body.format,
        download_url=export_info["download_url"],
        file_size_bytes=export_info["file_size"],
        exported_at=export_info["exported_at"],
        compliance_report=export_info.get("compliance_report"),
    )


@router.get("/exports/{proposal_id}", response_model=List[StoredFileOut])
async def list_proposal_exports(
    proposal_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Export history (Phase C) — export_proposal() above only ever returned
    a one-time download_url in its HTTP response; there was previously no
    way to come back later and find a file you'd already exported. Same
    ownership check as export_proposal() (Proposal.owner_id), since exports
    are still a per-owner action, not routed through workspace_access.py."""
    result = await db.execute(
        select(Proposal).where(Proposal.id == proposal_id, Proposal.owner_id == current_user.id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Proposal not found")

    files_result = await db.execute(
        select(StoredFile)
        .where(StoredFile.object_type == "proposal_export", StoredFile.object_id == proposal_id)
        .order_by(StoredFile.created_at.desc())
    )
    files = files_result.scalars().all()
    out = []
    for f in files:
        download_url = await storage.get_download_url(f.storage_key, filename=f.original_filename)
        out.append(StoredFileOut(
            id=f.id, original_filename=f.original_filename, content_type=f.content_type,
            size_bytes=f.size_bytes, created_at=f.created_at, download_url=download_url,
        ))
    return out
