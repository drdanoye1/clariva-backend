"""
Engine 12 — Scope of Work Engine
Turns the Project Knowledge Base (Clariva Enterprise™ PRD §9) into an active
project model — a "digital twin" of the funded project (PRD §10) — built
from a hierarchy of work packages, tasks, milestones, and deliverables.

Smart Dependency Engine (PRD §10.1): rather than a full graph database, this
is implemented as a lightweight change-event system — every write that
changes the Scope of Work sets flags on `ProjectKnowledge.stale_flags`
marking which downstream artifacts (the budget, proposal sections) may need
review. Nothing is auto-regenerated; a human (or a future, explicitly opted
-in pass) decides when to act on a stale flag. See docs/ARCHITECTURE.md §7.

Design notes (mirrors engines/credit_engine.py's style):
- Every method takes an already-open AsyncSession and only flushes, never
  commits — the caller's request-scoped session (see database.py::get_db)
  commits once, after the router handler returns.
- ProjectKnowledge / ScopeOfWork rows are created lazily on first access
  (get_or_create_*), so existing proposals created before this feature
  shipped work identically to new ones.
- All list-fetching happens via explicit `select()` queries, never via
  SQLAlchemy relationship traversal, to avoid re-triggering the async
  lazy-load pitfall fixed in credit_engine.py (accessing an unloaded/expired
  relationship attribute on an AsyncSession object raises MissingGreenlet).
"""
from __future__ import annotations

import json
import logging
import re
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import openai
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from engines import usage_tracking
from engines.usage_tracking import usage_from_response
from models.db_models import (
    BudgetRecord, Deliverable, Milestone, ProjectKnowledge, ProposalSection,
    ScopeOfWork, Task, WorkPackage, new_uuid,
)

_log = logging.getLogger(__name__)


def _ai_error(exc: Exception) -> HTTPException:
    """Convert an OpenAI exception into a generic HTTPException (no provider
    names exposed). This codebase's convention is a local copy per module
    rather than a shared import — see the near-identical helpers in
    engines/proposal_generator.py and routers/budget.py."""
    if isinstance(exc, openai.RateLimitError):
        _log.error("AI service rate limit exceeded: %s", exc)
        return HTTPException(status_code=503, detail="The AI writing service is temporarily unavailable due to high demand. Please try again in a few minutes.")
    if isinstance(exc, openai.AuthenticationError):
        _log.critical("AI service authentication failure: %s", exc)
        return HTTPException(status_code=503, detail="The AI writing service is not configured correctly. Please contact support.")
    if isinstance(exc, openai.APIConnectionError):
        _log.error("AI service connection error: %s", exc)
        return HTTPException(status_code=503, detail="The AI writing service is unreachable. Please try again in a moment.")
    if isinstance(exc, openai.APIStatusError):
        _log.error("AI service API error %s: %s", getattr(exc, "status_code", "?"), getattr(exc, "message", str(exc)))
        return HTTPException(status_code=503, detail="The AI writing service returned an unexpected error. Please try again.")
    _log.error("Unexpected scope-of-work generation error:\n%s", traceback.format_exc())
    return HTTPException(status_code=500, detail="Content generation failed. Please try again or contact support.")


