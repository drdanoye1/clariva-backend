"""
Clariva Intelligent Grant Writing Platform — SQLAlchemy ORM Models
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Integer, String, Text, JSON
)
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


def new_uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id                = Column(String(36), primary_key=True, default=new_uuid)
    email             = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password   = Column(String(255), nullable=False)
    full_name         = Column(String(255), nullable=False)
    organization      = Column(String(255), nullable=False)
    is_active         = Column(Boolean, default=True)
    is_superadmin     = Column(Boolean, default=False, nullable=False)
    role              = Column(String(30), default="user", nullable=False)  # user | admin | superadmin
    subscription_plan = Column(String(30), default="free", nullable=False)  # free | starter | pro | enterprise

    # MFA (TOTP) — Clariva Enterprise™ PRD §18 "SSO/MFA required at enterprise tier".
    # mfa_secret is set (but mfa_enabled stays False) during /auth/mfa/setup until
    # the user confirms a code via /auth/mfa/verify.
    mfa_enabled       = Column(Boolean, default=False, nullable=False)
    mfa_secret        = Column(String(64), nullable=True)

    # SSO — schema groundwork only (PRD §12/§18). No SAML/OIDC flow is wired up
    # yet; there's no identity provider to integrate against. These columns
    # exist so a future SSO login path has somewhere to record which provider
    # and external subject ID a user is linked to, without a later migration.
    sso_provider      = Column(String(50), nullable=True)   # e.g. "okta", "azure_ad"
    sso_subject_id    = Column(String(255), nullable=True)  # the provider's stable user ID

    created_at        = Column(DateTime(timezone=True), server_default=func.now())
    updated_at        = Column(DateTime(timezone=True), onupdate=func.now())

    proposals   = relationship("Proposal", back_populates="owner")
    org_context = relationship("OrgContextDB", back_populates="user", uselist=False)


class OrgContextDB(Base):
    __tablename__ = "org_contexts"

    id                    = Column(String(36), primary_key=True, default=new_uuid)
    user_id               = Column(String(36), ForeignKey("users.id"), unique=True)

    # ── Company Overview ──────────────────────────────────────────────────────
    organization_name     = Column(String(255))
    industry               = Column(String(255))
    core_technologies      = Column(JSON, default=list)
    prior_sbir_experience  = Column(Boolean, default=False)
    uei_number             = Column(String(20), nullable=True)
    cage_code              = Column(String(10), nullable=True)
    company_capabilities   = Column(Text, nullable=True)   # narrative of core competencies

    # ── Firm Identity & Address ───────────────────────────────────────────────
    # Fields NASA's SBIR/STTR ProSAMS "Firm Information" form requires that
    # weren't previously captured anywhere in the profile — see
    # docs/ARCHITECTURE.md's Company Profile addendum for the source review.
    ein_tax_id             = Column(String(15), nullable=True)   # EIN / Tax ID
    duns_number            = Column(String(13), nullable=True)   # legacy identifier, still requested alongside UEI
    firm_street            = Column(String(255), nullable=True)
    firm_apt_suite         = Column(String(100), nullable=True)
    firm_city              = Column(String(100), nullable=True)
    firm_state             = Column(String(50), nullable=True)
    firm_zip               = Column(String(12), nullable=True)   # ZIP+4
    firm_phone             = Column(String(30), nullable=True)

    # ── Principal Investigator ────────────────────────────────────────────────
    pi_name                = Column(String(255), nullable=True)
    pi_credentials         = Column(Text, nullable=True)   # degrees, certifications
    pi_orcid               = Column(String(25), nullable=True)
    pi_degree              = Column(String(100), nullable=True)  # PhD, MD, etc.
    pi_affiliation         = Column(String(255), nullable=True)  # primary employer
    pi_publications        = Column(Integer, nullable=True)
    pi_prior_sbir_awards   = Column(Integer, nullable=True)
    pi_email               = Column(String(255), nullable=True)
    pi_phone               = Column(String(30), nullable=True)

    # ── Business Official & Authorized Contract Negotiator ───────────────────
    # NASA (and most federal SBIR/STTR programs) requires these as two
    # additional named contacts distinct from the PI on every proposal — the
    # profile previously had no place to store them at all.
    bo_name                = Column(String(255), nullable=True)
    bo_title               = Column(String(150), nullable=True)
    bo_phone               = Column(String(30), nullable=True)
    bo_email               = Column(String(255), nullable=True)
    acn_name               = Column(String(255), nullable=True)
    acn_title              = Column(String(150), nullable=True)
    acn_phone              = Column(String(30), nullable=True)
    acn_email              = Column(String(255), nullable=True)

    # ── Key Personnel / Team ──────────────────────────────────────────────────
    # [{name, title, role, credentials, effort_pct, years_exp, orcid,
    #   labor_category, education_level}]
    team_members          = Column(JSON, default=list)

    # ── Facilities & Equipment ────────────────────────────────────────────────
    # [{name, type, description, certifications, sq_footage, location}]
    facilities            = Column(JSON, default=list)

    # ── Partners / Collaborators ──────────────────────────────────────────────
    # [{name, type (subcontractor|consultant|research_institution|industry),
    #   role, pi_name, location, effort_pct, institution_type,
    #   contact_phone, contact_email, has_letter_of_commitment,
    #   include_in_ga, is_foreign_vendor}]
    partners              = Column(JSON, default=list)

    # ── Past Performance ──────────────────────────────────────────────────────
    # [{title, agency, award_number, amount, period, outcome, relevance}]
    past_performance      = Column(JSON, default=list)

    created_at            = Column(DateTime(timezone=True), server_default=func.now())
    updated_at            = Column(DateTime(timezone=True), onupdate=func.now())

    user = relationship("User", back_populates="org_context")


class FOARecord(Base):
    """
    The opportunity record — originally just an AI-parsed FOA upload, now
    doubling as the pre-award pipeline entry (Clariva Enterprise™ PRD §15,
    Phase 4). Existing upload/parse-text/parse-url flows keep working
    unchanged: every new column below is nullable or defaulted, and
    `org_id` stays null for a personal/solo-use record exactly like before.
    """
    __tablename__ = "foa_records"

    id                  = Column(String(36), primary_key=True, default=new_uuid)
    agency              = Column(String(20), nullable=False)
    program_title       = Column(String(500), nullable=False)
    solicitation_number = Column(String(100), nullable=True)
    phase               = Column(String(20), nullable=False)
    grant_type          = Column(String(30), nullable=False, default="sbir")
    raw_text            = Column(Text, nullable=True)
    parsed_template     = Column(JSON, nullable=True)
    total_page_limit    = Column(Integer, nullable=True)
    deadline            = Column(DateTime(timezone=True), nullable=True)
    uploaded_by         = Column(String(36), ForeignKey("users.id"))
    created_at          = Column(DateTime(timezone=True), server_default=func.now())

    # --- Phase 4 — Funding Intelligence & Grant Tracking (PRD §15) ---------
    # Nullable: a personal, unshared opportunity has no org workspace to
    # stamp — same nullability rationale as WorkspaceTask.org_id (Phase 3).
    org_id                  = Column(String(36), ForeignKey("organizations.id"), nullable=True, index=True)
    # identified | qualifying | pursuing | submitted | awarded | declined | no_go
    pipeline_stage          = Column(String(20), nullable=False, default="identified", index=True)
    # manual | grants_gov | sam_gov — how this record entered the pipeline
    source                  = Column(String(20), nullable=False, default="manual")
    # Opportunity number/ID from the external source, for sync dedupe —
    # unique together with `source` (enforced in the engine, not the DB,
    # since SQLite's partial-unique-index support is limited).
    external_id             = Column(String(100), nullable=True, index=True)
    external_url            = Column(String(1000), nullable=True)
    estimated_award_floor   = Column(Float, nullable=True)
    estimated_award_ceiling = Column(Float, nullable=True)
    eligibility_summary     = Column(Text, nullable=True)
    # bid | no_go | undecided
    bid_no_go_decision      = Column(String(20), nullable=True)
    bid_no_go_rationale     = Column(Text, nullable=True)
    assigned_to             = Column(String(36), ForeignKey("users.id"), nullable=True)
    last_synced_at          = Column(DateTime(timezone=True), nullable=True)

    # --- Phase 5 — Award & Project Management (PRD §17: renewals) ----------
    # Set when this pipeline entry was created as a renewal/continuation of
    # a prior Award (engines/award_engine.py::create_renewal_opportunity),
    # so the funding pipeline can show "Renewal of <award>" provenance.
    originating_award_id    = Column(String(36), ForeignKey("awards.id"), nullable=True)

    # --- Version 3.0 architecture upgrade, Phase 14 (Renewal Loop Closure) --
    # Freeform notes captured on the "Create Renewal Opportunity" form
    # (AwardPanel.tsx). Previously accepted by RenewalCreate but silently
    # discarded by the engine; now persisted so they're visible on the
    # pipeline entry the renewal creates.
    renewal_notes            = Column(Text, nullable=True)

    proposals = relationship("Proposal", back_populates="foa")


class Proposal(Base):
    __tablename__ = "proposals"

    id                     = Column(String(36), primary_key=True, default=new_uuid)
    owner_id               = Column(String(36), ForeignKey("users.id"), nullable=False)
    foa_id                 = Column(String(36), ForeignKey("foa_records.id"), nullable=True)
    title                  = Column(String(500), nullable=False)
    agency                 = Column(String(30), nullable=False)
    phase                  = Column(String(20), nullable=False)
    grant_type             = Column(String(30), nullable=True, default="sbir")
    status                 = Column(String(30), default="draft")
    research_focus         = Column(Text, nullable=True)
    innovation_description = Column(Text, nullable=True)
    commercialization_plan = Column(Text, nullable=True)
    team_description       = Column(Text, nullable=True)
    # Taxonomy ground truth — from Step 1/2/3 grant wizard
    beneficiary_type       = Column(String(100), nullable=True)  # e.g. "Non-Profit Organization"
    funder_class           = Column(String(100), nullable=True)  # e.g. "Federal Agencies"
    program_label          = Column(String(300), nullable=True)  # e.g. "Community Development Block Grant (CDBG)"
    program_size           = Column(String(50),  nullable=True)  # e.g. "$250K–$5M"
    grantor_name           = Column(String(200), nullable=True)  # for non-federal grants
    version                = Column(Integer, default=1)
    created_at             = Column(DateTime(timezone=True), server_default=func.now())
    updated_at             = Column(DateTime(timezone=True), onupdate=func.now())

    owner    = relationship("User", back_populates="proposals")
    foa      = relationship("FOARecord", back_populates="proposals")
    sections = relationship("ProposalSection", back_populates="proposal", cascade="all, delete-orphan")
    scores   = relationship("ScoringRecord",   back_populates="proposal", cascade="all, delete-orphan")
    reviews  = relationship("ReviewerRecord",  back_populates="proposal", cascade="all, delete-orphan")
    exports  = relationship("ExportRecord",    back_populates="proposal", cascade="all, delete-orphan")
    budget   = relationship("BudgetRecord",     back_populates="proposal", uselist=False, cascade="all, delete-orphan")
    project_knowledge = relationship("ProjectKnowledge", back_populates="proposal", uselist=False, cascade="all, delete-orphan")


class ProposalSection(Base):
    __tablename__ = "proposal_sections"

    id               = Column(String(36), primary_key=True, default=new_uuid)
    proposal_id      = Column(String(36), ForeignKey("proposals.id"), nullable=False)
    section_id       = Column(String(100), nullable=False)
    title            = Column(String(500), nullable=False)
    content          = Column(Text, default="")
    word_count       = Column(Integer, default=0)
    page_estimate    = Column(Float, default=0.0)
    compliance_flags = Column(JSON, default=list)
    order_index      = Column(Integer, default=0)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())
    updated_at       = Column(DateTime(timezone=True), onupdate=func.now())

    proposal = relationship("Proposal", back_populates="sections")


class ScoringRecord(Base):
    __tablename__ = "scoring_records"

    id               = Column(String(36), primary_key=True, default=new_uuid)
    proposal_id      = Column(String(36), ForeignKey("proposals.id"), nullable=False)
    total_score      = Column(Float, nullable=False)
    section_scores   = Column(JSON, nullable=True)
    risk_penalties   = Column(JSON, default=list)
    compliance_score = Column(Float, default=0.0)
    technical_merit  = Column(Float, default=0.0)
    commercialization = Column(Float, default=0.0)
    innovation       = Column(Float, default=0.0)
    team_score       = Column(Float, default=0.0)
    scored_at        = Column(DateTime(timezone=True), server_default=func.now())

    proposal = relationship("Proposal", back_populates="scores")


class ReviewerRecord(Base):
    __tablename__ = "reviewer_records"

    id                      = Column(String(36), primary_key=True, default=new_uuid)
    proposal_id             = Column(String(36), ForeignKey("proposals.id"), nullable=False)
    reviewer_type           = Column(String(50), nullable=False)
    overall_impression      = Column(Text, nullable=True)
    strengths               = Column(JSON, default=list)
    weaknesses              = Column(JSON, default=list)
    questions_for_applicant = Column(JSON, default=list)
    decision                = Column(String(100), nullable=True)
    confidence              = Column(Float, default=0.0)
    simulated_at            = Column(DateTime(timezone=True), server_default=func.now())

    proposal = relationship("Proposal", back_populates="reviews")


class ExportRecord(Base):
    __tablename__ = "export_records"

    id              = Column(String(36), primary_key=True, default=new_uuid)
    proposal_id     = Column(String(36), ForeignKey("proposals.id"), nullable=False)
    format          = Column(String(10), nullable=False)
    download_url    = Column(String(1000), nullable=True)
    file_size_bytes = Column(Integer, default=0)
    exported_at     = Column(DateTime(timezone=True), server_default=func.now())

    proposal = relationship("Proposal", back_populates="exports")


class Organization(Base):
    __tablename__ = "organizations"

    id         = Column(String(36), primary_key=True, default=new_uuid)
    name       = Column(String(255), nullable=False)
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Extension point for phased enterprise rollout (Clariva Enterprise™ PRD
    # §6.2): lets a future module (RBAC, Scope of Work Engine, funding
    # intelligence, etc.) be turned on per-organization without branching the
    # codebase. Empty dict = no flags set = all-new-modules-off, so this is a
    # pure no-op until something actually reads it.
    feature_flags = Column(JSON, default=dict, nullable=True)

    # --- Phase 6 — Integrations & Marketplace (PRD §20: white-label/reseller) --
    # All nullable/defaulted — an org with none of these set renders exactly
    # like today (Clariva-branded). `white_label_enabled` is the Enterprise-
    # tier gate a future billing check can read; the fields themselves are
    # harmless to set ahead of that gate existing.
    white_label_enabled = Column(Boolean, default=False, nullable=False)
    brand_name          = Column(String(255), nullable=True)
    logo_url            = Column(String(1000), nullable=True)
    primary_color       = Column(String(20), nullable=True)  # hex, e.g. "#1d4ed8"

    memberships      = relationship("OrgMembership", back_populates="organization", cascade="all, delete-orphan")
    shared_proposals = relationship("OrgProposal",   back_populates="organization", cascade="all, delete-orphan")


class OrgMembership(Base):
    __tablename__ = "org_memberships"

    id         = Column(String(36), primary_key=True, default=new_uuid)
    org_id     = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    user_id    = Column(String(36), ForeignKey("users.id"), nullable=False)
    role       = Column(String(20), default="viewer")
    invited_by = Column(String(36), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    organization = relationship("Organization", back_populates="memberships")
    user         = relationship("User", foreign_keys=[user_id])


class Invitation(Base):
    """
    Pending org-member invitation (post-Phase-6 addendum). Bridges the gap
    between an org buying N bundled seats and having N real logins: an
    owner/editor invites an email that has no existing User account (see
    routers/organizations.py::invite_member) — this row is created instead
    of the 404 that used to be the only outcome, and an email is sent
    (engines-adjacent email_service.py, via Resend) with a link containing
    `token`. The invitee follows the link to routers/invitations.py's
    public (no-auth) accept endpoint, sets their own password, and a User +
    OrgMembership (+ TeamMembership, if team_id is set) are created
    atomically — the owner never sees or handles a password. If the email
    already belongs to an existing User at invite time, no Invitation row
    is created at all; membership is still added immediately as before.
    """
    __tablename__ = "invitations"

    id            = Column(String(36), primary_key=True, default=new_uuid)
    org_id        = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    email         = Column(String(255), nullable=False, index=True)
    role          = Column(String(20), default="editor", nullable=False)
    # Optional — lets an owner assign the invitee straight into a team
    # and/or department (PRD §11 workspace hierarchy) at invite time,
    # rather than requiring a second step after they join.
    team_id       = Column(String(36), ForeignKey("teams.id"), nullable=True)
    department_id = Column(String(36), ForeignKey("departments.id"), nullable=True)
    token         = Column(String(64), unique=True, nullable=False, index=True)
    invited_by    = Column(String(36), ForeignKey("users.id"), nullable=False)
    status        = Column(String(20), default="pending", nullable=False)  # pending | accepted | revoked
    expires_at    = Column(DateTime(timezone=True), nullable=False)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())
    accepted_at   = Column(DateTime(timezone=True), nullable=True)


class OrgProposal(Base):
    __tablename__ = "org_proposals"

    id          = Column(String(36), primary_key=True, default=new_uuid)
    org_id      = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    proposal_id = Column(String(36), ForeignKey("proposals.id"), nullable=False)
    shared_by   = Column(String(36), ForeignKey("users.id"), nullable=False)
    # Phase 3 — Collaboration (PRD §11): optional link to the Team (within
    # this org) working on the proposal. See migrations.py for the
    # column-add entry (org_proposals already existed before Phase 3).
    team_id     = Column(String(36), ForeignKey("teams.id"), nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    organization = relationship("Organization", back_populates="shared_proposals")
    proposal     = relationship("Proposal")



class BudgetRecord(Base):
    __tablename__ = "budget_records"

    id          = Column(String(36), primary_key=True, default=new_uuid)
    proposal_id = Column(String(36), ForeignKey("proposals.id"), unique=True, nullable=False)

    # Budget period (months)
    budget_months   = Column(Integer, default=12)

    # Line item arrays (JSON)
    # [{id, name, role, annual_salary, fringe_rate, effort_pct}]
    personnel       = Column(JSON, default=list)
    # [{id, name, role, rate_per_day, days}]
    consultants     = Column(JSON, default=list)
    # [{id, name, description, cost}]
    equipment       = Column(JSON, default=list)
    # [{id, purpose, destination, trips, people, cost_per_trip}]
    travel          = Column(JSON, default=list)
    # [{id, category, description, cost}]
    other_direct    = Column(JSON, default=list)
    # [{id, organization, pi_name, total_cost}]
    subcontracts    = Column(JSON, default=list)

    # Indirect (F&A)
    indirect_rate   = Column(Float, default=0.0)   # e.g. 45.0 for 45%
    indirect_base   = Column(String(10), default="mtdc")  # "mtdc" | "tdc"
    fee_rate        = Column(Float, default=7.0)           # small business profit %

    # Cached totals
    total_direct    = Column(Float, default=0.0)
    total_indirect  = Column(Float, default=0.0)
    total_cost      = Column(Float, default=0.0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    proposal = relationship("Proposal", back_populates="budget")


class MemoryRecord(Base):
    __tablename__ = "memory_records"

    id              = Column(String(36), primary_key=True, default=new_uuid)
    org_id          = Column(String(36), ForeignKey("users.id"), nullable=False)
    proposal_id     = Column(String(36), ForeignKey("proposals.id"), nullable=False)
    agency          = Column(String(20), nullable=False)
    outcome         = Column(String(20), nullable=True)
    score           = Column(Float, nullable=True)
    lessons_learned = Column(JSON, default=list)
    embedding       = Column(JSON, nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())


# ── Phase 1 — Enterprise Foundations ───────────────────────────────────────────
# (Clariva Enterprise™ PRD §12 RBAC, §13 Shared AI Credits, §18 Security)

class AuditLog(Base):
    """
    Append-only record of permission-gated actions, per the PRD's Security
    section (§18: "full audit logging of permission-gated actions"). org_id
    is nullable because not every audited action is org-scoped (e.g. a user
    enabling their own MFA).
    """
    __tablename__ = "audit_logs"

    id          = Column(String(36), primary_key=True, default=new_uuid)
    org_id      = Column(String(36), ForeignKey("organizations.id"), nullable=True, index=True)
    actor_id    = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    action      = Column(String(100), nullable=False)        # e.g. "member.role_changed"
    object_type = Column(String(50), nullable=True)          # e.g. "org_membership"
    object_id   = Column(String(36), nullable=True)
    detail      = Column(JSON, nullable=True)                # arbitrary structured context
    created_at  = Column(DateTime(timezone=True), server_default=func.now())


class AICreditLedger(Base):
    """
    One row per organization: the shared AI credit pool balance (PRD §13).
    """
    __tablename__ = "ai_credit_ledgers"

    id         = Column(String(36), primary_key=True, default=new_uuid)
    org_id     = Column(String(36), ForeignKey("organizations.id"), unique=True, nullable=False)
    balance    = Column(Float, default=100.0, nullable=False)  # starter free allotment
    # High-water mark used to compute "% of pool remaining" for the low-balance
    # warning: seeded to `balance` at ledger creation, and reset to the new
    # `balance` every time credit() (a top-up) runs — so "20% remaining" always
    # means 20% of what the org most recently topped up to, not some fixed
    # historical maximum. See engines/credit_engine.py's LOW_BALANCE_WARNING_PCT.
    reference_balance = Column(Float, default=100.0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class CreditTransaction(Base):
    """
    Immutable ledger entry for every credit debit (AI usage) or credit
    (top-up/allotment) against an organization's AICreditLedger.
    """
    __tablename__ = "credit_transactions"

    id            = Column(String(36), primary_key=True, default=new_uuid)
    org_id        = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    user_id       = Column(String(36), ForeignKey("users.id"), nullable=True)  # who triggered it; null for top-ups
    amount        = Column(Float, nullable=False)          # negative = debit, positive = credit/top-up
    reason        = Column(String(255), nullable=False)    # e.g. "proposal_generation:section:<id>"
    balance_after = Column(Float, nullable=False)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())


class CreditAllocation(Base):
    """
    Optional spending cap within an organization's shared pool (PRD §13:
    "Organization Admins allocate credit budgets to departments/teams and
    can cap per-user or per-project consumption"). Exactly one of
    (user_id, team_id, department_id) should be set for a scoped cap;
    all three null represents the org-wide default cap applied when no
    more specific cap matches (see CreditEngine.check_allocation, which
    enforces personal -> team -> department -> org-wide-default caps
    independently — a spend is blocked if it would exceed ANY applicable
    level, the same way a real budget hierarchy works).
    """
    __tablename__ = "credit_allocations"

    id            = Column(String(36), primary_key=True, default=new_uuid)
    org_id        = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    user_id       = Column(String(36), ForeignKey("users.id"), nullable=True)
    team_id       = Column(String(36), ForeignKey("teams.id"), nullable=True)
    department_id = Column(String(36), ForeignKey("departments.id"), nullable=True)
    cap           = Column(Float, nullable=True)              # None = unlimited
    period        = Column(String(20), default="monthly", nullable=False)  # "monthly" | "total"
    created_at    = Column(DateTime(timezone=True), server_default=func.now())
    updated_at    = Column(DateTime(timezone=True), onupdate=func.now())


# ── Phase 2 — Scope of Work Engine & Project Knowledge Base ────────────────────
# (Clariva Enterprise™ PRD §9 Shared Project Knowledge Base, §10 Scope of Work
# Engine). See docs/ARCHITECTURE.md §7 for the design rationale. One-to-one
# chain: Proposal -> ProjectKnowledge -> ScopeOfWork -> WorkPackage -> Task,
# with Milestone/Deliverable hanging off ScopeOfWork (optionally tagged to a
# WorkPackage). Existing Proposal/ProposalSection/BudgetRecord rows are
# untouched — this is an additive layer, exactly as the PRD specifies.

class ProjectKnowledge(Base):
    """
    Project-level tier of the Shared Project Knowledge Base (PRD §9) — one
    row per pursuit, one-to-one with a Proposal. The org-level tier already
    exists as OrgContextDB (company profile).
    """
    __tablename__ = "project_knowledge"

    id          = Column(String(36), primary_key=True, default=new_uuid)
    proposal_id = Column(String(36), ForeignKey("proposals.id"), unique=True, nullable=False)

    objectives      = Column(Text, nullable=True)
    need_statement  = Column(Text, nullable=True)
    evaluation_plan = Column(Text, nullable=True)
    risks           = Column(JSON, default=list)   # [{risk, mitigation, likelihood, impact}]
    outputs         = Column(Text, nullable=True)
    outcomes        = Column(Text, nullable=True)
    kpis            = Column(JSON, default=list)   # [{name, target, unit}]

    # Smart Dependency Engine (PRD §10.1): a dict of downstream artifacts that
    # need review after the last Scope of Work change, e.g.
    # {"budget": true, "sections": ["technical_approach", "budget_justification"]}.
    # Flagging only, not auto-regeneration — see docs/ARCHITECTURE.md §7.
    stale_flags     = Column(JSON, default=dict)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    proposal      = relationship("Proposal", back_populates="project_knowledge")
    scope_of_work = relationship("ScopeOfWork", back_populates="project_knowledge",
                                  uselist=False, cascade="all, delete-orphan")


class ScopeOfWork(Base):
    """
    The "digital twin" of the funded project (PRD §10): one row per
    ProjectKnowledge, parent of the work-package/task/milestone/deliverable
    hierarchy.
    """
    __tablename__ = "scope_of_work"

    id                    = Column(String(36), primary_key=True, default=new_uuid)
    project_knowledge_id  = Column(String(36), ForeignKey("project_knowledge.id"), unique=True, nullable=False)

    period_of_performance_months = Column(Integer, nullable=True)
    methodology_narrative        = Column(Text, nullable=True)
    logic_model         = Column(JSON, default=dict)   # {inputs, activities, outputs, outcomes}
    reporting_schedule  = Column(JSON, default=list)   # [{name, frequency, due_month}]

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    project_knowledge = relationship("ProjectKnowledge", back_populates="scope_of_work")
    work_packages = relationship("WorkPackage", back_populates="scope_of_work",
                                  cascade="all, delete-orphan", order_by="WorkPackage.order_index")
    milestones    = relationship("Milestone", back_populates="scope_of_work",
                                  cascade="all, delete-orphan", order_by="Milestone.due_month")
    deliverables  = relationship("Deliverable", back_populates="scope_of_work",
                                  cascade="all, delete-orphan", order_by="Deliverable.due_month")


class WorkPackage(Base):
    __tablename__ = "work_packages"

    id               = Column(String(36), primary_key=True, default=new_uuid)
    scope_of_work_id = Column(String(36), ForeignKey("scope_of_work.id"), nullable=False)
    name             = Column(String(255), nullable=False)
    description      = Column(Text, nullable=True)
    lead             = Column(String(255), nullable=True)
    start_month      = Column(Integer, nullable=True)
    end_month        = Column(Integer, nullable=True)
    # Total estimated cost for this work package — the unit the budget-sync
    # (see engines/scope_of_work_engine.py::sync_budget_from_scope_of_work)
    # merges into BudgetRecord.other_direct as one tagged line item.
    estimated_cost   = Column(Float, nullable=True)
    order_index      = Column(Integer, default=0)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())
    updated_at       = Column(DateTime(timezone=True), onupdate=func.now())

    scope_of_work = relationship("ScopeOfWork", back_populates="work_packages")
    tasks = relationship("Task", back_populates="work_package",
                          cascade="all, delete-orphan", order_by="Task.order_index")


class Task(Base):
    __tablename__ = "sow_tasks"  # "sow_" prefix avoids ever colliding with an unrelated future "tasks" concept

    id              = Column(String(36), primary_key=True, default=new_uuid)
    work_package_id = Column(String(36), ForeignKey("work_packages.id"), nullable=False)
    name            = Column(String(255), nullable=False)
    description     = Column(Text, nullable=True)
    owner           = Column(String(255), nullable=True)
    start_month     = Column(Integer, nullable=True)
    end_month       = Column(Integer, nullable=True)
    status          = Column(String(20), default="not_started")  # not_started | in_progress | complete
    order_index     = Column(Integer, default=0)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    updated_at      = Column(DateTime(timezone=True), onupdate=func.now())

    work_package = relationship("WorkPackage", back_populates="tasks")


class Milestone(Base):
    __tablename__ = "milestones"

    id               = Column(String(36), primary_key=True, default=new_uuid)
    scope_of_work_id = Column(String(36), ForeignKey("scope_of_work.id"), nullable=False)
    work_package_id  = Column(String(36), ForeignKey("work_packages.id"), nullable=True)
    name             = Column(String(255), nullable=False)
    description      = Column(Text, nullable=True)
    due_month        = Column(Integer, nullable=True)
    status           = Column(String(20), default="pending")  # pending | complete
    created_at       = Column(DateTime(timezone=True), server_default=func.now())
    updated_at       = Column(DateTime(timezone=True), onupdate=func.now())

    scope_of_work = relationship("ScopeOfWork", back_populates="milestones")


class Deliverable(Base):
    __tablename__ = "deliverables"

    id                = Column(String(36), primary_key=True, default=new_uuid)
    scope_of_work_id  = Column(String(36), ForeignKey("scope_of_work.id"), nullable=False)
    work_package_id   = Column(String(36), ForeignKey("work_packages.id"), nullable=True)
    name              = Column(String(255), nullable=False)
    description       = Column(Text, nullable=True)
    due_month         = Column(Integer, nullable=True)
    deliverable_type  = Column(String(50), nullable=True)   # report | product | dataset | other
    status            = Column(String(20), default="pending")  # pending | complete
    created_at        = Column(DateTime(timezone=True), server_default=func.now())
    updated_at        = Column(DateTime(timezone=True), onupdate=func.now())

    scope_of_work = relationship("ScopeOfWork", back_populates="deliverables")


# ── Phase 3 — Collaboration & Content Management ────────────────────────────────
# (Clariva Enterprise™ PRD §11 Internal and External Collaboration, §14
# Document Sharing). See docs/ARCHITECTURE.md §8 for the design rationale —
# in particular why WorkspaceGuestAccess is deliberately NOT an OrgMembership
# row, and why WorkspaceTask/Comment are separate from Phase 2's SOW Task.

class Department(Base):
    __tablename__ = "departments"

    id         = Column(String(36), primary_key=True, default=new_uuid)
    org_id     = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    name       = Column(String(255), nullable=False)
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    teams = relationship("Team", back_populates="department", cascade="all, delete-orphan")


class Team(Base):
    __tablename__ = "teams"

    id            = Column(String(36), primary_key=True, default=new_uuid)
    org_id        = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    department_id = Column(String(36), ForeignKey("departments.id"), nullable=True)
    name          = Column(String(255), nullable=False)
    created_by    = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())

    department = relationship("Department", back_populates="teams")
    members    = relationship("TeamMembership", back_populates="team", cascade="all, delete-orphan")


class TeamMembership(Base):
    __tablename__ = "team_memberships"

    id         = Column(String(36), primary_key=True, default=new_uuid)
    team_id    = Column(String(36), ForeignKey("teams.id"), nullable=False, index=True)
    user_id    = Column(String(36), ForeignKey("users.id"), nullable=False)
    role       = Column(String(20), default="member")  # member | lead
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    team = relationship("Team", back_populates="members")


class WorkspaceTask(Base):
    """
    Generic task assignment (PRD §11) — distinct from the Scope of Work
    Engine's project-plan Task (`sow_tasks`, Phase 2): this is a lightweight
    to-do assignable to any org member on a proposal (or org-wide, if
    proposal_id is null), not part of the funded project's formal work
    breakdown.
    """
    __tablename__ = "workspace_tasks"

    id          = Column(String(36), primary_key=True, default=new_uuid)
    # Nullable: a task on a proposal that isn't (yet) shared to any org has
    # no workspace to stamp — see workspace_access.py's ProposalAccess.org_id.
    org_id      = Column(String(36), ForeignKey("organizations.id"), nullable=True, index=True)
    proposal_id = Column(String(36), ForeignKey("proposals.id"), nullable=True, index=True)
    title       = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    assignee_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    status      = Column(String(20), default="open")  # open | in_progress | done
    due_date    = Column(DateTime(timezone=True), nullable=True)
    created_by  = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    updated_at  = Column(DateTime(timezone=True), onupdate=func.now())


class Comment(Base):
    """
    Threaded comments on any object (proposal, proposal section, budget
    line, Scope of Work task, workspace task, document, ...) per PRD §11.
    Polymorphic via (object_type, object_id) rather than a FK per object
    type, since the PRD explicitly calls for commenting on "any object."
    """
    __tablename__ = "comments"

    id                = Column(String(36), primary_key=True, default=new_uuid)
    # Nullable for the same reason as WorkspaceTask.org_id above.
    org_id            = Column(String(36), ForeignKey("organizations.id"), nullable=True, index=True)
    object_type       = Column(String(50), nullable=False, index=True)   # e.g. "proposal", "workspace_task"
    object_id         = Column(String(36), nullable=False, index=True)
    parent_comment_id = Column(String(36), ForeignKey("comments.id"), nullable=True)
    author_id         = Column(String(36), ForeignKey("users.id"), nullable=False)
    content           = Column(Text, nullable=False)
    mentions          = Column(JSON, default=list)   # [user_id, ...] parsed from @mentions
    edited            = Column(Boolean, default=False)
    created_at        = Column(DateTime(timezone=True), server_default=func.now())
    updated_at        = Column(DateTime(timezone=True), onupdate=func.now())


class Notification(Base):
    __tablename__ = "notifications"

    id          = Column(String(36), primary_key=True, default=new_uuid)
    user_id     = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    type        = Column(String(50), nullable=False)   # mention | task_assigned | approval_requested | approval_decided | comment_reply
    message     = Column(Text, nullable=False)
    object_type = Column(String(50), nullable=True)
    object_id   = Column(String(36), nullable=True)
    read        = Column(Boolean, default=False)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())


class ApprovalRequest(Base):
    """
    Configurable approval workflow (PRD §11), generalized beyond the
    single-shot proposal approve/approval_notes flow already in
    routers/proposals.py (left untouched, for backward compatibility) to
    any object type.
    """
    __tablename__ = "approval_requests"

    id             = Column(String(36), primary_key=True, default=new_uuid)
    # Nullable for the same reason as WorkspaceTask.org_id / Comment.org_id.
    org_id         = Column(String(36), ForeignKey("organizations.id"), nullable=True, index=True)
    object_type    = Column(String(50), nullable=False)
    object_id      = Column(String(36), nullable=False)
    requested_by   = Column(String(36), ForeignKey("users.id"), nullable=False)
    approver_id    = Column(String(36), ForeignKey("users.id"), nullable=True)  # None = any owner may decide
    status         = Column(String(20), default="pending")  # pending | approved | rejected
    notes          = Column(Text, nullable=True)             # requester's context
    decision_notes = Column(Text, nullable=True)
    decided_by     = Column(String(36), ForeignKey("users.id"), nullable=True)
    decided_at     = Column(DateTime(timezone=True), nullable=True)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())


class WorkspaceGuestAccess(Base):
    """
    External guest tier (PRD §11): scoped to ONE proposal, never full org
    visibility. Deliberately NOT an OrgMembership row — a guest never
    appears in the org member list or gains any org-wide permission. See
    workspace_access.py for how this is checked alongside ownership and
    OrgMembership.
    """
    __tablename__ = "workspace_guest_access"

    id          = Column(String(36), primary_key=True, default=new_uuid)
    org_id      = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    proposal_id = Column(String(36), ForeignKey("proposals.id"), nullable=False, index=True)
    user_id     = Column(String(36), ForeignKey("users.id"), nullable=False)
    invited_by  = Column(String(36), ForeignKey("users.id"), nullable=False)
    can_comment = Column(Boolean, default=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())


# ── Phase 3 — Document Library (PRD §14) ────────────────────────────────────────

class Document(Base):
    """
    One entry per library item (org- or proposal-scoped); content lives in
    DocumentVersion rows, not here — this is the stable identity a version
    history hangs off of. Deliberately has no `current_version_id` pointer
    column (which would create a circular FK with document_versions,
    fragile under SQLite's limited ALTER TABLE support) — "current version"
    is just the row with the highest version_number for this document_id.
    """
    __tablename__ = "documents"

    id           = Column(String(36), primary_key=True, default=new_uuid)
    org_id       = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    proposal_id  = Column(String(36), ForeignKey("proposals.id"), nullable=True, index=True)
    # document | knowledge | graphics | template | budget | evaluation | methodology | past_awards
    library_type = Column(String(30), nullable=False, default="document")
    title        = Column(String(500), nullable=False)
    status       = Column(String(20), default="active")  # active | archived
    created_by   = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())
    updated_at   = Column(DateTime(timezone=True), onupdate=func.now())

    versions = relationship("DocumentVersion", back_populates="document", cascade="all, delete-orphan")
    shares   = relationship("DocumentShare", back_populates="document", cascade="all, delete-orphan")


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id             = Column(String(36), primary_key=True, default=new_uuid)
    document_id    = Column(String(36), ForeignKey("documents.id"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    content        = Column(Text, nullable=True)          # rich text / generated content
    file_url       = Column(String(1000), nullable=True)  # uploaded/exported file, if any
    format         = Column(String(10), nullable=True)    # docx | pdf | txt | md
    change_note    = Column(Text, nullable=True)
    # Best-effort semantic-search embedding (PRD §14) — see
    # document_library_engine.py. Null if generation failed or was skipped
    # (e.g. OpenAI unreachable); search falls back to keyword matching for
    # that version rather than blocking the save.
    embedding      = Column(JSON, nullable=True)
    created_by     = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())

    document = relationship("Document", back_populates="versions")


class DocumentShare(Base):
    """
    Internal share (shared_with_user_id set) or external share link
    (external_email + share_token) with optional expiration — PRD §14
    "external-share links with expiration for partner/funder distribution."
    """
    __tablename__ = "document_shares"

    id                  = Column(String(36), primary_key=True, default=new_uuid)
    document_id         = Column(String(36), ForeignKey("documents.id"), nullable=False, index=True)
    shared_with_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    external_email      = Column(String(255), nullable=True)
    share_token         = Column(String(64), unique=True, nullable=True, index=True)
    permission          = Column(String(10), default="view")  # view | comment | edit
    expires_at          = Column(DateTime(timezone=True), nullable=True)
    created_by          = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at          = Column(DateTime(timezone=True), server_default=func.now())

    document = relationship("Document", back_populates="shares")


class RetentionPolicy(Base):
    __tablename__ = "retention_policies"

    id             = Column(String(36), primary_key=True, default=new_uuid)
    org_id         = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    library_type   = Column(String(30), nullable=False)
    retention_days = Column(Integer, nullable=True)  # None = keep forever
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    updated_at     = Column(DateTime(timezone=True), onupdate=func.now())


# ── Phase 4 — Funding Intelligence & Grant Tracking (PRD §15) ───────────────
# Continuous opportunity monitoring, watchlists, and the pre-award pipeline,
# extending FOARecord above as the opportunity record. See
# engines/funding_intelligence_engine.py (Engine 15) and
# docs/ARCHITECTURE.md §9 for the full design rationale.

class PipelineStageEvent(Base):
    """
    One row per pipeline-stage transition on a FOARecord — the "formalized
    status/stage history table" the PRD calls for, giving portfolio-level
    win-rate/cycle-time reporting without inferring it from a single
    current-stage column.
    """
    __tablename__ = "pipeline_stage_events"

    id           = Column(String(36), primary_key=True, default=new_uuid)
    foa_id       = Column(String(36), ForeignKey("foa_records.id"), nullable=False, index=True)
    from_stage   = Column(String(20), nullable=True)   # null for the initial "identified" event
    to_stage     = Column(String(20), nullable=False)
    changed_by   = Column(String(36), ForeignKey("users.id"), nullable=True)  # null for sync-created records
    notes        = Column(Text, nullable=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())


class Watchlist(Base):
    """
    Saved search criteria for continuous monitoring (PRD §15) — every sync
    checks new/updated FOARecords against each active watchlist and raises
    a Notification (Phase 3's existing table) on a match, rather than a
    simple bookmark list of specific opportunities.
    """
    __tablename__ = "watchlists"

    id                = Column(String(36), primary_key=True, default=new_uuid)
    # Nullable for the same reason as FOARecord.org_id — a personal
    # watchlist has no org workspace to stamp.
    org_id            = Column(String(36), ForeignKey("organizations.id"), nullable=True, index=True)
    owner_id          = Column(String(36), ForeignKey("users.id"), nullable=False)
    name              = Column(String(255), nullable=False)
    keyword           = Column(String(500), nullable=True)
    agencies          = Column(JSON, nullable=True)   # list[str] of agency codes, e.g. ["HHS", "NSF"]
    funding_categories = Column(JSON, nullable=True)  # list[str] of Grants.gov funding category codes
    min_award         = Column(Float, nullable=True)
    max_award         = Column(Float, nullable=True)
    active            = Column(Boolean, default=True, nullable=False)
    last_run_at       = Column(DateTime(timezone=True), nullable=True)
    created_at        = Column(DateTime(timezone=True), server_default=func.now())


# ── Phase 5 — Award & Project Management (Clariva Enterprise™ PRD §16-17) ──────
# A proposal that reaches FOARecord.pipeline_stage == "awarded" (Phase 4) may
# be converted — explicitly, via the engine, never automatically — into an
# Award: the "digital twin" (Phase 2's ScopeOfWork/WorkPackage/Task hierarchy)
# now has an active, funded instance to execute against. Award is 1:1 with a
# Proposal, same nullability pattern as FOARecord/Watchlist: org_id is null
# for a personal/unshared award.

class Award(Base):
    """
    The award record itself — agency-facing identifiers, period of
    performance, and total value. Budget administration, compliance,
    amendments, issues, performance, and closeout all hang off this row.
    """
    __tablename__ = "awards"

    id                              = Column(String(36), primary_key=True, default=new_uuid)
    proposal_id                     = Column(String(36), ForeignKey("proposals.id"), unique=True, nullable=False, index=True)
    foa_id                          = Column(String(36), ForeignKey("foa_records.id"), nullable=True)
    org_id                          = Column(String(36), ForeignKey("organizations.id"), nullable=True, index=True)
    budget_record_id                = Column(String(36), ForeignKey("budget_records.id"), nullable=True)
    award_number                    = Column(String(100), nullable=True)
    funding_agency                  = Column(String(20), nullable=False)
    period_of_performance_start     = Column(DateTime(timezone=True), nullable=True)
    period_of_performance_end       = Column(DateTime(timezone=True), nullable=True)
    total_award_value               = Column(Float, nullable=True)
    terms                           = Column(Text, nullable=True)
    # active | closed | terminated
    status                          = Column(String(20), nullable=False, default="active", index=True)
    # received | active | closed — the "Award Received" pre-activation negotiation
    # stage (Version 3.0 architecture upgrade, PRD addendum: "Three Synchronized
    # Operational Environments"). Deliberately a SEPARATE field from `status`
    # above, not a new value added to it: `status` continues to govern the
    # post-activation lifecycle exactly as it always has, while `award_status`
    # governs only whether a ProjectBaseline has been locked yet via
    # AwardEngine.activate_award(). NOTE the asymmetric defaults between this
    # Python-side default and the SQL migration's column default in
    # migrations.py: every Award created going forward (via AwardEngine.
    # create_award()) starts "received" and requires an explicit "Activate
    # Project" action, but every Award that already existed before this column
    # was added is backfilled to "active" by the migration — those awards were
    # created under the old create_award(), which set status="active"
    # immediately with no negotiation step, so they are already past the
    # Award Received stage and must not be retroactively gated behind a
    # baseline that was never captured for them.
    award_status                    = Column(String(20), nullable=False, default="received", index=True)
    created_by                      = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at                      = Column(DateTime(timezone=True), server_default=func.now())
    updated_at                      = Column(DateTime(timezone=True), onupdate=func.now())


class AwardExpenditure(Base):
    """
    Actual spend recorded against an award's baseline budget (the referenced
    BudgetRecord's total_cost/total_direct) — the input to burn-rate and
    variance tracking (PRD §16). Deliberately NOT a full accounting ledger:
    just enough structure (category + amount + date) to compare cumulative
    actuals against the baseline and the elapsed period of performance.
    """
    __tablename__ = "award_expenditures"

    id            = Column(String(36), primary_key=True, default=new_uuid)
    award_id      = Column(String(36), ForeignKey("awards.id"), nullable=False, index=True)
    category      = Column(String(50), nullable=False)   # personnel | equipment | travel | other_direct | subcontracts | indirect
    description   = Column(Text, nullable=True)
    amount        = Column(Float, nullable=False)
    incurred_date = Column(DateTime(timezone=True), nullable=True)
    recorded_by   = Column(String(36), ForeignKey("users.id"), nullable=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())


class AwardComplianceItem(Base):
    """
    Award terms/conditions & reporting-obligation checklist — the
    analogous-but-distinct counterpart to engines/compliance_engine.py's
    proposal page-limit checks (PRD §16: "reusing the existing Compliance
    Engine pattern" — the rule/violation/checklist shape, not the same
    proposal-specific engine, since award obligations are a different
    domain entirely).
    """
    __tablename__ = "award_compliance_items"

    id            = Column(String(36), primary_key=True, default=new_uuid)
    award_id      = Column(String(36), ForeignKey("awards.id"), nullable=False, index=True)
    obligation    = Column(String(500), nullable=False)
    category      = Column(String(50), nullable=True)    # reporting | financial | regulatory | other
    due_date      = Column(DateTime(timezone=True), nullable=True)
    # pending | complete | overdue | waived
    status        = Column(String(20), nullable=False, default="pending")
    notes         = Column(Text, nullable=True)
    completed_by  = Column(String(36), ForeignKey("users.id"), nullable=True)
    completed_at  = Column(DateTime(timezone=True), nullable=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())


class AwardAmendment(Base):
    """
    Structured change request (scope, budget, or period-of-performance) on
    an active award (PRD §16), routed through Phase 3's generic
    ApprovalRequest workflow rather than a parallel approval system —
    `approval_request_id` links to that row, and this table's `status` is
    kept in sync with it by the engine whenever the approval is decided.
    `effective_changes` is applied to the Award record only once approved.
    """
    __tablename__ = "award_amendments"

    id                  = Column(String(36), primary_key=True, default=new_uuid)
    award_id            = Column(String(36), ForeignKey("awards.id"), nullable=False, index=True)
    # scope | budget | period_of_performance | other
    amendment_type      = Column(String(30), nullable=False)
    description         = Column(Text, nullable=False)
    # e.g. {"total_award_value": 250000, "period_of_performance_end": "2028-01-01"}
    effective_changes   = Column(JSON, nullable=True)
    requested_by        = Column(String(36), ForeignKey("users.id"), nullable=False)
    approval_request_id = Column(String(36), ForeignKey("approval_requests.id"), nullable=True)
    # pending | approved | rejected
    status              = Column(String(20), nullable=False, default="pending")
    created_at          = Column(DateTime(timezone=True), server_default=func.now())
    decided_at          = Column(DateTime(timezone=True), nullable=True)


class ProjectIssue(Base):
    """
    Issue/risk tracking during project execution (PRD §17). Optionally tied
    to a specific WorkPackage; feeds the Smart Dependency Engine indirectly —
    resolving an issue that required a scope/budget change is expected to go
    through an AwardAmendment, which is what actually marks ProjectKnowledge
    stale, not the issue itself.
    """
    __tablename__ = "project_issues"

    id              = Column(String(36), primary_key=True, default=new_uuid)
    award_id        = Column(String(36), ForeignKey("awards.id"), nullable=False, index=True)
    work_package_id = Column(String(36), ForeignKey("work_packages.id"), nullable=True)
    title           = Column(String(500), nullable=False)
    description     = Column(Text, nullable=True)
    # low | medium | high | critical
    severity        = Column(String(20), nullable=False, default="medium")
    # open | resolved
    status          = Column(String(20), nullable=False, default="open")
    raised_by       = Column(String(36), ForeignKey("users.id"), nullable=True)
    resolved_at     = Column(DateTime(timezone=True), nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())


class AwardPerformanceRecord(Base):
    """
    Actual measurement against a KPI target — extends the KPI *target*
    already captured at ProjectKnowledge.kpis (Phase 2: [{name, target,
    unit}]) with tracked *actuals* over time, project-level performance
    management (PRD §17) built as a natural continuation of that existing
    field rather than a duplicate target-setting mechanism.
    """
    __tablename__ = "award_performance_records"

    id            = Column(String(36), primary_key=True, default=new_uuid)
    award_id      = Column(String(36), ForeignKey("awards.id"), nullable=False, index=True)
    kpi_name      = Column(String(255), nullable=False)
    target        = Column(Float, nullable=True)
    actual_value  = Column(Float, nullable=True)
    unit          = Column(String(50), nullable=True)
    period_label  = Column(String(100), nullable=True)   # e.g. "Q1 2027"
    notes         = Column(Text, nullable=True)
    recorded_by   = Column(String(36), ForeignKey("users.id"), nullable=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())


class AwardCloseout(Base):
    """
    Closeout record (PRD §17): deliverables reconciliation, equipment
    disposition, and structured knowledge capture. Closing an award writes
    a MemoryRecord (the existing Organizational Memory table, Phase 0) so
    lessons learned feed the next pursuit's KPI dashboard and semantic
    search exactly like a proposal outcome does today.
    """
    __tablename__ = "award_closeouts"

    id                        = Column(String(36), primary_key=True, default=new_uuid)
    award_id                  = Column(String(36), ForeignKey("awards.id"), unique=True, nullable=False)
    deliverables_reconciled   = Column(Boolean, default=False, nullable=False)
    deliverables_notes        = Column(Text, nullable=True)
    equipment_disposition     = Column(Text, nullable=True)
    final_report_submitted    = Column(Boolean, default=False, nullable=False)
    lessons_learned           = Column(JSON, default=list)
    memory_record_id          = Column(String(36), ForeignKey("memory_records.id"), nullable=True)
    closed_by                 = Column(String(36), ForeignKey("users.id"), nullable=True)
    closed_at                 = Column(DateTime(timezone=True), nullable=True)
    created_at                = Column(DateTime(timezone=True), server_default=func.now())


# ── Phase 7 — Award Received: Data Model Foundation (Version 3.0 architecture ──
# upgrade — "Three Synchronized Operational Environments": Pre-Award, Award
# Received, Post-Award). See docs/Clariva_Enterprise_v3_Roadmap.docx §3-4 for
# the full audit this closes: today a proposal moves directly from
# "submitted" to a live, editable Award (Phase 5 above) with no negotiation
# step, no sponsor-condition tracking, and no immutable record of what was
# originally approved. The two tables below give the Award Received stage
# its own real backend state, ahead of any UI work (Phases 8-14).

class ProjectBaseline(Base):
    """
    An immutable, versioned snapshot of everything a Proposed Project
    (Phase 2's ScopeOfWork/BudgetRecord "digital twin") looked like at the
    moment it became an Approved Project Baseline — the one object this
    codebase did not have before Phase 7, per the Version 3.0 brief's core
    finding. Created exactly once per award by AwardEngine.activate_award()
    (the "Activate Project" action, deliberately a separate, later step than
    Award creation — see Award.award_status's docstring), and again by
    AwardEngine.create_baseline_version() whenever an approved AwardAmendment
    changes budget, scope, or schedule.

    Rows are never edited after creation — "what was originally approved"
    (or most recently re-baselined) must always be reconstructable, which is
    the whole point: Phase 5's BudgetRecord/ScopeOfWork/WorkPackage/Task rows
    stay live and mutable for day-to-day project administration, while this
    table is what a future Planned-vs-Actual engine (Phase 10) diffs actual
    execution against. Snapshot content is denormalized JSON, not FK rows
    into the live tables, specifically so it survives edits/deletes to the
    live ScopeOfWork/BudgetRecord without cascading.
    """
    __tablename__ = "project_baselines"

    id                     = Column(String(36), primary_key=True, default=new_uuid)
    award_id               = Column(String(36), ForeignKey("awards.id"), nullable=False, index=True)
    version                = Column(Integer, nullable=False, default=1)
    # Exactly one row per award should have is_current=True — the baseline a
    # future variance engine (Phase 10) compares live execution against.
    # Enforced in the engine (AwardEngine.create_baseline_version flips the
    # previous current row to False in the same transaction), not a DB
    # constraint, matching this codebase's is_current-flag precedent
    # (ProjectBaseline is the first use of the pattern, but see how
    # OrgMembership/CreditAllocation etc. also rely on engine-enforced
    # invariants rather than DB-level ones).
    is_current             = Column(Boolean, default=True, nullable=False)
    total_award_value      = Column(Float, nullable=True)
    period_of_performance_start = Column(DateTime(timezone=True), nullable=True)
    period_of_performance_end   = Column(DateTime(timezone=True), nullable=True)
    # Denormalized snapshot of the BudgetRecord this award references at
    # baseline time — same shape as BudgetRecord's own JSON columns
    # (personnel/consultants/equipment/travel/other_direct/subcontracts) plus
    # its cached totals, so a variance engine never needs to touch the live
    # BudgetRecord row to know what was approved.
    budget_snapshot        = Column(JSON, default=dict)
    # Denormalized snapshot of the ScopeOfWork hierarchy: {work_packages: [...],
    # milestones: [...], deliverables: [...]}, each item flattened to plain
    # dicts (not FK rows) for the same survive-live-edits reason as above.
    scope_snapshot         = Column(JSON, default=dict)
    notes                  = Column(Text, nullable=True)
    created_by             = Column(String(36), ForeignKey("users.id"), nullable=True)
    created_at             = Column(DateTime(timezone=True), server_default=func.now())


class AwardCondition(Base):
    """
    A sponsor-imposed condition surfaced during Award Received review —
    e.g. "submit revised budget justification before first drawdown" or
    "withhold 10% pending IRB approval." Distinct from the existing
    AwardComplianceItem (Phase 5): that table is the post-activation,
    ongoing reporting/compliance calendar; this table is specifically the
    negotiation-stage review captured before a project is activated, which
    had no row to live in before Phase 7 (the brief's Award Received Core
    Activities explicitly list "review sponsor conditions" as an input).
    A condition may optionally be carried forward into an AwardComplianceItem
    by the user once the project is activated, but Phase 7 does not do this
    automatically — see docs/ARCHITECTURE.md for why (kept as a deliberate,
    later UI decision rather than an automatic data-model coupling).
    """
    __tablename__ = "award_conditions"

    id            = Column(String(36), primary_key=True, default=new_uuid)
    award_id      = Column(String(36), ForeignKey("awards.id"), nullable=False, index=True)
    description   = Column(Text, nullable=False)
    # reporting | financial | regulatory | programmatic | other
    category      = Column(String(50), nullable=True)
    due_date      = Column(DateTime(timezone=True), nullable=True)
    # open | resolved | waived
    status        = Column(String(20), nullable=False, default="open")
    resolved_by   = Column(String(36), ForeignKey("users.id"), nullable=True)
    resolved_at   = Column(DateTime(timezone=True), nullable=True)
    created_by    = Column(String(36), ForeignKey("users.id"), nullable=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())


# ── Phase 6 — Integrations & Marketplace (Clariva Enterprise™ PRD §19-20) ──────
# One uniform `ConnectorConnection` row represents any external integration —
# a generic outbound webhook, a Slack/Teams incoming-webhook URL, or a
# placeholder for an OAuth-based service (Microsoft 365, Google Workspace,
# Salesforce, HubSpot, DocuSign, Adobe Sign, financial ERPs) this environment
# has no real credentials for. See engines/connector_engine.py's
# CONNECTOR_TYPES registry for which types actually dispatch vs. degrade to
# "not configured" — the same graceful-degradation pattern SAM.gov
# established in Phase 4. This table IS the "webhook engine for outbound
# events" the PRD's platform-surface bullet calls for; there is no separate
# webhook-subscription table.

class ConnectorConnection(Base):
    __tablename__ = "connector_connections"

    id             = Column(String(36), primary_key=True, default=new_uuid)
    org_id         = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    # webhook | slack | teams | ms365 | google_workspace | salesforce | hubspot | docusign | adobe_sign | financial_erp
    connector_type = Column(String(30), nullable=False)
    name           = Column(String(255), nullable=False)
    # Type-specific settings — {"target_url", "secret"} for webhook,
    # {"webhook_url"} for slack/teams. OAuth-based types store nothing
    # meaningful here yet (no real credential flow to store).
    config         = Column(JSON, default=dict, nullable=False)
    # Which audit-log `action` strings (see audit.py::log_action) this
    # connector fires on. Empty/null list = all events.
    event_types    = Column(JSON, nullable=True)
    active         = Column(Boolean, default=True, nullable=False)
    last_tested_at = Column(DateTime(timezone=True), nullable=True)
    last_error     = Column(Text, nullable=True)
    created_by     = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    updated_at     = Column(DateTime(timezone=True), onupdate=func.now())


class ConnectorEventLog(Base):
    """Dispatch history — one row per attempted delivery, for observability
    and debugging (was this webhook actually fired? did it succeed?)."""
    __tablename__ = "connector_event_logs"

    id            = Column(String(36), primary_key=True, default=new_uuid)
    connector_id  = Column(String(36), ForeignKey("connector_connections.id"), nullable=False, index=True)
    event_type    = Column(String(100), nullable=False)
    payload       = Column(JSON, nullable=True)
    success       = Column(Boolean, nullable=False, default=False)
    status_code   = Column(Integer, nullable=True)
    error         = Column(Text, nullable=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())


class ApiKey(Base):
    """
    Public API credential (PRD §19: "a versioned public API... enabling a
    partner/marketplace ecosystem"). Keys are org- and role-scoped — a key
    created with role="viewer" gets exactly that role's rbac.py permissions
    on the public API surface, never more than the issuing owner's own
    access. Only `key_hash` (SHA-256, deterministic so it can be looked up
    by equality) is ever stored; the plaintext key is shown to the user
    exactly once, at creation.
    """
    __tablename__ = "api_keys"

    id           = Column(String(36), primary_key=True, default=new_uuid)
    org_id       = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    name         = Column(String(255), nullable=False)
    key_prefix   = Column(String(12), nullable=False)   # shown alongside the name so a user can tell keys apart
    key_hash     = Column(String(64), nullable=False, unique=True, index=True)
    role         = Column(String(20), nullable=False, default="viewer")  # owner | editor | viewer — capped at creator's own role
    created_by   = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at   = Column(DateTime(timezone=True), nullable=True)


class MarketplaceListing(Base):
    """
    Listing groundwork only (PRD §20: Marketplace is explicitly framed as
    "a future revenue channel") — browsable listings with no payment
    processing or install flow yet, same scoping discipline as SSO's
    schema-only Phase 1 treatment.
    """
    __tablename__ = "marketplace_listings"

    id             = Column(String(36), primary_key=True, default=new_uuid)
    # Null = a platform-provided listing rather than a third-party vendor's.
    vendor_org_id  = Column(String(36), ForeignKey("organizations.id"), nullable=True, index=True)
    # template_pack | connector | ai_capability
    listing_type   = Column(String(30), nullable=False)
    name           = Column(String(255), nullable=False)
    description    = Column(Text, nullable=True)
    price_cents    = Column(Integer, nullable=True)   # null = free/contact-for-pricing
    currency       = Column(String(10), nullable=False, default="usd")
    # draft | published | archived
    status         = Column(String(20), nullable=False, default="draft")
    created_by     = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    updated_at     = Column(DateTime(timezone=True), onupdate=func.now())
