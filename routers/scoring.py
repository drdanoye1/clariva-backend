"""Scoring router — mathematical proposal scoring."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models.db_models import Proposal, ProposalSection, ScoringRecord, User
from models.schemas import ScoringResult
from routers.auth import get_current_user
from engines.scoring_engine import ScoringEngine

router = APIRouter()
scorer = ScoringEngine()


@router.post("/{proposal_id}", response_model=ScoringResult)
async def score_proposal(
    proposal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Run full mathematical scoring on a proposal."""
    result = await db.execute(
        select(Proposal).where(
            Proposal.id == proposal_id,
            Proposal.owner_id == current_user.id,
        )
    )
    proposal = result.scalar_one_or_none()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    sec_result = await db.execute(
        select(ProposalSection)
        .where(ProposalSection.proposal_id == proposal_id)
        .order_by(ProposalSection.order_index)
    )
    sections = sec_result.scalars().all()
    if not any(s.content for s in sections):
        raise HTTPException(status_code=400, detail="Generate proposal content before scoring")

    try:
        scoring = await scorer.score(proposal, sections, db=db, user_id=current_user.id)
    except Exception as exc:
        print(f"[scoring] Engine error: {exc}")
        raise HTTPException(status_code=500, detail=f"Scoring engine error: {str(exc)}")

    try:
        import uuid as _uuid
        record = ScoringRecord(
            id=str(_uuid.uuid4()),
            proposal_id=proposal_id,
            total_score=scoring.total_score,
            section_scores=[s.model_dump() for s in scoring.section_scores],
            risk_penalties=[r.model_dump() for r in scoring.risk_penalties],
            compliance_score=scoring.compliance_score,
            technical_merit=scoring.technical_merit,
            commercialization=scoring.commercialization,
            innovation=scoring.innovation,
            team_score=scoring.team_score,
        )
        db.add(record)
        await db.flush()
    except Exception as save_err:
        print(f"[scoring] Warning: failed to save record: {save_err}")

    return scoring


@router.get("/{proposal_id}/latest", response_model=ScoringResult)
async def get_latest_score(
    proposal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ScoringRecord)
        .where(ScoringRecord.proposal_id == proposal_id)
        .order_by(ScoringRecord.scored_at.desc())
        .limit(1)
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="No scoring record found")

    from models.schemas import SectionScore, RiskPenalty
    return ScoringResult(
        proposal_id=proposal_id,
        total_score=record.total_score,
        section_scores=[SectionScore(**s) for s in (record.section_scores or [])],
        risk_penalties=[RiskPenalty(**r) for r in (record.risk_penalties or [])],
        compliance_score=record.compliance_score,
        technical_merit=record.technical_merit,
        commercialization=record.commercialization,
        innovation=record.innovation,
        team_score=record.team_score,
        scored_at=record.scored_at,
    )
