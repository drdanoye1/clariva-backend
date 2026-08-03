"""Document export router — TXT, DOCX, PDF."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
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

    export_info = await doc_engine.export(
        proposal=proposal,
        sections=sections,
        fmt=body.format,
        include_scoring=body.include_scoring_summary,
        include_reviewer=body.include_reviewer_feedback,
        include_compliance=body.include_compliance_status,
        generate_figures=body.generate_figures,
        format_options=body.format_options,
    )

    return ExportResponse(
        proposal_id=body.proposal_id,
        format=body.format,
        download_url=export_info["download_url"],
        file_size_bytes=export_info["file_size"],
        exported_at=export_info["exported_at"],
    )


@router.get("/download/{filename}")
async def download_file(filename: str):
    """Serve exported files from the temp export directory."""
    import os, tempfile
    export_dir = os.path.join(tempfile.gettempdir(), "sbir_exports")
    file_path = os.path.join(export_dir, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found or expired")
    media_types = {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".txt": "text/plain"}
    ext = os.path.splitext(filename)[1].lower()
    return FileResponse(file_path, filename=filename, media_type=media_types.get(ext, "application/octet-stream"))
