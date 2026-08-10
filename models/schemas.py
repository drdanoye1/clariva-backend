"""
Clariva Intelligent Grant Writing Platform — Pydantic Schemas
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# ── Enums ─────────────────────────────────────────────────────────────────────

class Agency(str, Enum):
    # SBIR / federal R&D agencies
    NSF    = "NSF"
    DOE    = "DOE"
    NIH    = "NIH"
    DOD    = "DOD"
    DARPA  = "DARPA"
    ARPA_E = "ARPA-E"
    NASA   = "NASA"
    # Non-SBIR federal agencies (community, health, housing, etc.)
    HUD    = "HUD"
    HHS    = "HHS"
    EPA    = "EPA"
    DOL    = "DOL"
    USDA   = "USDA"
    SBA    = "SBA"
    EDA    = "EDA"
    DOED   = "DOEd"
    VA     = "VA"
    DHS    = "DHS"
    DOC    = "DOC"
    DOT    = "DOT"
    HRSA   = "HRSA"
    AMERICORPS = "AmeriCorps"
    NEA    = "NEA"
    NEH    = "NEH"
    IMLS   = "IMLS"
    OTHER  = "OTHER"

class Phase(str, Enum):
    PRE_PHASE_I = "pre_phase_i"
    PHASE_I     = "phase_i"
    PHASE_II    = "phase_ii"
    FAST_TRACK  = "fast_track"

class GrantType(str, Enum):
    SBIR         = "sbir"
    STTR         = "sttr"
    NIH_R01      = "nih_r01"
    NIH_R21      = "nih_r21"
    NSF_STANDARD = "nsf_standard"
    FEDERAL_OTHER = "federal_other"
    STATE_GRANT  = "state_grant"
    FOUNDATION   = "foundation"
    CORPORATE    = "corporate"

class ProposalStatus(str, Enum):
    DRAFT      = "draft"
    IN_REVIEW  = "in_review"
    OPTIMIZING = "optimizing"
    READY      = "ready"
    APPROVED   = "approved"
    SUBMITTED  = "submitted"

class DocumentFormat(str, Enum):
    TXT  = "txt"
    DOCX = "docx"
    PDF  = "pdf"


# ── Auth ──────────────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str
    organization: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class LoginResponse(BaseModel):
    """
    Superset of TokenResponse used by POST /auth/login. Non-MFA users get
    exactly the same access_token/refresh_token/token_type shape as before
    (mfa_required defaults to False) — existing frontend code that reads
    `res.data.access_token` is unaffected. A user with MFA enabled instead
    gets mfa_required=True and a short-lived mfa_token to complete the
    second step via POST /auth/mfa/login.
    """
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    mfa_required: bool = False
    mfa_token: Optional[str] = None

class MFASetupResponse(BaseModel):
    secret: str
    otpauth_uri: str
    qr_code_png_base64: str

class MFAVerifyRequest(BaseModel):
    code: str

class MFADisableRequest(BaseModel):
    password: str
    code: Optional[str] = None

class MFALoginRequest(BaseModel):
    mfa_token: str
    code: str

class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: EmailStr
    full_name: str
    organization: str
    mfa_enabled: bool = False
    created_at: datetime
    # Phase 3.1 — Configurable Pricing Controls: the frontend needs this to
    # decide whether to show the admin nav link / page at all. The real
    # security boundary is still server-side (require_superadmin on every
    # admin.py / service_catalog.py admin endpoint) — this is UX-only.
    is_superadmin: bool = False


# ── Company Profile sub-objects ────────────────────────────────────────────────

class TeamMember(BaseModel):
    name: str
    title: str = ""
    role: str = ""             # e.g. "Co-PI", "Senior Researcher", "Consultant"
    credentials: str = ""      # degrees, certifications
    effort_pct: float = 0.0   # % effort on this proposal
    years_exp: int = 0
    orcid: Optional[str] = None

class Facility(BaseModel):
    name: str
    type: str = ""            # e.g. "Laboratory", "Cleanroom", "Manufacturing"
    description: str = ""
    certifications: List[str] = []
    sq_footage: Optional[int] = None
    location: str = ""

class Partner(BaseModel):
    name: str
    type: str = ""            # subcontractor | consultant | research_institution | industry
    role: str = ""
    pi_name: str = ""
    location: str = ""
    effort_pct: float = 0.0
    institution_type: str = ""  # university | national_lab | hospital | company

class PastPerformance(BaseModel):
    title: str
    agency: str = ""
    award_number: str = ""
    amount: str = ""           # "$350,000"
    period: str = ""           # "2021–2023"
    outcome: str = ""          # funded | completed | ongoing | not_funded
    relevance: str = ""        # narrative of relevance to current proposal


# ── Organization Context / Company Profile ─────────────────────────────────────

class OrgContext(BaseModel):
    """Full company profile used for proposal generation and gap detection."""

    # Overview
    organization_name: str = ""
    industry: str = ""
    core_technologies: List[str] = []
    prior_sbir_experience: bool = False
    uei_number: Optional[str] = None
    cage_code: Optional[str] = None
    company_capabilities: Optional[str] = None

    # PI
    pi_name: Optional[str] = None
    pi_credentials: Optional[str] = None
    pi_orcid: Optional[str] = None
    pi_degree: Optional[str] = None
    pi_affiliation: Optional[str] = None
    pi_publications: Optional[int] = None
    pi_prior_sbir_awards: Optional[int] = None

    # Arrays
    team_members: List[TeamMember] = []
    facilities: List[Facility] = []
    partners: List[Partner] = []
    past_performance: List[PastPerformance] = []


# ── FOA ───────────────────────────────────────────────────────────────────────

class FOASection(BaseModel):
    section_id: str
    title: str
    required: bool
    page_limit: Optional[int] = None
    guidance: str
    evaluation_weight: float = Field(ge=0, le=1)

class FOATemplate(BaseModel):
    foa_id: str
    agency: Agency
    program_title: str
    solicitation_number: Optional[str] = None
    phase: Phase
    total_page_limit: Optional[int] = None
    ordered_sections: List[FOASection]
    compliance_rules: List[str]
    deadline: Optional[datetime] = None
    weights: Dict[str, float]
    # Version 3.0 architecture upgrade, Phase 16 (cont'd) — plain-language
    # summary and eligibility text, generated in the same GPT-4o parse call
    # that produces ordered_sections/weights above, so a user can decide
    # whether an opportunity is worth pursuing without reading the full
    # solicitation first. Optional/nullable so older parsed_template blobs
    # (persisted before this field existed) still deserialize cleanly.
    summary: Optional[str] = None
    eligibility_summary: Optional[str] = None

class FOAUploadResponse(BaseModel):
    foa_id: str
    parsed: bool
    template: FOATemplate
    raw_text_preview: str


# ── Proposal ──────────────────────────────────────────────────────────────────

class SectionContent(BaseModel):
    section_id: str
    title: str
    content: str
    word_count: int
    page_estimate: float
    compliance_flags: list = []

class ProposalCreate(BaseModel):
    title: str
    agency: str = "OTHER"          # accepts any agency string (HUD, HHS, NSF, etc.)
    phase: Phase = Phase.PHASE_I
    grant_type: GrantType = GrantType.FEDERAL_OTHER
    foa_id: Optional[str] = None
    org_context: OrgContext
    research_focus: str
    innovation_description: str
    commercialization_plan: Optional[str] = None
    team_description: Optional[str] = None
    # Taxonomy ground truth from grant wizard Steps 1/2/3
    beneficiary_type: Optional[str] = None   # e.g. "Non-Profit Organization"
    funder_class:     Optional[str] = None   # e.g. "Federal Agencies"
    program_label:    Optional[str] = None   # e.g. "Community Development Block Grant (CDBG)"
    program_size:     Optional[str] = None   # e.g. "$250K–$5M"
    grantor_name:     Optional[str] = None   # for non-federal funder name

class ProposalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    proposal_id: str
    title: str
    agency: str
    phase: Phase
    grant_type: str = "federal_other"
    status: ProposalStatus
    sections: List[SectionContent]
    created_at: datetime
    updated_at: datetime
    version: int
    # Taxonomy metadata (optional — may be absent on older proposals)
    beneficiary_type: Optional[str] = None
    funder_class:     Optional[str] = None
    program_label:    Optional[str] = None
    program_size:     Optional[str] = None
    grantor_name:     Optional[str] = None

class SectionGenerateRequest(BaseModel):
    proposal_id: str
    section_id: str
    additional_context: Optional[str] = None
    regenerate: bool = False


# ── Scoring ───────────────────────────────────────────────────────────────────

class SectionScore(BaseModel):
    section_id: str
    section_title: str
    score: float = Field(ge=0, le=10)
    weight: float
    weighted_score: float
    strengths: List[str]
    weaknesses: List[str]
    recommendations: List[str]

class RiskPenalty(BaseModel):
    category: str
    description: str
    penalty: float

class ScoringResult(BaseModel):
    proposal_id: str
    total_score: float = Field(ge=0, le=100)
    section_scores: List[SectionScore]
    risk_penalties: List[RiskPenalty]
    compliance_score: float
    technical_merit: float
    commercialization: float
    innovation: float
    team_score: float
    scored_at: datetime


# ── Reviewer ──────────────────────────────────────────────────────────────────

class ReviewerType(str, Enum):
    NSF_PANELIST  = "nsf_panelist"
    NIH_REVIEWER  = "nih_reviewer"
    DOD_EVALUATOR = "dod_evaluator"
    DARPA_PM      = "darpa_pm"
    GENERIC       = "generic"

class ReviewerSimulation(BaseModel):
    proposal_id: str
    reviewer_type: ReviewerType
    overall_impression: str
    strengths: List[str]
    weaknesses: List[str]
    questions_for_applicant: List[str]
    decision: str
    confidence: float = Field(ge=0, le=1)
    simulated_at: datetime


# ── Compliance ────────────────────────────────────────────────────────────────

class ComplianceViolation(BaseModel):
    rule: str
    severity: str
    section_id: Optional[str]
    detail: str

class ComplianceReport(BaseModel):
    proposal_id: str
    passed: bool
    violations: List[ComplianceViolation]
    page_counts: Dict[str, float]
    total_pages: float
    checked_at: datetime


# ── Document Export ───────────────────────────────────────────────────────────

class ExportFormatOptions(BaseModel):
    """User-selectable formatting options at export time.  All default to the Federal Proposal Standard."""
    font: str = "Times New Roman"
    font_pt: int = 12                 # 12 or 11
    alignment: str = "left"           # "left" | "justify"
    margins_in: float = 1.0
    page_num_position: str = "center" # "center" | "right"
    cover_page_number: bool = False
    page_break_h1: bool = False
    section_numbering: bool = False
    space_after_pt: int = 6
    space_before_h1_pt: int = 12

class ExportRequest(BaseModel):
    proposal_id: str
    format: DocumentFormat
    include_scoring_summary: bool = True
    include_reviewer_feedback: bool = False
    include_compliance_status: bool = True
    generate_figures: bool = False
    format_options: Optional[ExportFormatOptions] = None

class ExportResponse(BaseModel):
    proposal_id: str
    format: DocumentFormat
    download_url: str
    file_size_bytes: int
    exported_at: datetime


# ── Stored Files (Version 3.0 upgrade, "Real File Storage" Phase C) ────────────
# Read-only view over models.db_models.StoredFile for the two "browse what's
# in R2" list endpoints (GET /awards/{id}/files, GET /documents/exports/
# {proposal_id}) — see those routers for the access-control checks, which
# differ per object_type and are NOT done generically here.

class StoredFileOut(BaseModel):
    id: str
    original_filename: str
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    created_at: datetime
    download_url: str


# ── Memory / KPI ──────────────────────────────────────────────────────────────

class MemoryEntry(BaseModel):
    entry_id: str
    org_id: str
    proposal_id: str
    agency: Agency
    outcome: Optional[str] = None
    score: Optional[float] = None
    lessons_learned: List[str] = []
    created_at: datetime

class KPIDashboard(BaseModel):
    org_id: str
    total_proposals: int
    funded: int
    success_rate: float
    avg_score: float
    proposals_by_agency: Dict[str, int]
    score_trend: List[Dict[str, Any]]
    top_weaknesses: List[str]


# ── Shared AI Credits (Clariva Enterprise™ PRD §13) ────────────────────────────

class CreditBalanceOut(BaseModel):
    org_id: str
    balance: float
    # Low-balance warning addendum: reference_balance is the "100%" mark
    # (the level of the last top-up); pct_remaining/low_balance are derived
    # server-side so the frontend never has to duplicate that math.
    reference_balance: float = 100.0
    pct_remaining: float = 100.0
    low_balance: bool = False
    updated_at: Optional[datetime] = None

class CreditTransactionOut(BaseModel):
    id: str
    user_id: Optional[str] = None
    amount: float
    reason: str
    balance_after: float
    created_at: datetime

class CreditTopupRequest(BaseModel):
    amount: float = Field(gt=0)
    reason: str = "manual_topup"

class CreditAllocationRequest(BaseModel):
    # Exactly one of these should be set — all None targets the org-wide
    # default cap. See CreditAllocation's docstring in db_models.py.
    user_id: Optional[str] = None
    team_id: Optional[str] = None
    department_id: Optional[str] = None
    cap: Optional[float] = Field(default=None, ge=0)  # None = unlimited
    period: str = "monthly"         # "monthly" | "total"

class CreditAllocationOut(BaseModel):
    id: str
    user_id: Optional[str] = None
    team_id: Optional[str] = None
    department_id: Optional[str] = None
    cap: Optional[float] = None
    period: str


# ── Scope of Work Engine & Project Knowledge Base (Clariva Enterprise™ PRD §9-10) ──
# See docs/ARCHITECTURE.md §7. All *Out schemas are built from plain, already
# -loaded ORM rows (never via relationship traversal) to avoid the async
# lazy-load pitfall documented there — every list below is assembled with its
# own explicit query in routers/scope_of_work.py, not `.model_validate()`
# over a relationship attribute.

class RiskItem(BaseModel):
    risk: str
    mitigation: str = ""
    likelihood: str = ""   # low | medium | high
    impact: str = ""       # low | medium | high

class KPIItem(BaseModel):
    name: str
    target: str = ""
    unit: str = ""

class ProjectKnowledgeUpdate(BaseModel):
    objectives: Optional[str] = None
    need_statement: Optional[str] = None
    evaluation_plan: Optional[str] = None
    risks: Optional[List[RiskItem]] = None
    outputs: Optional[str] = None
    outcomes: Optional[str] = None
    kpis: Optional[List[KPIItem]] = None

class ProjectKnowledgeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    proposal_id: str
    objectives: Optional[str] = None
    need_statement: Optional[str] = None
    evaluation_plan: Optional[str] = None
    risks: List[Dict[str, Any]] = []
    outputs: Optional[str] = None
    outcomes: Optional[str] = None
    kpis: List[Dict[str, Any]] = []
    stale_flags: Dict[str, Any] = {}
    created_at: datetime
    updated_at: Optional[datetime] = None


class ScopeOfWorkUpdate(BaseModel):
    period_of_performance_months: Optional[int] = Field(default=None, ge=1)
    methodology_narrative: Optional[str] = None
    logic_model: Optional[Dict[str, Any]] = None
    reporting_schedule: Optional[List[Dict[str, Any]]] = None

class ScopeOfWorkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_knowledge_id: str
    period_of_performance_months: Optional[int] = None
    methodology_narrative: Optional[str] = None
    logic_model: Dict[str, Any] = {}
    reporting_schedule: List[Dict[str, Any]] = []
    created_at: datetime
    updated_at: Optional[datetime] = None


class WorkPackageCreate(BaseModel):
    name: str
    description: Optional[str] = None
    lead: Optional[str] = None
    start_month: Optional[int] = None
    end_month: Optional[int] = None
    estimated_cost: Optional[float] = Field(default=None, ge=0)

class WorkPackageUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    lead: Optional[str] = None
    start_month: Optional[int] = None
    end_month: Optional[int] = None
    estimated_cost: Optional[float] = Field(default=None, ge=0)
    order_index: Optional[int] = None

class WorkPackageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    scope_of_work_id: str
    name: str
    description: Optional[str] = None
    lead: Optional[str] = None
    start_month: Optional[int] = None
    end_month: Optional[int] = None
    estimated_cost: Optional[float] = None
    order_index: int = 0


class TaskCreate(BaseModel):
    name: str
    description: Optional[str] = None
    owner: Optional[str] = None
    start_month: Optional[int] = None
    end_month: Optional[int] = None
    status: str = "not_started"

class TaskUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    owner: Optional[str] = None
    start_month: Optional[int] = None
    end_month: Optional[int] = None
    status: Optional[str] = None
    order_index: Optional[int] = None

class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    work_package_id: str
    name: str
    description: Optional[str] = None
    owner: Optional[str] = None
    start_month: Optional[int] = None
    end_month: Optional[int] = None
    status: str
    order_index: int = 0


class MilestoneCreate(BaseModel):
    name: str
    description: Optional[str] = None
    due_month: Optional[int] = None
    work_package_id: Optional[str] = None
    status: str = "pending"

class MilestoneUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    due_month: Optional[int] = None
    status: Optional[str] = None

class MilestoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    scope_of_work_id: str
    work_package_id: Optional[str] = None
    name: str
    description: Optional[str] = None
    due_month: Optional[int] = None
    status: str


class DeliverableCreate(BaseModel):
    name: str
    description: Optional[str] = None
    due_month: Optional[int] = None
    work_package_id: Optional[str] = None
    deliverable_type: Optional[str] = None
    status: str = "pending"

class DeliverableUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    due_month: Optional[int] = None
    deliverable_type: Optional[str] = None
    status: Optional[str] = None

class DeliverableOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    scope_of_work_id: str
    work_package_id: Optional[str] = None
    name: str
    description: Optional[str] = None
    due_month: Optional[int] = None
    deliverable_type: Optional[str] = None
    status: str


class ScopeOfWorkFull(BaseModel):
    """Everything needed to render the Scope of Work tab in one call — flat
    lists (not nested), grouped client-side by scope_of_work_id/work_package_id."""
    project_knowledge: ProjectKnowledgeOut
    scope_of_work: ScopeOfWorkOut
    work_packages: List[WorkPackageOut] = []
    tasks: List[TaskOut] = []
    milestones: List[MilestoneOut] = []
    deliverables: List[DeliverableOut] = []


class WorkBreakdownGenerateRequest(BaseModel):
    additional_context: Optional[str] = None

class GeneratedWorkPackage(BaseModel):
    name: str
    description: str = ""
    start_month: Optional[int] = None
    end_month: Optional[int] = None
    tasks: List[str] = []          # task names, created as child Task rows
    milestones: List[str] = []     # milestone names, created against this work package
    deliverables: List[str] = []   # deliverable names, created against this work package

class WorkBreakdownGenerateOut(BaseModel):
    """Result of an AI-suggested work breakdown — already persisted; returned
    so the frontend can render what was created without a second fetch."""
    work_packages: List[WorkPackageOut] = []
    tasks: List[TaskOut] = []
    milestones: List[MilestoneOut] = []
    deliverables: List[DeliverableOut] = []

class MethodologyGenerateRequest(BaseModel):
    additional_context: Optional[str] = None

class MethodologyGenerateOut(BaseModel):
    methodology_narrative: str

class EvaluationPlanGenerateOut(BaseModel):
    evaluation_plan: str

class BudgetSyncOut(BaseModel):
    proposal_id: str
    total_direct: float
    total_indirect: float
    total_cost: float
    synced_work_packages: int


class DeriveFromProposalOut(BaseModel):
    """Draft values extracted from the proposal's already-generated section
    content — returned for review, not auto-saved (same "draft into the
    field, Save is separate" contract as every other AI action here)."""
    objectives: Optional[str] = None
    need_statement: Optional[str] = None
    outputs: Optional[str] = None
    outcomes: Optional[str] = None
    evaluation_plan: Optional[str] = None
    methodology_narrative: Optional[str] = None
    source_sections_used: List[str] = []


class FieldSuggestRequest(BaseModel):
    field: str  # one of: objectives, need_statement, outputs, outcomes
    current_value: Optional[str] = None
    additional_context: Optional[str] = None


class FieldSuggestOut(BaseModel):
    field: str
    suggestion: str


# ── Phase 3 — Collaboration & Content Management (Clariva Enterprise™ PRD §11, §14) ──
# See docs/ARCHITECTURE.md §8. Composite *Out schemas that join in another
# table's data (author name, actor name, email) are built manually in
# routers/collaboration.py and routers/documents_library.py, never via
# `.model_validate()` on a bare ORM row — same convention as AuditLogOut in
# routers/organizations.py.

class DepartmentCreate(BaseModel):
    name: str

class DepartmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    org_id: str
    name: str
    created_at: datetime

class TeamCreate(BaseModel):
    name: str
    department_id: Optional[str] = None

class TeamUpdate(BaseModel):
    name: Optional[str] = None
    department_id: Optional[str] = None

class TeamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    org_id: str
    department_id: Optional[str] = None
    name: str
    created_at: datetime

class TeamMemberOut(BaseModel):
    id: str
    team_id: str
    user_id: str
    email: str
    full_name: str
    role: str

class WorkspaceTaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    proposal_id: Optional[str] = None
    assignee_id: Optional[str] = None
    due_date: Optional[datetime] = None

class WorkspaceTaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    assignee_id: Optional[str] = None
    status: Optional[str] = None
    due_date: Optional[datetime] = None

class WorkspaceTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    org_id: Optional[str] = None
    proposal_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    assignee_id: Optional[str] = None
    status: str
    due_date: Optional[datetime] = None
    created_by: str
    created_at: datetime

class CommentCreate(BaseModel):
    content: str
    parent_comment_id: Optional[str] = None

class CommentUpdate(BaseModel):
    content: str

class CommentOut(BaseModel):
    id: str
    object_type: str
    object_id: str
    parent_comment_id: Optional[str] = None
    author_id: str
    author_name: str
    content: str
    mentions: List[str] = []
    edited: bool = False
    created_at: datetime

class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    type: str
    message: str
    object_type: Optional[str] = None
    object_id: Optional[str] = None
    read: bool
    created_at: datetime

class ApprovalRequestCreate(BaseModel):
    object_type: str
    object_id: str
    approver_id: Optional[str] = None
    notes: Optional[str] = None

class ApprovalDecisionRequest(BaseModel):
    approved: bool
    decision_notes: Optional[str] = None

class ApprovalRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    org_id: Optional[str] = None
    object_type: str
    object_id: str
    requested_by: str
    approver_id: Optional[str] = None
    status: str
    notes: Optional[str] = None
    decision_notes: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    created_at: datetime

class GuestInviteRequest(BaseModel):
    email: EmailStr
    can_comment: bool = True

class GuestAccessOut(BaseModel):
    id: str
    org_id: str
    proposal_id: str
    user_id: str
    email: str
    can_comment: bool
    created_at: datetime

class ActivityEntryOut(BaseModel):
    id: str
    actor_id: str
    actor_name: Optional[str] = None
    action: str
    object_type: Optional[str] = None
    object_id: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None
    created_at: datetime


# ── Document Library (PRD §14) ────────────────────────────────────────────────

class DocumentCreate(BaseModel):
    title: str
    library_type: str = "document"
    proposal_id: Optional[str] = None
    content: Optional[str] = None
    file_url: Optional[str] = None
    format: Optional[str] = None
    change_note: Optional[str] = None

class DocumentVersionCreate(BaseModel):
    content: Optional[str] = None
    file_url: Optional[str] = None
    format: Optional[str] = None
    change_note: Optional[str] = None

class DocumentVersionOut(BaseModel):
    id: str
    document_id: str
    version_number: int
    content: Optional[str] = None
    file_url: Optional[str] = None
    format: Optional[str] = None
    change_note: Optional[str] = None
    has_embedding: bool = False
    created_by: str
    created_at: datetime

class DocumentOut(BaseModel):
    id: str
    org_id: str
    proposal_id: Optional[str] = None
    library_type: str
    title: str
    status: str
    version_count: int
    latest_version: Optional[DocumentVersionOut] = None
    created_by: str
    created_at: datetime
    updated_at: Optional[datetime] = None

class DocumentShareCreate(BaseModel):
    shared_with_user_id: Optional[str] = None
    external_email: Optional[EmailStr] = None
    permission: str = "view"
    expires_in_days: Optional[int] = Field(default=None, ge=1)

class DocumentShareOut(BaseModel):
    id: str
    document_id: str
    shared_with_user_id: Optional[str] = None
    external_email: Optional[str] = None
    share_token: Optional[str] = None
    permission: str
    expires_at: Optional[datetime] = None
    created_at: datetime

class DocumentSearchResult(BaseModel):
    document_id: str
    version_id: str
    title: str
    library_type: str
    snippet: str
    similarity: Optional[float] = None

class RetentionPolicyRequest(BaseModel):
    library_type: str
    retention_days: Optional[int] = Field(default=None, ge=1)

class RetentionPolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    org_id: str
    library_type: str
    retention_days: Optional[int] = None

class ArchiveExpiredOut(BaseModel):
    archived_count: int


# ── Supporting Documents (post-Phase-3 addendum — Communication/Partnership
# document generators from the pre-v5 "Supporting Documents Studio" concept,
# see engines/supporting_documents_engine.py) ───────────────────────────────

class SupportingDocumentTypeOut(BaseModel):
    key: str
    label: str
    category: str

class GenerateSupportingDocumentRequest(BaseModel):
    doc_type: str
    proposal_id: str
    recipient_name: Optional[str] = None
    recipient_organization: Optional[str] = None
    additional_context: Optional[str] = None


# ── Funding Intelligence & Grant Tracking (PRD §15, Phase 4) ────────────────────
# Pipeline/watchlist schemas for the pre-award funnel built on top of the
# existing FOARecord opportunity record. `agency`/`phase`/`grant_type` are
# plain `str` here (not the Agency/Phase/GrantType enums used by
# FOATemplate) because Grants.gov-synced records can carry any agency code
# or a non-SBIR opportunity type the strict enums don't cover.

class FOARecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    org_id: Optional[str] = None
    agency: str
    program_title: str
    solicitation_number: Optional[str] = None
    phase: str
    grant_type: str
    total_page_limit: Optional[int] = None
    deadline: Optional[datetime] = None
    source: str
    external_id: Optional[str] = None
    external_url: Optional[str] = None
    estimated_award_floor: Optional[float] = None
    estimated_award_ceiling: Optional[float] = None
    eligibility_summary: Optional[str] = None
    # Version 3.0 architecture upgrade, Phase 16 (cont'd) — plain-language
    # AI summary, generated alongside eligibility_summary above so a user
    # can decide whether an opportunity is worth pursuing before clicking
    # into it. None until the record has been parsed/enriched/summarized.
    ai_summary: Optional[str] = None
    pipeline_stage: str
    bid_no_go_decision: Optional[str] = None
    bid_no_go_rationale: Optional[str] = None
    assigned_to: Optional[str] = None
    uploaded_by: Optional[str] = None
    last_synced_at: Optional[datetime] = None
    created_at: datetime
    has_parsed_template: bool = False
    # Version 3.0 architecture upgrade, Phase 14 (Renewal Loop Closure) —
    # surfaces FOARecord.originating_award_id (set since Phase 5 but never
    # exposed) plus a resolved, human-readable label/link target so the
    # pipeline board can render "Renewal of <award>" provenance without a
    # separate lookup call. All four are None for every non-renewal record.
    originating_award_id: Optional[str] = None
    originating_proposal_id: Optional[str] = None
    originating_award_label: Optional[str] = None
    renewal_notes: Optional[str] = None
    # Funding Opportunity Intelligence, Phase 1 (Grant Finding Workspace
    # Upgrade) — the structured, 14-section pursuit-decision report that
    # POST /foa/{foa_id}/summarize now produces (see
    # engines/foa_parser.py::analyze_opportunity), plus cached headline
    # classifications so opportunity cards can show an eligibility/
    # complexity/attractiveness indicator without unpacking the full report.
    # None until a record has been analyzed. Typed as a loose dict (not a
    # nested Pydantic model) so the report's internal shape can evolve
    # without another schema/migration round-trip — the frontend renders it
    # defensively field-by-field.
    intelligence_report: Optional[dict] = None
    eligibility_status: Optional[str] = None
    complexity: Optional[str] = None
    attractiveness: Optional[str] = None
    attractiveness_reason: Optional[str] = None
    # Funding Opportunity Intelligence, Phase 2 (Organization-Specific
    # Matching, Ranking & Decision Intelligence) — see
    # engines/fit_score_engine.py. Deterministic, explainable, free (no AI
    # credit cost) — computed fresh on every /foa/pipeline read against
    # whichever Funding Intelligence Profile (personal or org, matching
    # this request's scope) is on file. All fields are None when there is
    # no profile to score against yet, never a misleading default score.
    fit_score: Optional[int] = None
    fit_bucket: Optional[str] = None
    fit_recommendation: Optional[str] = None
    fit_recommendation_reason: Optional[str] = None
    fit_categories: Optional[List[dict]] = None

class PipelineStageUpdateRequest(BaseModel):
    stage: str
    notes: Optional[str] = None

class PipelineStageEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    foa_id: str
    from_stage: Optional[str] = None
    to_stage: str
    changed_by: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime

class BidNoGoRequest(BaseModel):
    decision: str  # bid | no_go | undecided
    rationale: Optional[str] = None

class WatchlistCreate(BaseModel):
    name: str
    keyword: Optional[str] = None
    agencies: Optional[List[str]] = None
    funding_categories: Optional[List[str]] = None
    min_award: Optional[float] = None
    max_award: Optional[float] = None

class WatchlistUpdate(BaseModel):
    name: Optional[str] = None
    keyword: Optional[str] = None
    agencies: Optional[List[str]] = None
    funding_categories: Optional[List[str]] = None
    min_award: Optional[float] = None
    max_award: Optional[float] = None
    active: Optional[bool] = None

class WatchlistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    org_id: Optional[str] = None
    owner_id: str
    name: str
    keyword: Optional[str] = None
    agencies: Optional[List[str]] = None
    funding_categories: Optional[List[str]] = None
    min_award: Optional[float] = None
    max_award: Optional[float] = None
    active: bool
    last_run_at: Optional[datetime] = None
    created_at: datetime

class SyncResultOut(BaseModel):
    source: str                 # grants_gov | sam_gov
    configured: bool            # False for sam_gov with no API key set
    created_count: int = 0
    updated_count: int = 0
    matched_watchlists: int = 0
    message: Optional[str] = None

class PipelineReportOut(BaseModel):
    total_opportunities: int
    by_stage: Dict[str, int]
    win_rate: Optional[float] = None           # awarded / (awarded + declined), None if no terminal records yet
    avg_cycle_time_days: Optional[float] = None
    pipeline_value: float = 0.0                # sum of estimated_award_ceiling across non-terminal stages


# ── Organizational Learning & Historical Performance (Version 3.0 upgrade, ──
# Phase 3.1 — docs/Clariva Funding Opportunity Intelligence product
# definition upgrade for monetization_1.docx §4.1-4.2) ──────────────────────

class LearningTimelineEventOut(BaseModel):
    event_type: str            # opportunity_discovered | stage_change | proposal_status_change | outcome_recorded | award_created | award_closed
    occurred_at: datetime
    title: str
    description: Optional[str] = None
    foa_id: Optional[str] = None
    proposal_id: Optional[str] = None
    award_id: Optional[str] = None

class PerformanceBreakdownOut(BaseModel):
    label: str
    total: int
    awarded: int
    lost: int
    win_rate: Optional[float] = None

class HistoricalPerformanceOut(BaseModel):
    total_opportunities: int
    by_stage: Dict[str, int]
    win_rate: Optional[float] = None
    avg_cycle_time_days: Optional[float] = None
    pipeline_value: float = 0.0
    total_awarded_funding: float = 0.0         # sum of actual Award.total_award_value, not the FOA's estimated ceiling
    by_agency: List[PerformanceBreakdownOut] = []
    by_program_type: List[PerformanceBreakdownOut] = []
    by_funding_range: List[PerformanceBreakdownOut] = []


# ── Portfolio-Level Recommendations (Phase 3 §4.3) ────────────────────────────

class RecommendedOpportunityOut(BaseModel):
    foa_id: str
    program_title: str
    agency: str
    deadline: Optional[str] = None
    fit_score: int
    recommendation: str
    rationale: str


class ResourceConflictOut(BaseModel):
    foa_ids: List[str]
    titles: List[str]
    deadline_window_days: int
    message: str


class PortfolioRecommendationOut(BaseModel):
    recommended_portfolio: List[RecommendedOpportunityOut] = []
    by_stage: Dict[str, int] = {}
    pursuit_capacity: Optional[int] = None
    currently_pursuing: int = 0
    capacity_status: Optional[str] = None  # under | at | over | None (not configured)
    resource_conflicts: List[ResourceConflictOut] = []
    disclaimer: str


# ── Bulk Opportunity Intelligence (Phase 3 §4.5) ──────────────────────────────
# Two-step "cheap ranking, then paid deep analysis only on what you pick"
# flow — see engines/fit_score_engine.py::score_opportunity (free) and
# routers/foa.py::_analyze_single_opportunity (paid, per-item) for the
# reused engines this wraps. Team/Organization/Enterprise plan tiers only.

class BulkRankRequest(BaseModel):
    foa_ids: List[str]
    org_id: Optional[str] = None

class BulkRankResultOut(BaseModel):
    foa_id: str
    program_title: str
    agency: str
    pipeline_stage: str
    fit_score: Optional[int] = None
    fit_bucket: Optional[str] = None
    fit_recommendation: Optional[str] = None

class BulkRankResponseOut(BaseModel):
    results: List[BulkRankResultOut] = []
    disclaimer: str

class BulkAnalyzeRequest(BaseModel):
    foa_ids: List[str]
    org_id: Optional[str] = None

class BulkAnalyzeResultOut(BaseModel):
    foa_id: str
    program_title: str
    success: bool
    already_analyzed: bool = False
    error: Optional[str] = None

class BulkAnalyzeResponseOut(BaseModel):
    results: List[BulkAnalyzeResultOut] = []
    analyzed_count: int
    failed_count: int


# ── Award & Project Management (PRD §16-17, Phase 5) ─────────────────────────
# An Award is 1:1 with a Proposal, created explicitly (never automatically)
# once a pipeline opportunity reaches the "awarded" stage. Everything below
# hangs off award_id.

class AwardCreate(BaseModel):
    proposal_id: str
    funding_agency: str
    award_number: Optional[str] = None
    period_of_performance_start: Optional[datetime] = None
    period_of_performance_end: Optional[datetime] = None
    total_award_value: Optional[float] = None
    terms: Optional[str] = None
    # If true (default), snapshot the proposal's current BudgetRecord.id as
    # this award's budget baseline for burn-rate tracking.
    link_budget: bool = True

class QuickAwardIntakeRequest(BaseModel):
    """Version 3.0 upgrade, Phase 15 — 'Quick Award Intake'. For a customer
    who already has a signed/funded award and never used Pre-Award, so there
    is no existing Proposal for the Award-creation flow above to attach to.
    AwardEngine.create_award_from_intake() auto-creates a minimal
    origin="imported" Proposal shell behind the scenes and reuses the
    existing create_award() on it — no change to Award.proposal_id's
    1:1/required relationship. org_id is optional (a solo user with no
    organization can still import an award; they just can't attach the
    award-notice document afterward, since Document.org_id is required —
    see routers/awards.py::intake_document)."""
    title: str
    funding_agency: str
    award_number: Optional[str] = None
    period_of_performance_start: Optional[datetime] = None
    period_of_performance_end: Optional[datetime] = None
    total_award_value: Optional[float] = None
    terms: Optional[str] = None
    org_id: Optional[str] = None

class AwardUpdate(BaseModel):
    award_number: Optional[str] = None
    period_of_performance_start: Optional[datetime] = None
    period_of_performance_end: Optional[datetime] = None
    total_award_value: Optional[float] = None
    terms: Optional[str] = None
    status: Optional[str] = None  # active | closed | terminated

class AwardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    proposal_id: str
    foa_id: Optional[str] = None
    org_id: Optional[str] = None
    budget_record_id: Optional[str] = None
    award_number: Optional[str] = None
    funding_agency: str
    period_of_performance_start: Optional[datetime] = None
    period_of_performance_end: Optional[datetime] = None
    total_award_value: Optional[float] = None
    terms: Optional[str] = None
    status: str
    award_status: str = "received"  # received | active | closed — see Award.award_status docstring
    created_by: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    proposal_title: Optional[str] = None

class AwardExpenditureCreate(BaseModel):
    category: str
    description: Optional[str] = None
    amount: float
    incurred_date: Optional[datetime] = None

class AwardExpenditureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    award_id: str
    category: str
    description: Optional[str] = None
    amount: float
    incurred_date: Optional[datetime] = None
    recorded_by: Optional[str] = None
    created_at: datetime

class BudgetStatusOut(BaseModel):
    """Burn-rate / variance snapshot for an award (PRD §16)."""
    award_id: str
    baseline_total_cost: float = 0.0
    total_expended: float = 0.0
    burn_rate_pct: Optional[float] = None       # total_expended / baseline_total_cost * 100
    elapsed_pct: Optional[float] = None         # elapsed time / period of performance * 100
    variance_pct: Optional[float] = None        # burn_rate_pct - elapsed_pct; positive = overspending pace
    by_category: Dict[str, float] = Field(default_factory=dict)

class AwardComplianceItemCreate(BaseModel):
    obligation: str
    category: Optional[str] = None
    due_date: Optional[datetime] = None
    notes: Optional[str] = None

class AwardComplianceItemUpdate(BaseModel):
    obligation: Optional[str] = None
    category: Optional[str] = None
    due_date: Optional[datetime] = None
    status: Optional[str] = None  # pending | complete | overdue | waived
    notes: Optional[str] = None

class AwardComplianceItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    award_id: str
    obligation: str
    category: Optional[str] = None
    due_date: Optional[datetime] = None
    status: str
    notes: Optional[str] = None
    completed_by: Optional[str] = None
    completed_at: Optional[datetime] = None
    created_at: datetime

class AwardAmendmentCreate(BaseModel):
    amendment_type: str  # scope | budget | period_of_performance | other
    description: str
    effective_changes: Optional[Dict[str, Any]] = None
    approver_id: Optional[str] = None

class AwardAmendmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    award_id: str
    amendment_type: str
    description: str
    effective_changes: Optional[Dict[str, Any]] = None
    requested_by: str
    approval_request_id: Optional[str] = None
    status: str
    created_at: datetime
    decided_at: Optional[datetime] = None

class ProjectIssueCreate(BaseModel):
    title: str
    description: Optional[str] = None
    severity: str = "medium"  # low | medium | high | critical
    work_package_id: Optional[str] = None

class ProjectIssueUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    severity: Optional[str] = None
    status: Optional[str] = None  # open | resolved

class ProjectIssueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    award_id: str
    work_package_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    severity: str
    status: str
    raised_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime

class ProjectExecutionStatusOut(BaseModel):
    """Rolled-up execution status (PRD §17), read from Phase 2's Scope of
    Work Engine — no separate WBS is stored for Phase 5."""
    award_id: str
    total_work_packages: int = 0
    total_tasks: int = 0
    tasks_by_status: Dict[str, int] = Field(default_factory=dict)
    total_milestones: int = 0
    milestones_by_status: Dict[str, int] = Field(default_factory=dict)
    total_deliverables: int = 0
    deliverables_by_status: Dict[str, int] = Field(default_factory=dict)
    open_issues: int = 0
    stale_flags: Dict[str, Any] = Field(default_factory=dict)

class AwardPerformanceRecordCreate(BaseModel):
    kpi_name: str
    target: Optional[float] = None
    actual_value: Optional[float] = None
    unit: Optional[str] = None
    period_label: Optional[str] = None
    notes: Optional[str] = None

class AwardPerformanceRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    award_id: str
    kpi_name: str
    target: Optional[float] = None
    actual_value: Optional[float] = None
    unit: Optional[str] = None
    period_label: Optional[str] = None
    notes: Optional[str] = None
    recorded_by: Optional[str] = None
    created_at: datetime

class AwardReportOut(BaseModel):
    """Deterministic, structured report view (PRD §17) — a compiled read
    over Award/BudgetStatus/ProjectExecutionStatus/Performance/Compliance,
    not a separately-authored document."""
    award_id: str
    report_type: str  # technical | financial | progress | final | commercialization
    generated_at: datetime
    award: AwardOut
    budget_status: Optional[BudgetStatusOut] = None
    execution_status: Optional[ProjectExecutionStatusOut] = None
    performance: List[AwardPerformanceRecordOut] = Field(default_factory=list)
    compliance_summary: Optional[Dict[str, int]] = None
    narrative: Optional[str] = None   # populated only when AI narrative generation is requested

class ReportNarrativeRequest(BaseModel):
    additional_context: Optional[str] = None

# ── Persisted, human-reviewed reports (see models.db_models.AwardReport) ──────
# The live AwardReportOut/ReportNarrativeRequest pair above stays as-is for
# an ephemeral preview (GET .../report, POST .../report/narrative); these
# cover the actual draft → edit → submit → approve/reject → export
# workflow so a generated narrative is never exportable without a human
# reviewing and approving it first.

class AwardReportCreateRequest(BaseModel):
    report_type: str  # technical | financial | progress | final | commercialization
    additional_context: Optional[str] = None

class AwardReportEditRequest(BaseModel):
    narrative: str

class AwardReportSubmitRequest(BaseModel):
    approver_id: Optional[str] = None  # None = any org member with manage_approvals may decide
    notes: Optional[str] = None

class AwardReportRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    award_id: str
    report_type: str
    status: str  # draft | pending_approval | approved | rejected
    narrative: str
    ai_generated_narrative: Optional[str] = None
    report_data: Optional[Dict[str, Any]] = None
    requested_by: str
    approval_request_id: Optional[str] = None
    exported_file_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    decided_at: Optional[datetime] = None

class AwardReportExportOut(BaseModel):
    download_url: str
    file_size: int
    exported_at: datetime

class AwardCloseoutRequest(BaseModel):
    deliverables_reconciled: bool = False
    deliverables_notes: Optional[str] = None
    equipment_disposition: Optional[str] = None
    final_report_submitted: bool = False
    lessons_learned: Optional[List[str]] = None
    outcome_score: Optional[float] = None  # written into the MemoryRecord created on closeout

class AwardCloseoutOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    award_id: str
    deliverables_reconciled: bool
    deliverables_notes: Optional[str] = None
    equipment_disposition: Optional[str] = None
    final_report_submitted: bool
    lessons_learned: List[str] = Field(default_factory=list)
    memory_record_id: Optional[str] = None
    closed_by: Optional[str] = None
    closed_at: Optional[datetime] = None
    created_at: datetime

class RenewalCreate(BaseModel):
    program_title: Optional[str] = None   # defaults to "<original title> (Renewal)"
    deadline: Optional[datetime] = None
    notes: Optional[str] = None


# ── Award Received: Data Model Foundation (Version 3.0 architecture upgrade, ──
# Phase 7) — ProjectBaseline (the immutable "Approved Project Baseline") and
# AwardCondition (sponsor conditions reviewed before activation). See
# db_models.py's ProjectBaseline/AwardCondition docstrings for the full
# rationale.

class ActivateProjectRequest(BaseModel):
    """Payload for the 'Activate Project' action — locks the first
    ProjectBaseline for an award and flips Award.award_status to 'active'."""
    notes: Optional[str] = None

class ProjectBaselineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    award_id: str
    version: int
    is_current: bool
    total_award_value: Optional[float] = None
    period_of_performance_start: Optional[datetime] = None
    period_of_performance_end: Optional[datetime] = None
    budget_snapshot: Dict[str, Any] = Field(default_factory=dict)
    scope_snapshot: Dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime

class AwardIntelligenceDraftOut(BaseModel):
    """Version 3.0 upgrade, Phase D — 'Award Intake Intelligence'. Unpersisted
    result of AwardEngine.extract_scope_and_award_fields() + routers/
    budget.py's _extract_budget_from_text(), returned by POST /awards/
    {award_id}/extract-intelligence for the user to review before anything
    is saved. `work_packages` matches generate_work_breakdown()'s shape
    exactly (name/description/start_month/end_month/tasks/milestones/
    deliverables); `budget` matches extract_budget_from_file()'s
    {"extracted": {...}, "totals": {...}} shape, or is null if the uploaded
    document(s) didn't contain budget-worthy content. objectives/
    need_statement/outputs/outcomes/kpis (Phase D.3) mirror ProjectKnowledge's
    own columns and derive_project_knowledge_from_proposal()'s field set —
    read from the customer's *uploaded* documents instead of AI-*generated*
    proposal sections, since a Quick-Award-Intake/Award-page-upload proposal
    has no generated sections for that feature to derive from."""
    total_award_value: Optional[float] = None
    period_of_performance_start: Optional[datetime] = None
    period_of_performance_end: Optional[datetime] = None
    work_packages: List[Dict[str, Any]] = Field(default_factory=list)
    budget: Optional[Dict[str, Any]] = None
    objectives: Optional[str] = None
    need_statement: Optional[str] = None
    outputs: Optional[str] = None
    outcomes: Optional[str] = None
    kpis: List[Dict[str, Any]] = Field(default_factory=list)
    extraction_notes: Optional[str] = None
    source_document_count: int = 0

class AwardIntelligenceApplyRequest(BaseModel):
    """Body for POST /awards/{award_id}/apply-intelligence — the draft above,
    possibly hand-edited by the user first, sent back to be persisted."""
    total_award_value: Optional[float] = None
    period_of_performance_start: Optional[datetime] = None
    period_of_performance_end: Optional[datetime] = None
    work_packages: List[Dict[str, Any]] = Field(default_factory=list)
    budget: Optional[Dict[str, Any]] = None
    objectives: Optional[str] = None
    need_statement: Optional[str] = None
    outputs: Optional[str] = None
    outcomes: Optional[str] = None
    kpis: List[Dict[str, Any]] = Field(default_factory=list)

class AwardConditionCreate(BaseModel):
    description: str
    category: Optional[str] = None  # reporting | financial | regulatory | programmatic | other
    due_date: Optional[datetime] = None

class AwardConditionUpdate(BaseModel):
    description: Optional[str] = None
    category: Optional[str] = None
    due_date: Optional[datetime] = None
    status: Optional[str] = None  # open | resolved | waived

class AwardConditionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    award_id: str
    description: str
    category: Optional[str] = None
    due_date: Optional[datetime] = None
    status: str
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_by: Optional[str] = None
    created_at: datetime


# ── Planned vs. Actual (Version 3.0 architecture upgrade, Phase 10) ──────────
# Replaces the dollars-vs-time-only BudgetStatusOut with a real comparison
# against the locked ProjectBaseline (Phase 7) across budget, work packages,
# milestones, deliverables, and schedule — see AwardEngine.get_planned_vs_actual
# for the approximations this makes explicit (see its docstring).

class PlannedVsActualOut(BaseModel):
    award_id: str
    baseline_version: int
    baseline_locked_at: datetime
    # Budget — same burn-rate/elapsed-time math as BudgetStatusOut, but
    # against the frozen baseline_snapshot total, not the live BudgetRecord,
    # so this number doesn't silently drift if the live budget is edited
    # after activation without a formal re-baseline.
    baseline_total_cost: float = 0.0
    total_expended: float = 0.0
    burn_rate_pct: Optional[float] = None
    elapsed_pct: Optional[float] = None
    budget_variance_pct: Optional[float] = None
    # Scope — planned (from the baseline snapshot) vs current (live
    # ScopeOfWork). A non-zero drift means the live scope has changed since
    # the baseline was locked without a re-baseline capturing it yet.
    planned_work_packages: int = 0
    current_work_packages: int = 0
    planned_milestones: int = 0
    current_milestones: int = 0
    milestones_completed: int = 0
    milestones_behind_schedule: int = 0
    planned_deliverables: int = 0
    current_deliverables: int = 0
    deliverables_completed: int = 0
    deliverables_behind_schedule: int = 0
    scope_drift: bool = False


# ── Portfolio Dashboard (Version 3.0 architecture upgrade, Phase 12) ─────────
# One rolled-up view across every proposal/award a user can see (owned +
# every org they belong to), organized around the same three lifecycle
# stages as LifecycleBanner (frontend) and Award.award_status — see
# engines/portfolio_engine.py for how each field is computed.

class PortfolioAlertOut(BaseModel):
    severity: str  # "warning" | "info"
    message: str
    proposal_id: Optional[str] = None
    award_id: Optional[str] = None

class PortfolioSummaryOut(BaseModel):
    # Lifecycle pipeline widget
    pre_award_count: int = 0
    award_received_count: int = 0
    post_award_count: int = 0
    # Portfolio health
    total_active_award_value: float = 0.0
    open_sponsor_conditions: int = 0
    awards_over_budget: int = 0
    milestones_behind_schedule: int = 0
    deliverables_behind_schedule: int = 0
    # Executive alerts — ordered most-actionable first
    alerts: List[PortfolioAlertOut] = Field(default_factory=list)


# ── Integrations & Marketplace (PRD §19-20, Phase 6) ─────────────────────────

class ConnectorTypeOut(BaseModel):
    """One entry from the CONNECTOR_TYPES registry — describes a connector
    type without requiring a connection to exist yet, so the frontend can
    render "Coming soon" for OAuth-based types this environment can't
    functionally support."""
    connector_type: str
    label: str
    functional: bool          # False = registered but always "not configured" (needs real OAuth credentials)
    config_fields: List[str] = Field(default_factory=list)

class ConnectorCreate(BaseModel):
    connector_type: str
    name: str
    config: Optional[Dict[str, Any]] = None
    event_types: Optional[List[str]] = None

class ConnectorUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    event_types: Optional[List[str]] = None
    active: Optional[bool] = None

class ConnectorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    org_id: str
    connector_type: str
    name: str
    config: Optional[Dict[str, Any]] = None
    event_types: Optional[List[str]] = None
    active: bool
    last_tested_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_by: str
    created_at: datetime
    updated_at: Optional[datetime] = None

class ConnectorTestResultOut(BaseModel):
    success: bool
    configured: bool
    status_code: Optional[int] = None
    error: Optional[str] = None

class ConnectorEventLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    connector_id: str
    event_type: str
    success: bool
    status_code: Optional[int] = None
    error: Optional[str] = None
    created_at: datetime

class ApiKeyCreate(BaseModel):
    name: str
    role: str = "viewer"  # owner | editor | viewer — capped at the creator's own role

class ApiKeyCreatedOut(BaseModel):
    """Returned exactly once, at creation — the only time the plaintext key
    is ever available."""
    id: str
    name: str
    role: str
    key: str
    key_prefix: str

class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    org_id: str
    name: str
    key_prefix: str
    role: str
    created_by: str
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None

class OrganizationBrandingUpdate(BaseModel):
    white_label_enabled: Optional[bool] = None
    brand_name: Optional[str] = None
    logo_url: Optional[str] = None
    primary_color: Optional[str] = None

class OrganizationBrandingOut(BaseModel):
    org_id: str
    white_label_enabled: bool
    brand_name: Optional[str] = None
    logo_url: Optional[str] = None
    primary_color: Optional[str] = None

class MarketplaceListingCreate(BaseModel):
    listing_type: str  # template_pack | connector | ai_capability
    name: str
    description: Optional[str] = None
    price_cents: Optional[int] = None
    currency: str = "usd"
    vendor_org_id: Optional[str] = None  # None = platform-provided listing

class MarketplaceListingUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price_cents: Optional[int] = None
    currency: Optional[str] = None
    status: Optional[str] = None  # draft | published | archived

class PublicProposalOut(BaseModel):
    """Deliberately minimal, stable shape for the public API (PRD §19) —
    external partners get a small, intentional surface, not every internal
    field ProposalOut exposes to the app's own frontend. Constructed
    field-by-field in routers/public_api.py rather than via
    `from_attributes`, since the ORM's `id` column is renamed to
    `proposal_id` here."""
    proposal_id: str
    title: str
    agency: str
    phase: str
    status: str
    created_at: datetime
    updated_at: Optional[datetime] = None

class PublicPipelineCreate(BaseModel):
    agency: str
    program_title: str
    phase: str = "phase_i"
    grant_type: str = "sbir"
    deadline: Optional[datetime] = None

class MarketplaceListingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    vendor_org_id: Optional[str] = None
    listing_type: str
    name: str
    description: Optional[str] = None
    price_cents: Optional[int] = None
    currency: str
    status: str
    created_by: str
    created_at: datetime
    updated_at: Optional[datetime] = None

class MarketplacePurchaseRequest(BaseModel):
    buyer_org_id: str

class MarketplacePurchaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    listing_id: str
    buyer_org_id: str
    purchased_by: str
    price_cents_paid: int
    created_at: datetime


# ── Phase 2 — On-Demand AI Services Marketplace & Org Funding Controls ──
# (Enterprise Public-Facing Pricing & Internal Engineering Economics spec
# v1.0, Aug 2026, §9.2). See engines/service_catalog_engine.py for the
# pricing/entitlement logic these schemas surface.

class ServiceCatalogItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    service_key: str
    category: str
    name: str
    description: Optional[str] = None
    complexity: Optional[str] = None
    workspace: str
    subscriber_price_cents: int
    payg_price_cents: Optional[int] = None
    recurring: bool
    active: bool


class ServiceQuoteOut(BaseModel):
    """Price-before-generation confirmation payload — a router must fetch
    this and have the caller confirm it before calling consume() for any
    paid service (acceptance criterion: "no paid marketplace generation
    occurs without authorization")."""
    service_key: str
    name: str
    category: str
    complexity: Optional[str] = None
    recurring: bool
    funding_source: str          # "complimentary" | "ai_services_balance"
    price_cents: int             # 0 when funding_source == "complimentary"
    list_price_cents: int        # the catalog price regardless of funding source (for display)
    ai_services_balance_cents: int
    sufficient_balance: bool


class ServiceConsumeRequest(BaseModel):
    service_key: str
    # Free-form context recorded on the AIServiceTransaction, e.g.
    # {"proposal_id": "...", "foa_id": "..."}. Never used for pricing math.
    reference: Optional[Dict[str, Any]] = None


class ServiceEntitlementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    service_key: str
    granted_quantity: int
    used_quantity: int
    expires_at: Optional[datetime] = None


class AIServiceTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    user_id: Optional[str] = None
    service_key: str
    funding_source: str
    price_cents: int
    reference: Optional[Dict[str, Any]] = None
    created_at: datetime


class OrgServiceSummaryOut(BaseModel):
    ai_services_balance_cents: int
    entitlements: List[ServiceEntitlementOut]
    transactions: List[AIServiceTransactionOut]


# ── Phase 3.1 — Configurable Pricing Controls (admin-only) ──────────────
# Enterprise Pricing spec §9.3 (Phase 3): lets a superadmin edit the
# service catalog / complimentary allowances / an org's plan without a
# code change + redeploy. See routers/admin.py's require_superadmin and
# the new admin-only endpoints on routers/service_catalog.py.

class ServiceCatalogItemAdminUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    subscriber_price_cents: Optional[int] = Field(default=None, ge=0)
    payg_price_cents: Optional[int] = Field(default=None, ge=0)
    active: Optional[bool] = None


class ServiceCatalogItemAdminCreate(BaseModel):
    service_key: str
    category: str
    name: str
    description: Optional[str] = None
    complexity: Optional[str] = None
    workspace: str
    subscriber_price_cents: int = Field(ge=0)
    payg_price_cents: Optional[int] = Field(default=None, ge=0)
    recurring: bool = False


class ComplimentaryAllowanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    plan: str
    service_key: str
    quantity: Optional[int] = None
    validity_days: int


class ComplimentaryAllowanceAdminCreate(BaseModel):
    plan: str
    service_key: str
    quantity: Optional[int] = Field(default=None, ge=0)
    validity_days: int = Field(default=90, ge=1)


class ComplimentaryAllowanceAdminUpdate(BaseModel):
    quantity: Optional[int] = Field(default=None, ge=0)
    validity_days: Optional[int] = Field(default=None, ge=1)


class OrgAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    plan: str
    created_at: datetime


class OrgPlanUpdateRequest(BaseModel):
    plan: str  # free | professional | team | organization | enterprise