def _parse_json_response(raw: str) -> Dict[str, Any]:
    """Strip markdown fences and parse JSON, with a brace-scan fallback —
    mirrors routers/budget.py::extract_budget_from_file's parsing exactly."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}") + 1
        try:
            return json.loads(cleaned[start:end])
        except Exception:
            raise HTTPException(status_code=500, detail="Could not parse the AI-generated work breakdown.")


def _recalc_budget_totals(budget: BudgetRecord) -> None:
    """Recompute BudgetRecord.total_direct/total_indirect/total_cost in
    place. Intentionally mirrors routers/budget.py::_calc_totals()'s formula
    rather than importing it — engines stay free of router-module imports
    per docs/ARCHITECTURE.md §1.2. If that formula changes, update both."""
    months = float(budget.budget_months or 12)
    personnel_salary = personnel_fringe = 0.0
    for p in budget.personnel or []:
        sal = float(p.get("annual_salary") or 0)
        fringe = float(p.get("fringe_rate") or 0) / 100.0
        effort = float(p.get("effort_pct") or 0) / 100.0
        cost = sal * effort * (months / 12.0)
        personnel_salary += cost
        personnel_fringe += cost * fringe

    consultant_total  = sum(float(c.get("rate_per_day") or 0) * float(c.get("days") or 0) for c in (budget.consultants or []))
    equipment_total   = sum(float(e.get("cost") or 0) for e in (budget.equipment or []))
    travel_total      = sum(float(t.get("trips") or 0) * float(t.get("people") or 1) * float(t.get("cost_per_trip") or 0) for t in (budget.travel or []))
    other_total       = sum(float(o.get("cost") or 0) for o in (budget.other_direct or []))
    subcontract_total = sum(float(s.get("total_cost") or 0) for s in (budget.subcontracts or []))

    total_direct = personnel_salary + personnel_fringe + consultant_total + equipment_total + travel_total + other_total + subcontract_total

    if (budget.indirect_base or "mtdc").lower() == "mtdc":
        sub_excess = sum(max(0.0, float(s.get("total_cost") or 0) - 25_000.0) for s in (budget.subcontracts or []))
        base_amount = max(0.0, total_direct - equipment_total - sub_excess)
    else:
        base_amount = total_direct

    indirect_rate = float(budget.indirect_rate or 0) / 100.0
    total_indirect = base_amount * indirect_rate

    budget.total_direct = round(total_direct, 2)
    budget.total_indirect = round(total_indirect, 2)
    budget.total_cost = round(total_direct + total_indirect, 2)


class ScopeOfWorkEngine:
    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    # ── Project Knowledge ────────────────────────────────────────────────────

    async def get_or_create_project_knowledge(self, db: AsyncSession, proposal_id: str) -> ProjectKnowledge:
        result = await db.execute(select(ProjectKnowledge).where(ProjectKnowledge.proposal_id == proposal_id))
        pk = result.scalar_one_or_none()
        if pk:
            return pk
        pk = ProjectKnowledge(id=new_uuid(), proposal_id=proposal_id)
        db.add(pk)
        await db.flush()
        # `created_at` is server-generated (server_default=func.now()); refresh
        # now so a caller can safely read it synchronously afterward instead of
        # hitting AsyncSession's MissingGreenlet on an implicit lazy-load —
        # see the identical fix and rationale in engines/credit_engine.py.
        await db.refresh(pk)
        return pk

    async def update_project_knowledge(self, db: AsyncSession, proposal_id: str, updates: Dict[str, Any]) -> ProjectKnowledge:
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        for field in ("objectives", "need_statement", "evaluation_plan", "outputs", "outcomes"):
            if updates.get(field) is not None:
                setattr(pk, field, updates[field])
        if updates.get("risks") is not None:
            pk.risks = updates["risks"]
        if updates.get("kpis") is not None:
            pk.kpis = updates["kpis"]
        self.mark_stale(pk, "sections")
        await db.flush()
        await db.refresh(pk)
        return pk

    # ── Staleness / Smart Dependency Engine (PRD §10.1) ─────────────────────

    def mark_stale(self, pk: ProjectKnowledge, *flags: str) -> None:
        current = dict(pk.stale_flags or {})
        for flag in flags:
            current[flag] = True
        current["last_changed_at"] = datetime.utcnow().isoformat()
        pk.stale_flags = current

    def clear_stale(self, pk: ProjectKnowledge, *flags: str) -> None:
        current = dict(pk.stale_flags or {})
        for flag in flags:
            current.pop(flag, None)
        pk.stale_flags = current

    async def clear_stale_flag(self, db: AsyncSession, proposal_id: str, flag: str) -> ProjectKnowledge:
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        self.clear_stale(pk, flag)
        await db.flush()
        await db.refresh(pk)
        return pk

    # ── Scope of Work ────────────────────────────────────────────────────────

    async def get_or_create_scope_of_work(self, db: AsyncSession, proposal_id: str) -> ScopeOfWork:
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        result = await db.execute(select(ScopeOfWork).where(ScopeOfWork.project_knowledge_id == pk.id))
        sow = result.scalar_one_or_none()
        if sow:
            return sow
        sow = ScopeOfWork(id=new_uuid(), project_knowledge_id=pk.id)
        db.add(sow)
        await db.flush()
        await db.refresh(sow)  # see the matching comment in get_or_create_project_knowledge
        return sow

    async def update_scope_of_work(self, db: AsyncSession, proposal_id: str, updates: Dict[str, Any]) -> ScopeOfWork:
        sow = await self.get_or_create_scope_of_work(db, proposal_id)
        for field in ("period_of_performance_months", "methodology_narrative"):
            if updates.get(field) is not None:
                setattr(sow, field, updates[field])
        if updates.get("logic_model") is not None:
            sow.logic_model = updates["logic_model"]
        if updates.get("reporting_schedule") is not None:
            sow.reporting_schedule = updates["reporting_schedule"]
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        self.mark_stale(pk, "sections")
        await db.flush()
        await db.refresh(sow)
        return sow

    # ── Work Packages ────────────────────────────────────────────────────────

    async def list_work_packages(self, db: AsyncSession, scope_of_work_id: str) -> List[WorkPackage]:
        result = await db.execute(
            select(WorkPackage).where(WorkPackage.scope_of_work_id == scope_of_work_id).order_by(WorkPackage.order_index)
        )
        return list(result.scalars().all())

    async def get_work_package_or_404(self, db: AsyncSession, work_package_id: str) -> WorkPackage:
        result = await db.execute(select(WorkPackage).where(WorkPackage.id == work_package_id))
        wp = result.scalar_one_or_none()
        if not wp:
            raise HTTPException(status_code=404, detail="Work package not found")
        return wp

    async def create_work_package(self, db: AsyncSession, proposal_id: str, data: Dict[str, Any]) -> WorkPackage:
        sow = await self.get_or_create_scope_of_work(db, proposal_id)
        existing = await self.list_work_packages(db, sow.id)
        wp = WorkPackage(
            id=new_uuid(), scope_of_work_id=sow.id, order_index=len(existing),
            name=data["name"], description=data.get("description"), lead=data.get("lead"),
            start_month=data.get("start_month"), end_month=data.get("end_month"),
            estimated_cost=data.get("estimated_cost"),
        )
        db.add(wp)
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        flags = ["sections"] + (["budget"] if data.get("estimated_cost") else [])
        self.mark_stale(pk, *flags)
        await db.flush()
        await db.refresh(wp)
        return wp

    async def update_work_package(self, db: AsyncSession, proposal_id: str, work_package_id: str, data: Dict[str, Any]) -> WorkPackage:
        wp = await self.get_work_package_or_404(db, work_package_id)
        cost_changed = "estimated_cost" in data and data["estimated_cost"] != wp.estimated_cost
        for field in ("name", "description", "lead", "start_month", "end_month", "estimated_cost", "order_index"):
            if field in data and data[field] is not None:
                setattr(wp, field, data[field])
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        flags = ["sections"] + (["budget"] if cost_changed else [])
        self.mark_stale(pk, *flags)
        await db.flush()
        await db.refresh(wp)
        return wp

    async def delete_work_package(self, db: AsyncSession, proposal_id: str, work_package_id: str) -> None:
        wp = await self.get_work_package_or_404(db, work_package_id)
        had_cost = bool(wp.estimated_cost)
        await db.delete(wp)
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        flags = ["sections"] + (["budget"] if had_cost else [])
        self.mark_stale(pk, *flags)
        await db.flush()

    # ── Tasks ────────────────────────────────────────────────────────────────

    async def list_tasks(self, db: AsyncSession, work_package_ids: List[str]) -> List[Task]:
        if not work_package_ids:
            return []
        result = await db.execute(
            select(Task).where(Task.work_package_id.in_(work_package_ids)).order_by(Task.order_index)
        )
        return list(result.scalars().all())

    async def get_task_or_404(self, db: AsyncSession, task_id: str) -> Task:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return task

    async def create_task(self, db: AsyncSession, proposal_id: str, work_package_id: str, data: Dict[str, Any]) -> Task:
        await self.get_work_package_or_404(db, work_package_id)
        existing = await self.list_tasks(db, [work_package_id])
        task = Task(
            id=new_uuid(), work_package_id=work_package_id, order_index=len(existing),
            name=data["name"], description=data.get("description"), owner=data.get("owner"),
            start_month=data.get("start_month"), end_month=data.get("end_month"),
            status=data.get("status") or "not_started",
        )
        db.add(task)
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        self.mark_stale(pk, "sections")
        await db.flush()
        await db.refresh(task)
        return task

    async def update_task(self, db: AsyncSession, proposal_id: str, task_id: str, data: Dict[str, Any]) -> Task:
        task = await self.get_task_or_404(db, task_id)
        for field in ("name", "description", "owner", "start_month", "end_month", "status", "order_index"):
            if field in data and data[field] is not None:
                setattr(task, field, data[field])
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        self.mark_stale(pk, "sections")
        await db.flush()
        await db.refresh(task)
        return task

    async def delete_task(self, db: AsyncSession, proposal_id: str, task_id: str) -> None:
        task = await self.get_task_or_404(db, task_id)
        await db.delete(task)
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        self.mark_stale(pk, "sections")
        await db.flush()

    # ── Milestones ───────────────────────────────────────────────────────────

    async def list_milestones(self, db: AsyncSession, scope_of_work_id: str) -> List[Milestone]:
        result = await db.execute(
            select(Milestone).where(Milestone.scope_of_work_id == scope_of_work_id).order_by(Milestone.due_month)
        )
        return list(result.scalars().all())

    async def get_milestone_or_404(self, db: AsyncSession, milestone_id: str) -> Milestone:
        result = await db.execute(select(Milestone).where(Milestone.id == milestone_id))
        m = result.scalar_one_or_none()
        if not m:
            raise HTTPException(status_code=404, detail="Milestone not found")
        return m

    async def create_milestone(self, db: AsyncSession, proposal_id: str, data: Dict[str, Any]) -> Milestone:
        sow = await self.get_or_create_scope_of_work(db, proposal_id)
        if data.get("work_package_id"):
            await self.get_work_package_or_404(db, data["work_package_id"])
        m = Milestone(
            id=new_uuid(), scope_of_work_id=sow.id, work_package_id=data.get("work_package_id"),
            name=data["name"], description=data.get("description"), due_month=data.get("due_month"),
            status=data.get("status") or "pending",
        )
        db.add(m)
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        self.mark_stale(pk, "sections")
        await db.flush()
        await db.refresh(m)
        return m

    async def update_milestone(self, db: AsyncSession, proposal_id: str, milestone_id: str, data: Dict[str, Any]) -> Milestone:
        m = await self.get_milestone_or_404(db, milestone_id)
        for field in ("name", "description", "due_month", "status"):
            if field in data and data[field] is not None:
                setattr(m, field, data[field])
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        self.mark_stale(pk, "sections")
        await db.flush()
        await db.refresh(m)
        return m

    async def delete_milestone(self, db: AsyncSession, proposal_id: str, milestone_id: str) -> None:
        m = await self.get_milestone_or_404(db, milestone_id)
        await db.delete(m)
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        self.mark_stale(pk, "sections")
        await db.flush()

    # ── Deliverables ─────────────────────────────────────────────────────────

    async def list_deliverables(self, db: AsyncSession, scope_of_work_id: str) -> List[Deliverable]:
        result = await db.execute(
            select(Deliverable).where(Deliverable.scope_of_work_id == scope_of_work_id).order_by(Deliverable.due_month)
        )
        return list(result.scalars().all())

    async def get_deliverable_or_404(self, db: AsyncSession, deliverable_id: str) -> Deliverable:
        result = await db.execute(select(Deliverable).where(Deliverable.id == deliverable_id))
        d = result.scalar_one_or_none()
        if not d:
            raise HTTPException(status_code=404, detail="Deliverable not found")
        return d

    async def create_deliverable(self, db: AsyncSession, proposal_id: str, data: Dict[str, Any]) -> Deliverable:
        sow = await self.get_or_create_scope_of_work(db, proposal_id)
        if data.get("work_package_id"):
            await self.get_work_package_or_404(db, data["work_package_id"])
        d = Deliverable(
            id=new_uuid(), scope_of_work_id=sow.id, work_package_id=data.get("work_package_id"),
            name=data["name"], description=data.get("description"), due_month=data.get("due_month"),
            deliverable_type=data.get("deliverable_type"), status=data.get("status") or "pending",
        )
        db.add(d)
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        self.mark_stale(pk, "sections")
        await db.flush()
        await db.refresh(d)
        return d

    async def update_deliverable(self, db: AsyncSession, proposal_id: str, deliverable_id: str, data: Dict[str, Any]) -> Deliverable:
        d = await self.get_deliverable_or_404(db, deliverable_id)
        for field in ("name", "description", "due_month", "deliverable_type", "status"):
            if field in data and data[field] is not None:
                setattr(d, field, data[field])
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        self.mark_stale(pk, "sections")
        await db.flush()
        await db.refresh(d)
        return d

    async def delete_deliverable(self, db: AsyncSession, proposal_id: str, deliverable_id: str) -> None:
        d = await self.get_deliverable_or_404(db, deliverable_id)
        await db.delete(d)
        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        self.mark_stale(pk, "sections")
        await db.flush()

    # ── AI Generation ────────────────────────────────────────────────────────

    def _profile_block(self, company_profile: Dict[str, Any]) -> str:
        lines = []
        if company_profile.get("organization_name"):
            lines.append(f"Organization: {company_profile['organization_name']}")
        if company_profile.get("industry"):
            lines.append(f"Industry: {company_profile['industry']}")
        if company_profile.get("company_capabilities"):
            lines.append(f"Capabilities: {company_profile['company_capabilities']}")
        return "\n".join(lines)

    async def generate_methodology_narrative(
        self, proposal: Any, project_knowledge: ProjectKnowledge,
        company_profile: Dict[str, Any], additional_context: Optional[str] = None,
        db: Optional[AsyncSession] = None, org_id: Optional[str] = None, user_id: Optional[str] = None,
        price_cents_charged: int = 0,
    ) -> str:
        prompt = f"""
