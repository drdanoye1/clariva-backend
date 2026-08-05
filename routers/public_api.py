"""
Public API (Clariva Enterprise™ PRD §19: "a versioned public API...
enabling a partner/marketplace ecosystem"), mounted at /api/v1/public.

Authenticates via an `X-API-Key` header (api_keys.py::get_api_key_principal)
instead of the JWT `get_current_user` flow every other router uses — a key
is a machine credential scoped to one organization + one role
(owner/editor/viewer), so every endpoint here resolves org scope from the
key itself, never from a path/query parameter a caller could otherwise use
to reach into another org's data.

Deliberately a small, curated surface (proposals, pipeline, awards) rather
than exposing every internal endpoint — external partners get a
stable, intentional shape (see PublicProposalOut's docstring), and reuse
the same underlying engines the app's own routers already use rather than
duplicating business logic.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import Award, FOARecord, OrgProposal, Proposal, new_uuid
from models.schemas import AwardOut, FOARecordOut, PublicPipelineCreate, PublicProposalOut
from api_keys import ApiKeyPrincipal, get_api_key_principal, require_write
from engines.funding_intelligence_engine import FundingIntelligenceEngine

router = APIRouter()
funding = FundingIntelligenceEngine()


def _to_public_proposal(p: Proposal) -> PublicProposalOut:
    return PublicProposalOut(
        proposal_id=p.id, title=p.title, agency=p.agency, phase=p.phase,
        status=p.status, created_at=p.created_at, updated_at=p.updated_at,
    )


def _to_foa_out(record: FOARecord) -> FOARecordOut:
    return FOARecordOut(
        id=record.id, org_id=record.org_id, agency=record.agency, program_title=record.program_title,
        solicitation_number=record.solicitation_number, phase=record.phase, grant_type=record.grant_type,
        total_page_limit=record.total_page_limit, deadline=record.deadline, source=record.source,
        external_id=record.external_id, external_url=record.external_url,
        estimated_award_floor=record.estimated_award_floor, estimated_award_ceiling=record.estimated_award_ceiling,
        eligibility_summary=record.eligibility_summary, pipeline_stage=record.pipeline_stage,
        bid_no_go_decision=record.bid_no_go_decision, bid_no_go_rationale=record.bid_no_go_rationale,
        assigned_to=record.assigned_to, uploaded_by=record.uploaded_by, last_synced_at=record.last_synced_at,
        created_at=record.created_at, has_parsed_template=record.parsed_template is not None,
    )


def _to_award_out(award: Award) -> AwardOut:
    return AwardOut(
        id=award.id, proposal_id=award.proposal_id, foa_id=award.foa_id, org_id=award.org_id,
        budget_record_id=award.budget_record_id, award_number=award.award_number,
        funding_agency=award.funding_agency,
        period_of_performance_start=award.period_of_performance_start,
        period_of_performance_end=award.period_of_performance_end,
        total_award_value=award.total_award_value, terms=award.terms, status=award.status,
        created_by=award.created_by, created_at=award.created_at, updated_at=award.updated_at,
    )


# ── Proposals (read-only) ────────────────────────────────────────────────────

@router.get("/proposals", response_model=List[PublicProposalOut])
async def list_public_proposals(
    db: AsyncSession = Depends(get_db), principal: ApiKeyPrincipal = Depends(get_api_key_principal),
):
    result = await db.execute(
        select(Proposal).join(OrgProposal, OrgProposal.proposal_id == Proposal.id)
        .where(OrgProposal.org_id == principal.org_id).order_by(Proposal.created_at.desc())
    )
    return [_to_public_proposal(p) for p in result.scalars().all()]


@router.get("/proposals/{proposal_id}", response_model=PublicProposalOut)
async def get_public_proposal(
    proposal_id: str, db: AsyncSession = Depends(get_db), principal: ApiKeyPrincipal = Depends(get_api_key_principal),
):
    result = await db.execute(
        select(Proposal).join(OrgProposal, OrgProposal.proposal_id == Proposal.id)
        .where(OrgProposal.org_id == principal.org_id, Proposal.id == proposal_id)
    )
    proposal = result.scalar_one_or_none()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return _to_public_proposal(proposal)


# ── Pipeline (read + write) ──────────────────────────────────────────────────

@router.get("/pipeline", response_model=List[FOARecordOut])
async def list_public_pipeline(
    pipeline_stage: Optional[str] = None,
    db: AsyncSession = Depends(get_db), principal: ApiKeyPrincipal = Depends(get_api_key_principal),
):
    records = await funding.list_pipeline(db, org_id=principal.org_id, pipeline_stage=pipeline_stage)
    return [_to_foa_out(r) for r in records]


@router.post("/pipeline", response_model=FOARecordOut)
async def create_public_pipeline_entry(
    payload: PublicPipelineCreate, db: AsyncSession = Depends(get_db), principal: ApiKeyPrincipal = Depends(get_api_key_principal),
):
    require_write(principal)
    record = FOARecord(
        id=new_uuid(), agency=payload.agency, program_title=payload.program_title, phase=payload.phase,
        grant_type=payload.grant_type, deadline=payload.deadline, org_id=principal.org_id,
        pipeline_stage="identified", source="manual", uploaded_by=None,
    )
    db.add(record)
    await db.flush()
    await db.refresh(record)
    await db.commit()
    return _to_foa_out(record)


# ── Awards (read-only) ───────────────────────────────────────────────────────

@router.get("/awards", response_model=List[AwardOut])
async def list_public_awards(
    db: AsyncSession = Depends(get_db), principal: ApiKeyPrincipal = Depends(get_api_key_principal),
):
    result = await db.execute(select(Award).where(Award.org_id == principal.org_id).order_by(Award.created_at.desc()))
    return [_to_award_out(a) for a in result.scalars().all()]


@router.get("/awards/{award_id}", response_model=AwardOut)
async def get_public_award(
    award_id: str, db: AsyncSession = Depends(get_db), principal: ApiKeyPrincipal = Depends(get_api_key_principal),
):
    result = await db.execute(select(Award).where(Award.id == award_id, Award.org_id == principal.org_id))
    award = result.scalar_one_or_none()
    if not award:
        raise HTTPException(status_code=404, detail="Award not found")
    return _to_award_out(award)
