"""
Scope of Work Engine router — Project Knowledge Base authoring (PRD §9), the
Scope of Work hierarchy of work packages/tasks/milestones/deliverables (PRD
§10), AI-assisted drafting, staleness-flag review, and the Budget Builder
sync. Mounted at the same /api/v1/proposals prefix as proposals.py, the same
way credits.py shares organizations.py's prefix — see docs/ARCHITECTURE.md
§1.1.

Ownership: every endpoint reuses proposals.py's `_get_proposal_or_404`
(owner-only), the same access rule proposal editing already uses. Org-shared
proposals are not yet scope-of-work-editable by non-owner org members —
that's a natural RBAC extension for a later pass, not a Phase 2 regression
(the proposal itself has the same limitation today).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import User
from models.schemas import (
    BudgetSyncOut, DeliverableCreate, DeliverableOut, DeliverableUpdate,
    DeriveFromProposalOut, EvaluationPlanGenerateOut, FieldSuggestOut,
    FieldSuggestRequest, MethodologyGenerateOut, MethodologyGenerateRequest,
    MilestoneCreate, MilestoneOut, MilestoneUpdate, ProjectKnowledgeOut,
    ProjectKnowledgeUpdate, ScopeOfWorkFull, ScopeOfWorkOut, ScopeOfWorkUpdate,
    TaskCreate, TaskOut, TaskUpdate, WorkBreakdownGenerateOut,
    WorkBreakdownGenerateRequest, WorkPackageCreate, WorkPackageOut, WorkPackageUpdate,
)
from config import settings
from routers.auth import get_current_user
from routers.proposals import _get_proposal_or_404, _load_company_profile
from routers.organizations import _assert_member
from engines.scope_of_work_engine import ScopeOfWorkEngine
from engines.credit_engine import CreditEngine, GENERATION_COST, debit_or_402
from engines import usage_tracking

router = APIRouter()
engine = ScopeOfWorkEngine()
credit_engine = CreditEngine()


async def _meter(org_id: Optional[str], user_id: str, db: AsyncSession, reason: str) -> None:
    """See proposals.py::generate_section for the org_id / credit-metering
    contract: optional and additive, omit it for unmetered personal use."""
    if org_id:
        await _assert_member(org_id, user_id, db)
        await debit_or_402(credit_engine, db, org_id, user_id, GENERATION_COST, reason=reason)


# ── Project Knowledge ────────────────────────────────────────────────────────

@router.get("/{proposal_id}/knowledge", response_model=ProjectKnowledgeOut)
async def get_project_knowledge(
    proposal_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    return await engine.get_or_create_project_knowledge(db, proposal_id)


@router.patch("/{proposal_id}/knowledge", response_model=ProjectKnowledgeOut)
async def update_project_knowledge(
    proposal_id: str, body: ProjectKnowledgeUpdate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    return await engine.update_project_knowledge(db, proposal_id, body.model_dump(exclude_unset=True))


@router.delete("/{proposal_id}/knowledge/stale-flags/{flag}", response_model=ProjectKnowledgeOut)
async def clear_stale_flag(
    proposal_id: str, flag: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Dismiss a staleness flag (e.g. "sections") without necessarily acting
    on it — an explicit "I've reviewed this" acknowledgement. Budget staleness
    is instead cleared automatically by POST .../sync-budget."""
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    return await engine.clear_stale_flag(db, proposal_id, flag)


# ── Scope of Work (+ full aggregate view for the frontend tab) ──────────────

