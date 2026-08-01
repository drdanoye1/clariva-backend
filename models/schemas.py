"""
Clariva Intelligent Grant Writing Platform — Pydantic Schemas
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, Field


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

class UserOut(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    organization: str
    created_at: datetime

    class Config:
        from_attributes = True


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

    class Config:
        from_attributes = True

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

class ExportRequest(BaseModel):
    proposal_id: str
    format: DocumentFormat
    include_scoring_summary: bool = True
    include_reviewer_feedback: bool = False
    include_compliance_status: bool = True

class ExportResponse(BaseModel):
    proposal_id: str
    format: DocumentFormat
    download_url: str
    file_size_bytes: int
    exported_at: datetime


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
