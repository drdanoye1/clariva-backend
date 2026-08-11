"""Proposals router — CRUD + section generation, multi-grant-type aware."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from config import settings
from database import get_db
from models.db_models import Proposal, ProposalSection, ProposalStatusEvent, FOARecord, OrgContextDB, User, new_uuid
from engines.company_profile import get_org_context
from models.schemas import ProposalCreate, ProposalOut, SectionContent, SectionGenerateRequest
from engines.grant_templates import get_sections as get_grant_sections, list_grant_types
from routers.auth import get_current_user
from engines.proposal_generator import ProposalGeneratorEngine
from engines.workflow_engine import WorkflowEngine
from engines.credit_engine import CreditEngine, GENERATION_COST, debit_or_402
from engines import usage_tracking
from routers.organizations import _assert_member
from audit import log_action


class ProposalUpdate(BaseModel):
    title:            Optional[str] = None
    research_focus:   Optional[str] = None
    status:           Optional[str] = None
    # Taxonomy override — lets users fix grant type on existing proposals
    grant_type:       Optional[str] = None
    agency:           Optional[str] = None
    beneficiary_type: Optional[str] = None
    funder_class:     Optional[str] = None
    program_label:    Optional[str] = None
    program_size:     Optional[str] = None
    grantor_name:     Optional[str] = None


class SectionEdit(BaseModel):
    content: str


router        = APIRouter()
generator     = ProposalGeneratorEngine()
workflow      = WorkflowEngine()
credit_engine = CreditEngine()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sections_for_grant_type(grant_type_val: str) -> list:
    """Convert grant template sections into the format used by ProposalSection rows."""
    return [
        {
            "section_id":        s["id"],
            "title":             s["title"],
            "guidance":          s.get("guidance", ""),
            "page_limit":        s.get("page_limit"),
            "evaluation_weight": s.get("weight", 0.10),
        }
        for s in get_grant_sections(grant_type_val)
    ]


async def _load_company_profile(user_id: str, db: AsyncSession) -> dict:
    # Funding Opportunity Intelligence, Phase 2 — OrgContextDB.user_id is no
    # longer unique (a user can also own org-shared profiles), so this must
    # go through get_org_context() rather than querying user_id directly;
    # see engines/company_profile.py's module docstring. Personal-profile
    # lookup only (org_id omitted) — proposal generation staying scoped to
    # "the generating user's own profile" is unchanged behavior from before
    # this migration.
    ctx = await get_org_context(db, user_id=user_id)
    if not ctx:
        return {}
    return {
        "organization_name":     ctx.organization_name,
        "industry":              ctx.industry,
        "core_technologies":     ctx.core_technologies or [],
        "prior_sbir_experience": ctx.prior_sbir_experience,
        "uei_number":            ctx.uei_number,
        "cage_code":             ctx.cage_code,
        "company_capabilities":  ctx.company_capabilities,
        "pi_name":               ctx.pi_name,
        "pi_credentials":        ctx.pi_credentials,
        "pi_orcid":              ctx.pi_orcid,
        "pi_degree":             ctx.pi_degree,
        "pi_affiliation":        ctx.pi_affiliation,
        "pi_publications":       ctx.pi_publications,
        "pi_prior_sbir_awards":  ctx.pi_prior_sbir_awards,
        "team_members":          ctx.team_members or [],
        "facilities":            ctx.facilities or [],
        "partners":              ctx.partners or [],
        "past_performance":      ctx.past_performance or [],
    }


async def _get_proposal_or_404(proposal_id: str, owner_id: str, db: AsyncSession) -> Proposal:
    result = await db.execute(
        select(Proposal).where(Proposal.id == proposal_id, Proposal.owner_id == owner_id)
    )
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return p


async def _load_proposal(proposal_id: str, db: AsyncSession, owner_id: Optional[str] = None) -> ProposalOut:
    q = select(Proposal).where(Proposal.id == proposal_id)
    if owner_id:
        q = q.where(Proposal.owner_id == owner_id)
    result = await db.execute(q)
    proposal = result.scalar_one_or_none()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    sec_result = await db.execute(
        select(ProposalSection)
        .where(ProposalSection.proposal_id == proposal_id)
        .order_by(ProposalSection.order_index)
    )
    sections = sec_result.scalars().all()

    return ProposalOut(
        proposal_id=proposal.id,
        title=proposal.title,
        agency=proposal.agency,
        phase=proposal.phase,
        grant_type=proposal.grant_type or "federal_other",
        status=proposal.status,
        version=proposal.version,
        created_at=proposal.created_at,
        updated_at=proposal.updated_at or proposal.created_at,
        beneficiary_type=proposal.beneficiary_type,
        funder_class=proposal.funder_class,
        program_label=proposal.program_label,
        program_size=proposal.program_size,
        grantor_name=proposal.grantor_name,
        sections=[
            SectionContent(
                section_id=s.section_id,
                title=s.title,
                content=s.content,
                word_count=s.word_count,
                page_estimate=s.page_estimate,
                compliance_flags=s.compliance_flags or [],
            )
            for s in sections
        ],
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/grant-types")
async def list_available_grant_types():
    """All supported grant types for the new proposal wizard."""
    return list_grant_types()


@router.post("/", response_model=ProposalOut, status_code=201)
async def create_proposal(
    body: ProposalCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    grant_type_val = body.grant_type.value if body.grant_type else "federal_other"
    agency_val     = body.agency if isinstance(body.agency, str) else (body.agency.value if body.agency else "OTHER")

    # Determine section scaffold
    if body.foa_id:
        foa_result = await db.execute(select(FOARecord).where(FOARecord.id == body.foa_id))
        foa_record = foa_result.scalar_one_or_none()
        if not foa_record:
            raise HTTPException(status_code=404, detail="FOA not found")

        # parsed_template can be None for FOAs that were synced from an
        # external source (Grants.gov/SAM.gov, Phase 4) but never had their
        # template parsed — fall back to an empty dict rather than assuming
        # every FOA record has one.
        parsed_template = foa_record.parsed_template or {}
        foa_agency   = parsed_template.get("agency", agency_val or "OTHER")
        foa_sections = parsed_template.get("ordered_sections", [])

        # Guard: reject FOA sections that look like topic areas rather than proposal narrative sections.
        # Topic-area titles from NSF/DOE/etc. solicitations (e.g. "Advanced Manufacturing",
        # "Agricultural Technologies") are NOT proposal sections — the FOA parser may have
        # mistakenly extracted them. A valid proposal section contains narrative keywords.
        _NARRATIVE_KEYWORDS = {
            "summary", "abstract", "aims", "approach", "merit", "impact", "narrative",
            "commerciali", "team", "personnel", "facility", "facilities", "budget",
            "justif", "innovation", "technical", "feasib", "reference", "background",
            "method", "objective", "significance", "environ", "resource", "plan",
            "transition", "management", "schedule", "milestone",
        }
        def _looks_like_proposal_section(s: dict) -> bool:
            title = s.get("title", "").lower()
            return any(kw in title for kw in _NARRATIVE_KEYWORDS)

        valid_foa_sections = [s for s in foa_sections if _looks_like_proposal_section(s)]

        if valid_foa_sections and len(valid_foa_sections) >= 3 and foa_agency != "OTHER":
            sections_meta = valid_foa_sections
        else:
            sections_meta = _sections_for_grant_type(grant_type_val)
    else:
        sections_meta = _sections_for_grant_type(grant_type_val)

    proposal = Proposal(
        id=str(uuid.uuid4()),
        owner_id=current_user.id,
        foa_id=body.foa_id,
        title=body.title,
        agency=agency_val,
        phase=body.phase.value,
        grant_type=grant_type_val,
        status="draft",
        research_focus=body.research_focus,
        innovation_description=body.innovation_description,
        commercialization_plan=body.commercialization_plan,
        team_description=body.team_description,
        # Taxonomy ground truth
        beneficiary_type=body.beneficiary_type,
        funder_class=body.funder_class,
        program_label=body.program_label,
        program_size=body.program_size,
        grantor_name=body.grantor_name,
    )
    db.add(proposal)
    await db.flush()

    for idx, sec in enumerate(sections_meta):
        db.add(ProposalSection(
            id=str(uuid.uuid4()),
            proposal_id=proposal.id,
            section_id=sec.get("section_id", f"section_{idx}"),
            title=sec.get("title", f"Section {idx + 1}"),
            content="",
            order_index=idx,
        ))

    await db.flush()
    return await _load_proposal(proposal.id, db)


@router.get("/", response_model=List[dict])
async def list_proposals(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Proposal).where(Proposal.owner_id == current_user.id)
        .order_by(Proposal.updated_at.desc())
    )
    return [
        {
            "proposal_id": p.id,
            "title":       p.title,
            "agency":      p.agency,
            "phase":       p.phase,
            "grant_type":  p.grant_type or "sbir",
            "status":      p.status,
            "version":     p.version,
            "updated_at":  p.updated_at,
        }
        for p in result.scalars().all()
    ]


@router.get("/{proposal_id}", response_model=ProposalOut)
async def get_proposal(
    proposal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _load_proposal(proposal_id, db, owner_id=current_user.id)


@router.post("/{proposal_id}/generate-section", response_model=SectionContent)
async def generate_section(
    proposal_id: str,
    body: SectionGenerateRequest,
    org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    org_id is optional and additive: pass it to meter this generation call
    against that organization's shared AI credit pool (Clariva Enterprise™
    PRD §13). Omit it (as every existing caller does today) and generation
    behaves exactly as before — unmetered personal use. Not automatically
    tied to org-shared proposals; the caller decides which pool to charge.
    """
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    if org_id:
        await _assert_member(org_id, current_user.id, db)
        await debit_or_402(credit_engine, db, org_id, current_user.id, GENERATION_COST,
                            reason=f"proposal_generation:section:{body.section_id}")
    company_profile = await _load_company_profile(current_user.id, db)

    sec_result = await db.execute(
        select(ProposalSection).where(
            ProposalSection.proposal_id == proposal_id,
            ProposalSection.section_id == body.section_id,
        )
    )
    section = sec_result.scalar_one_or_none()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")

    generated = await generator.generate_section(
        section_id=body.section_id,
        section_title=section.title,
        proposal=proposal,
        company_profile=company_profile,
        additional_context=body.additional_context,
    )

    section.content          = generated["content"]
    section.word_count       = generated["word_count"]
    section.page_estimate    = generated["page_estimate"]
    section.compliance_flags = generated.get("missing_flags", [])

    # Phase 3 §4.7 — Administrator-Only Engineering Economics.
    usage = generated.get("_usage") or {}
    await usage_tracking.record_usage(
        db, operation="proposal:generate_section", model=usage.get("model", settings.OPENAI_MODEL),
        prompt_tokens=usage.get("prompt_tokens", 0), completion_tokens=usage.get("completion_tokens", 0),
        org_id=org_id, user_id=current_user.id,
        price_cents_charged=int(GENERATION_COST * 100) if org_id else 0,
        reference={"proposal_id": proposal_id, "section_id": body.section_id},
    )
    await db.flush()

    return SectionContent(
        section_id=section.section_id,
        title=section.title,
        content=section.content,
        word_count=section.word_count,
        page_estimate=section.page_estimate,
        compliance_flags=section.compliance_flags or [],
    )


