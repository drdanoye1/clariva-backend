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

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models.db_models import Proposal, ProposalSection, User
from models.schemas import DocumentFormat, ExportRequest, ExportResponse
from routers.auth import get_current_user
from engines.document_output import DocumentOutputEngine

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
    )
    await db.commit()

    return ExportResponse(
        proposal_id=body.proposal_id,
        format=body.format,
        download_url=export_info["download_url"],
        file_size_bytes=export_info["file_size"],
        exported_at=export_info["exported_at"],
    )