Write a methodology narrative (3-5 paragraphs, plain prose, no markdown or headers)
for the following funded/proposed project, suitable for use as the technical
approach in a grant proposal and as the project's working scope-of-work document.

Project title: {getattr(proposal, "title", "")}
Agency / grant type: {getattr(proposal, "agency", "")} / {getattr(proposal, "grant_type", "")}
Research focus: {getattr(proposal, "research_focus", "") or "Not yet specified"}
Innovation description: {getattr(proposal, "innovation_description", "") or "Not yet specified"}
Project objectives: {project_knowledge.objectives or "Not yet specified"}
Need statement: {project_knowledge.need_statement or "Not yet specified"}
{self._profile_block(company_profile)}
{f"Additional context: {additional_context}" if additional_context else ""}

If information needed to write a specific, credible narrative is missing, write
around it generally rather than inventing specifics — do not fabricate data,
partners, or results.
""".strip()
        try:
            response = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are an expert grant writer and project planner producing a methodology narrative. Output plain prose only — no markdown, no headers, no bullet characters."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.6,
                max_tokens=900,
            )
        except Exception as exc:
            raise _ai_error(exc)
        narrative = response.choices[0].message.content.strip()
        if db is not None:
            prompt_tokens, completion_tokens = usage_from_response(response)
            await usage_tracking.record_usage(
                db, org_id=org_id, user_id=user_id, operation="scope_of_work:methodology",
                model=settings.OPENAI_MODEL, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                price_cents_charged=price_cents_charged, reference={"proposal_id": getattr(proposal, "id", None)},
            )
        return narrative

    async def generate_evaluation_plan(
        self, proposal: Any, project_knowledge: ProjectKnowledge,
        company_profile: Dict[str, Any], additional_context: Optional[str] = None,
        db: Optional[AsyncSession] = None, org_id: Optional[str] = None, user_id: Optional[str] = None,
        price_cents_charged: int = 0,
    ) -> str:
        prompt = f"""