@router.post("/{proposal_id}/generate-all", response_model=ProposalOut)
async def generate_all_sections(
    proposal_id: str,
    org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """See generate_section() for the org_id / credit-metering contract."""
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    company_profile = await _load_company_profile(current_user.id, db)

    sec_result = await db.execute(
        select(ProposalSection)
        .where(ProposalSection.proposal_id == proposal_id)
        .order_by(ProposalSection.order_index)
    )
    sections = sec_result.scalars().all()

    # Only generate sections that are empty — skip already-filled ones
    empty_sections = [s for s in sections if not (s.content and s.content.strip())]

    if empty_sections and org_id:
        await _assert_member(org_id, current_user.id, db)
        await debit_or_402(credit_engine, db, org_id, current_user.id,
                            GENERATION_COST * len(empty_sections),
                            reason=f"proposal_generation:all:{proposal_id}")

    if empty_sections:
        # Fire all AI calls in parallel — reduces total time from O(n*15s) to O(15s)
        results = await asyncio.gather(
            *[
                generator.generate_section(
                    section_id=s.section_id,
                    section_title=s.title,
                    proposal=proposal,
                    company_profile=company_profile,
                )
                for s in empty_sections
            ],
            return_exceptions=True,
        )

        # Write results back sequentially (DB session is not concurrency-safe)
        for section, result in zip(empty_sections, results):
            if isinstance(result, Exception):
                # Log and skip failed sections rather than aborting the whole batch
                import logging
                logging.getLogger(__name__).error(
                    "Section %s generation failed: %s", section.section_id, result
                )
                continue
            section.content          = result["content"]
            section.word_count       = result["word_count"]
            section.page_estimate    = result["page_estimate"]
            section.compliance_flags = result.get("missing_flags", [])

            # Phase 3 §4.7 — Administrator-Only Engineering Economics.
            usage = result.get("_usage") or {}
            await usage_tracking.record_usage(
                db, operation="proposal:generate_section", model=usage.get("model", settings.OPENAI_MODEL),
                prompt_tokens=usage.get("prompt_tokens", 0), completion_tokens=usage.get("completion_tokens", 0),
                org_id=org_id, user_id=current_user.id,
                price_cents_charged=int(GENERATION_COST * 100) if org_id else 0,
                reference={"proposal_id": proposal_id, "section_id": section.section_id},
            )

    proposal.status = "in_review"
    await db.flush()
    return await _load_proposal(proposal_id, db)


@router.patch("/{proposal_id}/sections/{section_id}", response_model=SectionContent)
async def update_section_content(
    proposal_id: str,
    section_id: str,
    body: SectionEdit,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_proposal_or_404(proposal_id, current_user.id, db)
    sec_result = await db.execute(
        select(ProposalSection).where(
            ProposalSection.proposal_id == proposal_id,
            ProposalSection.section_id == section_id,
        )
    )
    section = sec_result.scalar_one_or_none()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")

    words = len(body.content.split()) if body.content.strip() else 0
    section.content       = body.content
    section.word_count    = words
    section.page_estimate = round(words / 500, 2)
    await db.flush()

    return SectionContent(
        section_id=section.section_id,
        title=section.title,
        content=section.content,
        word_count=section.word_count,
        page_estimate=section.page_estimate,
        compliance_flags=section.compliance_flags or [],
    )


@router.patch("/{proposal_id}", response_model=ProposalOut)
async def update_proposal(
    proposal_id: str,
    body: ProposalUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    if body.title is not None:
        proposal.title = body.title
    if body.research_focus is not None:
        proposal.research_focus = body.research_focus
    if body.status is not None:
        VALID = {"draft", "in_review", "optimizing", "ready", "submitted", "approved"}
        if body.status not in VALID:
            raise HTTPException(status_code=400, detail=f"Invalid status: {body.status}")
        # Version 3.0 upgrade, Phase 3.1 (Organizational Learning) — record
        # every status change as a discrete, timestamped event, mirroring
        # PipelineStageEvent's rationale exactly: `status` alone only ever
        # shows the current value, which made "proposals started, abandoned
        # and submitted" impossible to reconstruct historically. Only log a
        # genuine transition (skip a no-op PATCH that resends the same
        # status), same guard PipelineStageEvent's writer uses.
        if body.status != proposal.status:
            db.add(ProposalStatusEvent(
                id=new_uuid(), proposal_id=proposal.id,
                from_status=proposal.status, to_status=body.status,
                changed_by=current_user.id,
            ))
        proposal.status = body.status
    # Taxonomy overrides — allow fixing existing proposals without deletion
    if body.grant_type is not None:
        proposal.grant_type = body.grant_type
    if body.agency is not None:
        proposal.agency = body.agency
    if body.beneficiary_type is not None:
        proposal.beneficiary_type = body.beneficiary_type
    if body.funder_class is not None:
        proposal.funder_class = body.funder_class
    if body.program_label is not None:
        proposal.program_label = body.program_label
    if body.program_size is not None:
        proposal.program_size = body.program_size
    if body.grantor_name is not None:
        proposal.grantor_name = body.grantor_name
    await db.flush()
    return await _load_proposal(proposal_id, db)


@router.delete("/{proposal_id}", status_code=204)
async def delete_proposal(
    proposal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    await log_action(db, actor_id=current_user.id, action="proposal.deleted",
                      object_type="proposal", object_id=proposal_id,
                      detail={"title": proposal.title})
    await db.delete(proposal)


# ── Human-in-the-Loop Review & Approval ──────────────────────────────────────

class SectionReviewRequest(BaseModel):
    reviewer_name: str
    notes: Optional[str] = None
    approved: bool = True


class ProposalApprovalRequest(BaseModel):
    approver_name: str
    approval_notes: Optional[str] = None


@router.post("/{proposal_id}/sections/{section_id}/review")
async def review_section(
    proposal_id: str,
    section_id: str,
    body: SectionReviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark a proposal section as human-reviewed."""
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    result = await db.execute(
        select(ProposalSection).where(
            ProposalSection.proposal_id == proposal_id,
            ProposalSection.section_id == section_id,
        )
    )
    section = result.scalar_one_or_none()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")

    # Store review metadata in compliance_flags list (reuse existing JSON field)
    flags = list(section.compliance_flags or [])
    # Remove any existing review entry
    flags = [f for f in flags if not (isinstance(f, dict) and f.get("type") == "human_review")]
    flags.append({
        "type": "human_review",
        "approved": body.approved,
        "reviewer": body.reviewer_name,
        "notes": body.notes or "",
        "reviewed_at": datetime.utcnow().isoformat(),
    })
    section.compliance_flags = flags
    await db.commit()
    return {"section_id": section_id, "reviewed": True, "approved": body.approved}


@router.post("/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: str,
    body: ProposalApprovalRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Human approval gate — sets proposal status to 'approved'."""
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)

    # Verify all sections with content have been human-reviewed
    result = await db.execute(
        select(ProposalSection).where(ProposalSection.proposal_id == proposal_id)
    )
    sections = result.scalars().all()
    content_sections = [s for s in sections if s.content and s.content.strip()]
    unreviewed = [
        s.title for s in content_sections
        if not any(
            isinstance(f, dict) and f.get("type") == "human_review"
            for f in (s.compliance_flags or [])
        )
    ]
    if unreviewed:
        raise HTTPException(
            status_code=400,
            detail=f"The following sections have not been reviewed: {', '.join(unreviewed)}"
        )

    proposal.status = "approved"
    await log_action(db, actor_id=current_user.id, action="proposal.approved",
                      object_type="proposal", object_id=proposal_id,
                      detail={"approver": body.approver_name})
    await db.commit()
    return {
        "proposal_id": proposal_id,
        "status": "approved",
        "approver": body.approver_name,
        "approval_notes": body.approval_notes,
        "approved_at": datetime.utcnow().isoformat(),
    }


@router.post("/{proposal_id}/submit-for-review")
async def submit_for_review(
    proposal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Move proposal from draft to 'under_review' status."""
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    if proposal.status not in ("draft", "generated"):
        raise HTTPException(status_code=400, detail=f"Cannot submit for review from status '{proposal.status}'")
    proposal.status = "under_review"
    await db.commit()
    return {"proposal_id": proposal_id, "status": "under_review"}


@router.get("/{proposal_id}/review-status")
async def get_review_status(
    proposal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return per-section review status for the proposal."""
    proposal = await _get_proposal_or_404(proposal_id, current_user.id, db)
    result = await db.execute(
        select(ProposalSection).where(ProposalSection.proposal_id == proposal_id)
    )
    sections = result.scalars().all()
    section_statuses = []
    for s in sections:
        review = next(
            (f for f in (s.compliance_flags or []) if isinstance(f, dict) and f.get("type") == "human_review"),
            None,
        )
        section_statuses.append({
            "section_id": s.section_id,
            "title": s.title,
            "has_content": bool(s.content and s.content.strip()),
            "reviewed": review is not None,
            "approved": review.get("approved") if review else None,
            "reviewer": review.get("reviewer") if review else None,
            "notes": review.get("notes") if review else None,
            "reviewed_at": review.get("reviewed_at") if review else None,
        })
    reviewed_count = sum(1 for s in section_statuses if s["reviewed"] and s["has_content"])
    total_content = sum(1 for s in section_statuses if s["has_content"])
    return {
        "proposal_id": proposal_id,
        "proposal_status": proposal.status,
        "sections": section_statuses,
        "reviewed_count": reviewed_count,
        "total_sections": total_content,
        "all_reviewed": reviewed_count == total_content and total_content > 0,
    }
