"""Reviewer simulation router — with company profile gap awareness."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models.db_models import OrgContextDB, Proposal, ProposalSection, ReviewerRecord, User
from models.schemas import ReviewerSimulation, ReviewerType
from routers.auth import get_current_user
from engines.reviewer_simulator import ReviewerSimulatorEngine
from engines.company_profile import get_org_context

router    = APIRouter()
simulator = ReviewerSimulatorEngine()


async def _load_company_profile(user_id: str, db: AsyncSession) -> dict:
    # Funding Opportunity Intelligence, Phase 2 — see
    # engines/company_profile.py's module docstring: OrgContextDB.user_id
    # is no longer unique, so this can't query it directly anymore.
    ctx = await get_org_context(db, user_id=user_id)
    if not ctx:
        return {}
    return {
        "organization_name":    ctx.organization_name,
        "industry":             ctx.industry,
        "company_capabilities": ctx.company_capabilities,
        "pi_name":              ctx.pi_name,
        "pi_credentials":       ctx.pi_credentials,
        "pi_degree":            ctx.pi_degree,
        "pi_publications":      ctx.pi_publications,
        "pi_prior_sbir_awards": ctx.pi_prior_sbir_awards,
        "team_members":         ctx.team_members or [],
        "facilities":           ctx.facilities or [],
        "partners":             ctx.partners or [],
        "past_performance":     ctx.past_performance or [],
        "uei_number":           ctx.uei_number,
    }


@router.post("/{proposal_id}", response_model=ReviewerSimulation)
async def simulate_review(
    proposal_id: str,
    reviewer_type: ReviewerType = ReviewerType.GENERIC,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Simulate a funding agency reviewer — surfaces profile gaps as specific weaknesses."""
    try:
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

        company_profile = await _load_company_profile(current_user.id, db)

        simulation = await simulator.simulate(
            proposal, sections, reviewer_type, company_profile=company_profile,
            db=db, user_id=current_user.id,
        )

        try:
            import uuid as _uuid
            record = ReviewerRecord(
                id=str(_uuid.uuid4()),
                proposal_id=proposal_id,
                reviewer_type=reviewer_type.value,
                overall_impression=simulation.overall_impression,
                strengths=simulation.strengths,
                weaknesses=simulation.weaknesses,
                questions_for_applicant=simulation.questions_for_applicant,
                decision=simulation.decision,
                confidence=simulation.confidence,
            )
            db.add(record)
            await db.flush()
        except Exception as save_err:
            print(f"[reviewer] Warning: failed to save record: {save_err}")

        return simulation

    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        print(f"[reviewer] Unhandled error: {exc}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Reviewer simulation error: {str(exc)}")


@router.get("/{proposal_id}/latest", response_model=ReviewerSimulation)
async def get_latest_review(
    proposal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ReviewerRecord)
        .where(ReviewerRecord.proposal_id == proposal_id)
        .order_by(ReviewerRecord.simulated_at.desc())
        .limit(1)
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="No reviewer simulation found")

    return ReviewerSimulation(
        proposal_id=proposal_id,
        reviewer_type=ReviewerType(record.reviewer_type),
        overall_impression=record.overall_impression or "",
        strengths=record.strengths or [],
        weaknesses=record.weaknesses or [],
        questions_for_applicant=record.questions_for_applicant or [],
        decision=record.decision or "Pending",
        confidence=record.confidence,
        simulated_at=record.simulated_at,
    )