Write an evaluation plan (2-4 paragraphs, plain prose, no markdown or headers)
describing how success will be measured for this project.

Project title: {getattr(proposal, "title", "")}
Project objectives: {project_knowledge.objectives or "Not yet specified"}
Intended outputs: {project_knowledge.outputs or "Not yet specified"}
Intended outcomes: {project_knowledge.outcomes or "Not yet specified"}
KPIs already defined: {json.dumps(project_knowledge.kpis or [])}
{f"Additional context: {additional_context}" if additional_context else ""}

Describe evaluation methods, data collection approach, and how KPIs will be
tracked over the period of performance. Do not fabricate specific numeric
targets beyond what is given above — describe the approach generally where
targets are not yet defined.
""".strip()
        try:
            response = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are an expert grant writer producing an evaluation plan. Output plain prose only — no markdown, no headers, no bullet characters."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.6,
                max_tokens=700,
            )
        except Exception as exc:
            raise _ai_error(exc)
        plan = response.choices[0].message.content.strip()
        if db is not None:
            prompt_tokens, completion_tokens = usage_from_response(response)
            await usage_tracking.record_usage(
                db, org_id=org_id, user_id=user_id, operation="scope_of_work:evaluation_plan",
                model=settings.OPENAI_MODEL, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                price_cents_charged=price_cents_charged, reference={"proposal_id": getattr(proposal, "id", None)},
            )
        return plan

    async def generate_work_breakdown(
        self, proposal: Any, project_knowledge: ProjectKnowledge, scope_of_work: ScopeOfWork,
        company_profile: Dict[str, Any], additional_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Returns a parsed (not yet persisted) suggested work breakdown:
        {"work_packages": [{"name","description","start_month","end_month",
        "tasks": [str], "milestones": [str], "deliverables": [str]}, ...]}.
        Caller persists it via apply_generated_work_breakdown()."""
        prompt = f"""
Propose a work breakdown structure for this project as JSON only (no markdown
fences, no commentary — just the JSON object).

Project title: {getattr(proposal, "title", "")}
Agency / grant type: {getattr(proposal, "agency", "")} / {getattr(proposal, "grant_type", "")}
Research focus: {getattr(proposal, "research_focus", "") or "Not yet specified"}
Methodology narrative: {scope_of_work.methodology_narrative or "Not yet specified"}
Period of performance (months): {scope_of_work.period_of_performance_months or "Not yet specified"}
{f"Additional context: {additional_context}" if additional_context else ""}

Return JSON matching exactly this shape:
{{
  "work_packages": [
    {{
      "name": "string",
      "description": "string",
      "start_month": 1,
      "end_month": 6,
      "tasks": ["string", "..."],
      "milestones": ["string", "..."],
      "deliverables": ["string", "..."]
    }}
  ]
}}

Propose 3-6 work packages spanning the full period of performance, each with
2-5 tasks. Only include milestones/deliverables where genuinely meaningful —
empty lists are fine. Month numbers are 1-indexed from project start.
""".strip()
        try:
            response = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are an expert project planner. Respond with a single JSON object only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5,
                max_tokens=1800,
            )
        except Exception as exc:
            raise _ai_error(exc)
        result = _parse_json_response(response.choices[0].message.content or "")
        # Phase 3 §4.7 — Administrator-Only Engineering Economics. Same
        # "_usage rides along in the return dict" convention as
        # foa_parser.py::analyze_opportunity — the caller
        # (routers/scope_of_work.py::generate_work_breakdown) pops it off
        # and hands it to engines/usage_tracking.record_usage().
        prompt_tokens, completion_tokens = usage_from_response(response)
        result["_usage"] = {"model": settings.OPENAI_MODEL, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
        return result

    async def apply_generated_work_breakdown(
        self, db: AsyncSession, proposal_id: str, breakdown: Dict[str, Any],
    ) -> Dict[str, List[Any]]:
        """Persists a generate_work_breakdown() result as real rows."""
        created: Dict[str, List[Any]] = {"work_packages": [], "tasks": [], "milestones": [], "deliverables": []}
        for wp_data in breakdown.get("work_packages", []) or []:
            wp = await self.create_work_package(db, proposal_id, {
                "name": wp_data.get("name") or "Untitled Work Package",
                "description": wp_data.get("description"),
                "start_month": wp_data.get("start_month"),
                "end_month": wp_data.get("end_month"),
            })
            created["work_packages"].append(wp)
            for t_name in wp_data.get("tasks", []) or []:
                created["tasks"].append(await self.create_task(db, proposal_id, wp.id, {"name": t_name}))
            for m_name in wp_data.get("milestones", []) or []:
                created["milestones"].append(await self.create_milestone(db, proposal_id, {"name": m_name, "work_package_id": wp.id}))
            for d_name in wp_data.get("deliverables", []) or []:
                created["deliverables"].append(await self.create_deliverable(db, proposal_id, {"name": d_name, "work_package_id": wp.id}))
        return created

    # ── Derive from Proposal / inline field suggestions ─────────────────────
    # Both power the Scope of Work tab's non-blank-slate onramps: a one-click
    # "Derive from Proposal" action for proposals that already have generated
    # section content, and a per-field "Suggest" button for manual editing —
    # distinct from generate_methodology_narrative/generate_evaluation_plan
    # above, which draft from proposal *metadata* only (title/agency/research
    # focus) rather than the actual written narrative.

    _SUGGESTABLE_FIELDS: Dict[str, str] = {
        "objectives": "the project's objectives — what it aims to achieve",
        "need_statement": "the need statement — the problem or gap this project addresses",
        "outputs": "the project's intended outputs — concrete things produced",
        "outcomes": "the project's intended outcomes — longer-term changes/results",
    }

    async def derive_project_knowledge_from_proposal(
        self, db: AsyncSession, proposal: Any, project_knowledge: ProjectKnowledge,
        org_id: Optional[str] = None, user_id: Optional[str] = None, price_cents_charged: int = 0,
    ) -> Dict[str, Any]:
        """Reads the proposal's already-generated ProposalSection content and
        extracts a Project Knowledge / Scope of Work draft from it. Returns an
        unpersisted dict for the caller to show for review — nothing is saved
        until the user clicks Save on each card, same as every other AI
        action in this engine. Raises 400 if the proposal has no generated
        section content yet (nothing to derive from) rather than silently
        returning nulls, so the frontend can point the user at manual entry
        or full proposal generation instead."""
        result = await db.execute(
            select(ProposalSection).where(ProposalSection.proposal_id == proposal.id).order_by(ProposalSection.section_id)
        )
        sections = [s for s in result.scalars().all() if (s.content or "").strip()]
        if not sections:
            raise HTTPException(
                status_code=400,
                detail="This proposal doesn't have any generated section content yet to derive a Scope of Work from. Generate proposal sections first, or fill in Project Knowledge manually below.",
            )

        # Bound the context sent to the model rather than concatenating the
        # entire proposal — generous enough for a full SBIR/STTR narrative,
        # cut off gracefully if it runs long.
        blocks: List[str] = []
        budget = 12000
        for s in sections:
            chunk = f"### {s.title}\n{s.content.strip()}"
            if len(chunk) > budget:
                chunk = chunk[:budget]
            blocks.append(chunk)
            budget -= len(chunk)
            if budget <= 0:
                break
        proposal_text = "\n\n".join(blocks)

        prompt = f"""
Read the following already-written grant proposal sections and extract a
Project Knowledge / Scope of Work summary as JSON only (no markdown fences,
no commentary — just the JSON object).

Project title: {getattr(proposal, "title", "")}
Agency / grant type: {getattr(proposal, "agency", "")} / {getattr(proposal, "grant_type", "")}

--- PROPOSAL SECTIONS ---
{proposal_text}
--- END PROPOSAL SECTIONS ---

Return JSON matching exactly this shape:
{{
  "objectives": "string or null",
  "need_statement": "string or null",
  "outputs": "string or null",
  "outcomes": "string or null",
  "evaluation_plan": "string or null",
  "methodology_narrative": "string or null"
}}

Each value should be plain prose (no markdown, no bullets) summarizing what
the proposal sections already say — do not invent details the sections don't
support. Use null for any field the sections genuinely don't address.
""".strip()
        try:
            response = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are an expert grant analyst extracting structured project information from a written proposal. Respond with a single JSON object only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=1600,
            )
        except Exception as exc:
            raise _ai_error(exc)
        parsed = _parse_json_response(response.choices[0].message.content or "")
        parsed["source_sections_used"] = [s.section_id for s in sections]
        prompt_tokens, completion_tokens = usage_from_response(response)
        await usage_tracking.record_usage(
            db, org_id=org_id, user_id=user_id, operation="scope_of_work:derive_from_proposal",
            model=settings.OPENAI_MODEL, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            price_cents_charged=price_cents_charged, reference={"proposal_id": proposal.id},
        )
        return parsed

    async def generate_risks_and_kpis(
        self, db: AsyncSession, proposal: Any, project_knowledge: ProjectKnowledge,
        org_id: Optional[str] = None, user_id: Optional[str] = None, price_cents_charged: int = 0,
    ) -> Dict[str, Any]:
        """Powers the Risks/KPIs sections' "AI Suggest" button — added after
        a report that manually adding risks/KPIs one at a time via the
        +Add Risk/+Add KPI buttons produced a disjointed list with no real
        connection to what the proposal actually says. Reads the same
        generated ProposalSection content
        derive_project_knowledge_from_proposal() does (same "requires
        generated sections" gate and reason: nothing to ground a
        proposal-specific risk/KPI list in otherwise), plus whatever
        Project Knowledge fields and risks/KPIs already exist, so results
        are additive and non-repetitive rather than a generic boilerplate
        list. Returns an unpersisted result for the caller to append to the
        existing risks/kpis arrays for review — nothing is saved until Save
        Project Knowledge is clicked, same as every other AI action here."""
        result = await db.execute(
            select(ProposalSection).where(ProposalSection.proposal_id == proposal.id).order_by(ProposalSection.section_id)
        )
        sections = [s for s in result.scalars().all() if (s.content or "").strip()]
        if not sections:
            raise HTTPException(
                status_code=400,
                detail="This proposal doesn't have any generated section content yet to generate risks/KPIs from. Generate proposal sections first, or add them manually below.",
            )

        blocks: List[str] = []
        budget = 12000
        for s in sections:
            chunk = f"### {s.title}\n{s.content.strip()}"
            if len(chunk) > budget:
                chunk = chunk[:budget]
            blocks.append(chunk)
            budget -= len(chunk)
            if budget <= 0:
                break
        proposal_text = "\n\n".join(blocks)

        prompt = f"""
Read the following already-written grant proposal sections and propose a list
of project risks and key performance indicators (KPIs) as JSON only (no
markdown fences, no commentary — just the JSON object).

Project title: {getattr(proposal, "title", "")}
Agency / grant type: {getattr(proposal, "agency", "")} / {getattr(proposal, "grant_type", "")}
Project objectives: {project_knowledge.objectives or "Not yet specified"}
Need statement: {project_knowledge.need_statement or "Not yet specified"}
Intended outputs: {project_knowledge.outputs or "Not yet specified"}
Intended outcomes: {project_knowledge.outcomes or "Not yet specified"}
Risks already listed: {json.dumps(project_knowledge.risks or [])}
KPIs already listed: {json.dumps(project_knowledge.kpis or [])}

--- PROPOSAL SECTIONS ---
{proposal_text}
--- END PROPOSAL SECTIONS ---

Return JSON matching exactly this shape:
{{
  "risks": [
    {{"risk": "string", "mitigation": "string", "likelihood": "Low|Medium|High", "impact": "Low|Medium|High"}}
  ],
  "kpis": [
    {{"name": "string", "target": "string", "unit": "string"}}
  ]
}}

Propose 3-5 risks and 3-6 KPIs specific to what this proposal actually
describes (technical approach, timeline, team, deliverables) — not generic
grant-writing boilerplate. Do not repeat any risk or KPI already listed
above; propose new, distinct ones, or return a shorter list if the proposal
genuinely doesn't support more. Do not fabricate numeric targets the text
doesn't support — use a qualitative target where a precise number isn't
grounded in the proposal.
""".strip()
        try:
            response = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are an expert grant reviewer identifying project risks and measurable KPIs from a written proposal. Respond with a single JSON object only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5,
                max_tokens=1200,
            )
        except Exception as exc:
            raise _ai_error(exc)
        parsed = _parse_json_response(response.choices[0].message.content or "")
        parsed["source_sections_used"] = [s.section_id for s in sections]
        prompt_tokens, completion_tokens = usage_from_response(response)
        await usage_tracking.record_usage(
            db, org_id=org_id, user_id=user_id, operation="scope_of_work:risks_kpis",
            model=settings.OPENAI_MODEL, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            price_cents_charged=price_cents_charged, reference={"proposal_id": proposal.id},
        )
        return parsed

    async def suggest_project_knowledge_field(
        self, proposal: Any, project_knowledge: ProjectKnowledge, company_profile: Dict[str, Any],
        field: str, current_value: Optional[str] = None, additional_context: Optional[str] = None,
        db: Optional[AsyncSession] = None, org_id: Optional[str] = None, user_id: Optional[str] = None,
        price_cents_charged: int = 0,
    ) -> str:
        """Powers the inline "Suggest" button next to a manually-edited
        Project Knowledge field. Unlike the full-field AI Draft buttons
        (which overwrite from scratch), this factors in whatever the user
        has already typed: if the field is in progress, it refines/completes
        it; if it's empty, it proposes a first draft. Either way the result
        replaces the field for the user to accept, edit further, or discard."""
        if field not in self._SUGGESTABLE_FIELDS:
            raise HTTPException(status_code=400, detail=f"Unknown field '{field}'.")
        label = self._SUGGESTABLE_FIELDS[field]
        sibling_context = "\n".join(
            f"{self._SUGGESTABLE_FIELDS[f]}: {getattr(project_knowledge, f, None) or 'Not yet specified'}"
            for f in self._SUGGESTABLE_FIELDS if f != field
        )
        prompt = f"""
Suggest a value for {label}, for the following project.

Project title: {getattr(proposal, "title", "")}
Agency / grant type: {getattr(proposal, "agency", "")} / {getattr(proposal, "grant_type", "")}
Research focus: {getattr(proposal, "research_focus", "") or "Not yet specified"}
{self._profile_block(company_profile)}

Other project knowledge already on file:
{sibling_context}

What the user has typed so far for this field ({"empty" if not (current_value or "").strip() else "in progress"}):
{current_value or "(nothing yet)"}
{f"Additional context: {additional_context}" if additional_context else ""}

If the user has already typed something, refine or complete it rather than
replacing it with something unrelated. If it's empty, propose a first draft.
Respond with plain prose only (1-3 sentences) — no markdown, no headers, no
surrounding quotation marks.
""".strip()
        try:
            response = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are an expert grant writer helping complete a project knowledge field. Output plain prose only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.6,
                max_tokens=300,
            )
        except Exception as exc:
            raise _ai_error(exc)
        suggestion = response.choices[0].message.content.strip()
        if db is not None:
            prompt_tokens, completion_tokens = usage_from_response(response)
            await usage_tracking.record_usage(
                db, org_id=org_id, user_id=user_id, operation=f"scope_of_work:suggest_field:{field}",
                model=settings.OPENAI_MODEL, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                price_cents_charged=price_cents_charged, reference={"proposal_id": getattr(proposal, "id", None)},
            )
        return suggestion

    # ── Budget sync (PRD §10: "Budget structure feeding the existing Budget Builder") ──

    async def sync_budget_from_scope_of_work(self, db: AsyncSession, proposal_id: str) -> Tuple[BudgetRecord, int]:
        """Merges each work package's `estimated_cost` into BudgetRecord.other_direct
        as one line item tagged source="scope_of_work" + work_package_id, so a
        re-sync replaces only its own previously-generated items and never
        touches line items the user added by hand in the Budget Builder."""
        sow = await self.get_or_create_scope_of_work(db, proposal_id)
        work_packages = await self.list_work_packages(db, sow.id)

        result = await db.execute(select(BudgetRecord).where(BudgetRecord.proposal_id == proposal_id))
        budget = result.scalar_one_or_none()
        if not budget:
            budget = BudgetRecord(id=new_uuid(), proposal_id=proposal_id)
            db.add(budget)
            await db.flush()

        kept = [item for item in (budget.other_direct or []) if item.get("source") != "scope_of_work"]
        synced = 0
        for wp in work_packages:
            if wp.estimated_cost:
                kept.append({
                    "id": f"sow-{wp.id}",
                    "category": "Scope of Work",
                    "description": wp.name,
                    "cost": wp.estimated_cost,
                    "source": "scope_of_work",
                    "work_package_id": wp.id,
                })
                synced += 1
        budget.other_direct = kept
        _recalc_budget_totals(budget)

        pk = await self.get_or_create_project_knowledge(db, proposal_id)
        self.clear_stale(pk, "budget")

        await db.flush()
        await db.refresh(budget)
        return budget, synced