@router.get("/{proposal_id}/scope-of-work", response_model=ScopeOfWorkFull)
async def get_scope_of_work(
    proposal_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    pk = await engine.get_or_create_project_knowledge(db, proposal_id)
    sow = await engine.get_or_create_scope_of_work(db, proposal_id)
    work_packages = await engine.list_work_packages(db, sow.id)
    tasks = await engine.list_tasks(db, [wp.id for wp in work_packages])
    milestones = await engine.list_milestones(db, sow.id)
    deliverables = await engine.list_deliverables(db, sow.id)
    return ScopeOfWorkFull(
        project_knowledge=ProjectKnowledgeOut.model_validate(pk),
        scope_of_work=ScopeOfWorkOut.model_validate(sow),
        work_packages=[WorkPackageOut.model_validate(wp) for wp in work_packages],
        tasks=[TaskOut.model_validate(t) for t in tasks],
        milestones=[MilestoneOut.model_validate(m) for m in milestones],
        deliverables=[DeliverableOut.model_validate(d) for d in deliverables],
    )


@router.patch("/{proposal_id}/scope-of-work", response_model=ScopeOfWorkOut)
async def update_scope_of_work(
    proposal_id: str, body: ScopeOfWorkUpdate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    return await engine.update_scope_of_work(db, proposal_id, body.model_dump(exclude_unset=True))


# ── Work Packages ────────────────────────────────────────────────────────────

@router.post("/{proposal_id}/scope-of-work/work-packages", response_model=WorkPackageOut, status_code=201)
async def create_work_package(
    proposal_id: str, body: WorkPackageCreate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    return await engine.create_work_package(db, proposal_id, body.model_dump())


@router.patch("/{proposal_id}/scope-of-work/work-packages/{work_package_id}", response_model=WorkPackageOut)
async def update_work_package(
    proposal_id: str, work_package_id: str, body: WorkPackageUpdate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    return await engine.update_work_package(db, proposal_id, work_package_id, body.model_dump(exclude_unset=True))


@router.delete("/{proposal_id}/scope-of-work/work-packages/{work_package_id}", status_code=204)
async def delete_work_package(
    proposal_id: str, work_package_id: str,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    await engine.delete_work_package(db, proposal_id, work_package_id)


# ── Tasks ────────────────────────────────────────────────────────────────────

@router.post("/{proposal_id}/scope-of-work/work-packages/{work_package_id}/tasks", response_model=TaskOut, status_code=201)
async def create_task(
    proposal_id: str, work_package_id: str, body: TaskCreate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    return await engine.create_task(db, proposal_id, work_package_id, body.model_dump())


@router.patch("/{proposal_id}/scope-of-work/tasks/{task_id}", response_model=TaskOut)
async def update_task(
    proposal_id: str, task_id: str, body: TaskUpdate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    return await engine.update_task(db, proposal_id, task_id, body.model_dump(exclude_unset=True))


@router.delete("/{proposal_id}/scope-of-work/tasks/{task_id}", status_code=204)
async def delete_task(
    proposal_id: str, task_id: str,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    await engine.delete_task(db, proposal_id, task_id)


# ── Milestones ───────────────────────────────────────────────────────────────

@router.post("/{proposal_id}/scope-of-work/milestones", response_model=MilestoneOut, status_code=201)
async def create_milestone(
    proposal_id: str, body: MilestoneCreate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    return await engine.create_milestone(db, proposal_id, body.model_dump())


@router.patch("/{proposal_id}/scope-of-work/milestones/{milestone_id}", response_model=MilestoneOut)
async def update_milestone(
    proposal_id: str, milestone_id: str, body: MilestoneUpdate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    return await engine.update_milestone(db, proposal_id, milestone_id, body.model_dump(exclude_unset=True))


@router.delete("/{proposal_id}/scope-of-work/milestones/{milestone_id}", status_code=204)
async def delete_milestone(
    proposal_id: str, milestone_id: str,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    await engine.delete_milestone(db, proposal_id, milestone_id)


# ── Deliverables ─────────────────────────────────────────────────────────────

@router.post("/{proposal_id}/scope-of-work/deliverables", response_model=DeliverableOut, status_code=201)
async def create_deliverable(
    proposal_id: str, body: DeliverableCreate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    return await engine.create_deliverable(db, proposal_id, body.model_dump())


@router.patch("/{proposal_id}/scope-of-work/deliverables/{deliverable_id}", response_model=DeliverableOut)
async def update_deliverable(
    proposal_id: str, deliverable_id: str, body: DeliverableUpdate,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    return await engine.update_deliverable(db, proposal_id, deliverable_id, body.model_dump(exclude_unset=True))


@router.delete("/{proposal_id}/scope-of-work/deliverables/{deliverable_id}", status_code=204)
async def delete_deliverable(
    proposal_id: str, deliverable_id: str,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    await engine.delete_deliverable(db, proposal_id, deliverable_id)


# ── AI Generation ────────────────────────────────────────────────────────────
# All three endpoints accept the same optional org_id credit-metering query
# param as proposals.py's generate-section/generate-all — see _meter() above.

@router.post("/{proposal_id}/scope-of-work/generate-methodology", response_model=MethodologyGenerateOut)
async def generate_methodology(
    proposal_id: str, body: MethodologyGenerateRequest, org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    await _meter(org_id, current_user.id, db, reason=f"scope_of_work:methodology:{proposal_id}")
    pk = await engine.get_or_create_project_knowledge(db, proposal_id)
    company_profile = await _load_company_profile(current_user.id, db)
    narrative = await engine.generate_methodology_narrative(
        proposal, pk, company_profile, additional_context=body.additional_context,
        db=db, org_id=org_id, user_id=current_user.id,
        price_cents_charged=int(GENERATION_COST * 100) if org_id else 0,
    )
    sow = await engine.update_scope_of_work(db, proposal_id, {"methodology_narrative": narrative})
    return MethodologyGenerateOut(methodology_narrative=sow.methodology_narrative)


@router.post("/{proposal_id}/scope-of-work/generate-evaluation-plan", response_model=EvaluationPlanGenerateOut)
async def generate_evaluation_plan(
    proposal_id: str, body: MethodologyGenerateRequest, org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    await _meter(org_id, current_user.id, db, reason=f"scope_of_work:evaluation_plan:{proposal_id}")
    pk = await engine.get_or_create_project_knowledge(db, proposal_id)
    company_profile = await _load_company_profile(current_user.id, db)
    plan = await engine.generate_evaluation_plan(
        proposal, pk, company_profile, additional_context=body.additional_context,
        db=db, org_id=org_id, user_id=current_user.id,
        price_cents_charged=int(GENERATION_COST * 100) if org_id else 0,
    )
    pk = await engine.update_project_knowledge(db, proposal_id, {"evaluation_plan": plan})
    return EvaluationPlanGenerateOut(evaluation_plan=pk.evaluation_plan)


@router.post("/{proposal_id}/scope-of-work/generate-work-breakdown", response_model=WorkBreakdownGenerateOut)
async def generate_work_breakdown(
    proposal_id: str, body: WorkBreakdownGenerateRequest, org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Generates AND persists a suggested work breakdown (new work packages,
    tasks, milestones, deliverables) in one call — existing rows are left
    alone; re-running this adds another proposed breakdown alongside them
    rather than replacing anything, since deleting user edits automatically
    would be a nasty surprise."""
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    await _meter(org_id, current_user.id, db, reason=f"scope_of_work:work_breakdown:{proposal_id}")
    pk = await engine.get_or_create_project_knowledge(db, proposal_id)
    sow = await engine.get_or_create_scope_of_work(db, proposal_id)
    company_profile = await _load_company_profile(current_user.id, db)
    breakdown = await engine.generate_work_breakdown(
        proposal, pk, sow, company_profile, additional_context=body.additional_context,
    )
    # Phase 3 §4.7 — Administrator-Only Engineering Economics.
    usage = breakdown.pop("_usage", None) or {}
    await usage_tracking.record_usage(
        db, operation="scope_of_work:work_breakdown", model=usage.get("model", settings.OPENAI_MODEL),
        prompt_tokens=usage.get("prompt_tokens", 0), completion_tokens=usage.get("completion_tokens", 0),
        org_id=org_id, user_id=current_user.id,
        price_cents_charged=int(GENERATION_COST * 100) if org_id else 0,
        reference={"proposal_id": proposal_id},
    )
    created = await engine.apply_generated_work_breakdown(db, proposal_id, breakdown)
    return WorkBreakdownGenerateOut(
        work_packages=[WorkPackageOut.model_validate(wp) for wp in created["work_packages"]],
        tasks=[TaskOut.model_validate(t) for t in created["tasks"]],
        milestones=[MilestoneOut.model_validate(m) for m in created["milestones"]],
        deliverables=[DeliverableOut.model_validate(d) for d in created["deliverables"]],
    )


@router.post("/{proposal_id}/scope-of-work/derive-from-proposal", response_model=DeriveFromProposalOut)
async def derive_from_proposal(
    proposal_id: str, org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """One-click alternative to typing Project Knowledge/Scope of Work by
    hand, or to drafting from proposal metadata alone (see
    generate_methodology/generate_evaluation_plan above): reads the
    proposal's already-generated section content and extracts a draft
    summary from it. Returned for review — nothing is persisted here; the
    user saves each field via the existing PATCH endpoints, same as every
    other AI-draft action in this router. 400s if the proposal has no
    generated section content yet."""
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    await _meter(org_id, current_user.id, db, reason=f"scope_of_work:derive_from_proposal:{proposal_id}")
    pk = await engine.get_or_create_project_knowledge(db, proposal_id)
    draft = await engine.derive_project_knowledge_from_proposal(
        db, proposal, pk, org_id=org_id, user_id=current_user.id,
        price_cents_charged=int(GENERATION_COST * 100) if org_id else 0,
    )
    return DeriveFromProposalOut(**draft)


@router.post("/{proposal_id}/scope-of-work/suggest-field", response_model=FieldSuggestOut)
async def suggest_field(
    proposal_id: str, body: FieldSuggestRequest, org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Powers the inline "Suggest" button next to a manually-edited Project
    Knowledge field (objectives/need_statement/outputs/outcomes) — factors in
    whatever the user has already typed rather than drafting from scratch."""
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    await _meter(org_id, current_user.id, db, reason=f"scope_of_work:suggest_field:{proposal_id}:{body.field}")
    pk = await engine.get_or_create_project_knowledge(db, proposal_id)
    company_profile = await _load_company_profile(current_user.id, db)
    suggestion = await engine.suggest_project_knowledge_field(
        proposal, pk, company_profile, body.field,
        current_value=body.current_value, additional_context=body.additional_context,
        db=db, org_id=org_id, user_id=current_user.id,
        price_cents_charged=int(GENERATION_COST * 100) if org_id else 0,
    )
    return FieldSuggestOut(field=body.field, suggestion=suggestion)


# ── Budget sync ──────────────────────────────────────────────────────────────

@router.post("/{proposal_id}/scope-of-work/sync-budget", response_model=BudgetSyncOut)
async def sync_budget(
    proposal_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Merges each work package's estimated_cost into the Budget Builder
    (see engines/scope_of_work_engine.py::sync_budget_from_scope_of_work for
    the non-destructive merge rule) and clears the "budget" stale flag."""
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    budget, synced = await engine.sync_budget_from_scope_of_work(db, proposal_id)
    return BudgetSyncOut(
        proposal_id=proposal_id,
        total_direct=budget.total_direct,
        total_indirect=budget.total_indirect,
        total_cost=budget.total_cost,
        synced_work_packages=synced,
    )
