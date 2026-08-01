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
    industry              = Column(String(255))
    core_technologies     = Column(JSON, default=list)
    prior_sbir_experience = Column(Boolean, default=False)
    uei_number            = Column(String(20), nullable=True)
    cage_code             = Column(String(10), nullable=True)
    company_capabilities  = Column(Text, nullable=True)   # narrative of core competencies

    # ── Principal Investigator ────────────────────────────────────────────────
    pi_name               = Column(String(255), nullable=True)
    pi_credentials        = Column(Text, nullable=True)   # degrees, certifications
    pi_orcid              = Column(String(25), nullable=True)
    pi_degree             = Column(String(100), nullable=True)  # PhD, MD, etc.
    pi_affiliation        = Column(String(255), nullable=True)  # primary employer
    pi_publications       = Column(Integer, nullable=True)
    pi_prior_sbir_awards  = Column(Integer, nullable=True)

    # ── Key Personnel / Team ──────────────────────────────────────────────────
    # [{name, title, role, credentials, effort_pct, years_exp, orcid}]
    team_members          = Column(JSON, default=list)

    # ── Facilities & Equipment ────────────────────────────────────────────────
    # [{name, type, description, certifications, sq_footage, location}]
    facilities            = Column(JSON, default=list)

    # ── Partners / Collaborators ──────────────────────────────────────────────
    # [{name, type (subcontractor|consultant|research_institution|industry),
    #   role, pi_name, location, effort_pct, institution_type}]
    partners              = Column(JSON, default=list)

    # ── Past Performance ──────────────────────────────────────────────────────
    # [{title, agency, award_number, amount, period, outcome, relevance}]
    past_performance      = Column(JSON, default=list)

    created_at            = Column(DateTime(timezone=True), server_default=func.now())
    updated_at            = Column(DateTime(timezone=True), onupdate=func.now())

    user = relationship("User", back_populates="org_context")


class FOARecord(Base):
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


class OrgProposal(Base):
    __tablename__ = "org_proposals"

    id          = Column(String(36), primary_key=True, default=new_uuid)
    org_id      = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    proposal_id = Column(String(36), ForeignKey("proposals.id"), nullable=False)
    shared_by   = Column(String(36), ForeignKey("users.id"), nullable=False)
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
