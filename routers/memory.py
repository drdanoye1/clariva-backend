"""Organizational memory & KPI router."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models.db_models import MemoryRecord, Proposal, User
from models.schemas import KPIDashboard, MemoryEntry
from routers.auth import get_current_user
from engines.memory_system import MemoryEngine

router = APIRouter()
memory_engine = MemoryEngine()


@router.get("/kpi", response_model=KPIDashboard)
async def get_kpi_dashboard(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return KPI metrics for the current user's organization."""
    result = await db.execute(
        select(MemoryRecord).where(MemoryRecord.org_id == current_user.id)
    )
    records = result.scalars().all()

    return memory_engine.build_kpi(current_user.id, records)


@router.post("/{proposal_id}/record-outcome")
async def record_outcome(
    proposal_id: str,
    outcome: str,  # "funded" | "not_funded"
    lessons: list[str] = [],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Record the final outcome of a submitted proposal."""
    result = await db.execute(
        select(Proposal).where(
            Proposal.id == proposal_id,
            Proposal.owner_id == current_user.id,
        )
    )
    proposal = result.scalar_one_or_none()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    record = MemoryRecord(
        org_id=current_user.id,
        proposal_id=proposal_id,
        agency=proposal.agency,
        outcome=outcome,
        lessons_learned=lessons,
    )
    db.add(record)
    await db.flush()
    return {"message": "Outcome recorded", "record_id": record.id}


@router.get("/similar-proposals")
async def find_similar_proposals(
    query: str,
    limit: int = 5,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Semantic search over past proposals (requires pgvector)."""
    results = await memory_engine.semantic_search(query, current_user.id, db, limit)
    return results
