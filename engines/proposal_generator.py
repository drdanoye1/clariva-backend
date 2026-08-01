"""
Engine 4 — Proposal Generation Engine
Section-aware, company-context-driven content generation using GPT-4o.
"""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any, Dict, List, Optional

import openai
from fastapi import HTTPException

from config import settings
from engines.grant_templates import get_grant_type, get_generation_context

# ── System prompt ─────────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """
You are an expert grant writer with 20+ years of experience winning federal, state, and foundation grants.
You write proposals that sound like they come from a specific, credible organization — never generic boilerplate.

Rules:
- Use the organization's actual name, PI, team, and facilities when provided
- Be specific: name people, cite credentials, reference real resources
- When information is marked [MISSING: ...], keep the placeholder exactly as-is — do not invent details
- Use active voice and precise, quantified language
- Address the funder's specific evaluation criteria directly
- Do NOT include section headers in your output — content paragraphs only
- Match the grant type's terminology, format, and priorities
- CRITICAL: Use ONLY the grant type terminology from the GRANT SELECTION PATH provided.
  If this is a CDBG grant, write CDBG language. If NIH R01, write NIH language.
  Never substitute SBIR/STTR language unless the grant type is literally SBIR or STTR.

FORMATTING — CRITICAL:
- Output plain prose only. Do NOT use any markdown formatting.
- No asterisks for bold (**text** or *text*), no pound signs for headings (## or ###),
  no underscores for italic (__text__ or _text_), no backticks, no bullet hyphens.
- The output is inserted directly into a federal Word document; markdown characters
  will appear literally and disqualify the submission.

References:
- Where relevant, cite authoritative sources (program guidance documents, federal regulations,
  agency strategic plans, peer-reviewed literature) inline as (Source: [Title, URL or citation]).
- At the end of sections with cited material, add a brief "Key References" list.
- Do not fabricate citations. Only cite sources that demonstrably exist for this grant program.
"""

def _build_system_prompt(grant_type: str = "federal_other", program_label: str = "") -> str:
    ctx = get_generation_context(grant_type)
    prog = f"\nSPECIFIC PROGRAM: {program_label}" if program_label else ""
    return BASE_SYSTEM_PROMPT.strip() + "\n\n" + ctx + prog

# ── Section context mapping ───────────────────────────────────────────────────
# Maps section_id keywords → which profile fields to inject

SECTION_PROFILE_KEYS = {
    "team":              ["pi", "team_members"],
    "personnel":         ["pi", "team_members"],
    "qualifications":    ["pi", "team_members"],
    "biography":         ["pi"],
    "biosketches":       ["pi", "team_members"],
    "facilities":        ["facilities"],
    "equipment":         ["facilities"],
    "resources":         ["facilities"],
    "past_performance":  ["past_performance", "company_capabilities"],
    "introduction":      ["company_capabilities", "past_performance"],
    "background":        ["company_capabilities", "past_performance"],
    "company":           ["company_capabilities", "past_performance"],
    "commercialization": ["partners", "company_capabilities"],
    "partnership":       ["partners"],
    "collaboration":     ["partners"],
    "subcontract":       ["partners"],
    "project_summary":   ["pi", "company_capabilities"],
    "abstract":          ["pi", "company_capabilities"],
    "significance":      ["company_capabilities", "past_performance"],
}

# ── Word count targets by section ─────────────────────────────────────────────

WORD_TARGETS = {
    "project_summary":    300,
    "abstract":           250,
    "specific_aims":      350,
    "introduction":       400,
    "significance":       600,
    "innovation":         600,
    "technical_merit":    1000,
    "technical_approach": 1200,
    "approach":           1500,
    "team":               500,
    "team_qualifications": 500,
    "personnel":          450,
    "facilities":         400,
    "past_performance":   500,
    "commercialization":  900,
    "budget_justification": 400,
    "broader_impacts":    500,
    "references":         0,
}

# ── Agency-specific reviewer framing ─────────────────────────────────────────

AGENCY_GUIDANCE = {
    "NSF": """NSF SBIR evaluates TWO equally-weighted criteria — address BOTH explicitly in every section:

1. INTELLECTUAL MERIT: Scientific and technical quality, novelty, and rigor.
   - Explain WHY this research is scientifically novel (not just commercially useful)
   - Reference the state of the art and explain what gap this fills
   - Demonstrate the PI and team's research qualifications
   - Use precise, quantified technical language (avoid marketing language)
   - Describe the research hypothesis and experimental approach

2. BROADER IMPACTS: Societal benefit, STEM workforce development, commercial potential.
   - Explain concrete societal benefits beyond the immediate customer
   - Address how this advances the NSF mission of scientific progress
   - Describe the commercial pathway and market size with data
   - Mention any educational, diversity, or workforce development aspects

NSF-SPECIFIC RULES:
- NSF SBIR does NOT have a separate commercialization section — embed market potential throughout
- NSF Phase I asks: "Is the innovation technically feasible?" — every section must answer this
- Avoid DoD/military language; NSF is civilian-focused
- Technology Topic Areas sections must map to NSF's defined topic clusters
- Do NOT claim "first ever" without evidence — NSF reviewers will reject unsubstantiated claims
- Use academic/scientific tone balanced with commercial clarity""",

    "NIH": """NIH evaluates five scored criteria — address ALL five explicitly:

1. SIGNIFICANCE (20%): Does this address an important health problem? What is the clinical/public health impact?
   - Cite epidemiological data, disease burden, unmet clinical need
   - Reference NIH priority areas and Healthy People goals

2. INNOVATION (15%): Does this challenge existing paradigms? What is novel about the approach/technology/method?
   - Distinguish from current standard of care or existing technologies
   - Explain the conceptual or methodological advance

3. APPROACH (30%): Are the methods rigorous and feasible? Are potential pitfalls addressed?
   - Include Aim structure: rationale, hypothesis, methods, expected outcomes, alternatives
   - Address rigor, reproducibility, and biological sex as a variable

4. INVESTIGATORS (20%): Are the PI and team well-suited?
   - Emphasize NIH-relevant track record, publications, prior funding
   - Describe team's complementary expertise

5. ENVIRONMENT (15%): Does the institutional environment contribute?
   - Reference institutional support, core facilities, collaborations

NIH RULES: Use past tense for completed work, future tense for proposed work. Use "we" not "I". All aims must be independently achievable.""",

    "DOD": """DoD SBIR evaluates: Technical Merit (50%), Commercialization Potential (25%), Company Qualifications (25%).

TECHNICAL MERIT: Be highly specific about the military/defense problem being solved.
   - Reference specific DoD program offices, acquisition programs, or capability gaps
   - Use MIL-SPEC terminology where appropriate
   - Quantify performance improvements (TRL progression, SWAP-C metrics)
   - Address Technology Readiness Level (TRL) — Phase I typically TRL 2-4

TRANSITION POTENTIAL: DoD SBIR requires a clear path to a Program of Record.
   - Identify the specific DoD program/PM office that will adopt the technology
   - Describe Phase II and Phase III transition strategy explicitly
   - Reference any Letters of Interest from DoD transition partners

DUAL-USE: Address both defense and commercial market opportunities
   - Identify commercial spin-off markets
   - DoD values companies with diversified revenue potential

COMPANY QUALIFICATIONS: Emphasize any existing DoD contracts, clearances, or prior SBIR awards.""",

    "DARPA": """DARPA funds revolutionary breakthroughs — NOT incremental improvements. Evaluation: Technical Merit + Potential Impact.

DARPA CULTURE:
- Be BOLD. State explicitly what paradigm you are breaking
- "High risk, high payoff" is the DARPA mandate — acknowledge the risk
- Describe what happens if successful in transformative terms (orders of magnitude, not percent improvements)
- DARPA PMs are scientists/engineers — use deep technical language without apology

REQUIRED FRAMING:
- State the fundamental technical barrier that has prevented this before
- Explain why NOW is the right time (new material, new method, new insight)
- Describe the team's UNIQUE capability that no other team has
- Show 3-5 year vision, not just the immediate project

AVOID: Incremental framing ("slightly better than X"), excessive market analysis, conservative claims""",

    "DOE": """DOE SBIR evaluates: Scientific/Technical Merit (40%), Commercialization (35%), Qualifications (25%).

DOE MISSION ALIGNMENT:
- Explicitly connect to DOE Office mission areas (EERE, SC, NE, FE, OE, etc.)
- Reference DOE Quadrennial Technology Review priorities
- Use DOE terminology: TRL, MRL, energy efficiency, decarbonization, grid modernization

COMMERCIALIZATION (DOE emphasizes this heavily):
- Provide a Technology-to-Market plan
- Identify DOE national lab partners if applicable (CRADA, license opportunities)
- Address domestic manufacturing and supply chain
- Reference relevant industry standards (IEEE, ANSI, ASME)

SCIENTIFIC MERIT: DOE values basic research connection — cite foundational science supporting the approach.""",

    "NASA": """NASA SBIR evaluates: Technical Innovation (40%), Commercial Potential (30%), Experience/Capability (30%).

NASA MISSION ALIGNMENT:
- Map directly to NASA Strategic Plan goals and specific Mission Directorate priorities
- Reference specific NASA programs, missions, or technology roadmaps (NASA Technology Taxonomy)
- Explain how this technology enables or enhances NASA capabilities

TECHNICAL INNOVATION:
- Quantify improvements over current NASA-used technologies (mass, power, cost, performance)
- Address space environment constraints (radiation, thermal, vacuum, launch loads)
- Describe TRL advancement plan (Phase I: TRL 2→4, Phase II: TRL 4→6)

COMMERCIAL PATHWAY:
- Identify both NASA and non-NASA commercial markets
- NASA SBIR values companies that can succeed without continued NASA funding""",

    "ARPA-E": """ARPA-E funds transformational energy technology. Evaluation: Technical Merit + Potential Impact + Team.

TRANSFORMATIONAL STANDARD:
- Must be 10x better than current best, not 10% better — state the magnitude explicitly
- Addresses energy security, economic competitiveness, or environmental impact at scale
- Technology must be too early for private sector investment (Valley of Death stage)

TECHNICAL APPROACH:
- Show deep understanding of the fundamental physics/chemistry/engineering
- Address why current approaches fail and why yours won't
- Include preliminary data or theoretical basis for feasibility

IMPACT QUANTIFICATION:
- Estimate energy savings in quads or CO2 reduction in gigatons
- Reference EIA, DOE, or IEA data for market context
- Address levelized cost of energy (LCOE) or similar metrics

TEAM: ARPA-E values interdisciplinary teams combining science, engineering, and commercialization expertise.""",

    # ── Non-SBIR Federal Agency Guidance ──────────────────────────────────────

    "HUD": """HUD grants (CDBG, HOME, Choice Neighborhoods, CoC, etc.) focus on community development, affordable housing, and ending homelessness.

EVALUATION CRITERIA:
- Need: Document the specific housing/community problem with local data (census, HUD CHAS, PIC data)
- Capacity: Demonstrate organizational track record managing federal funds (prior HUD grants, audits, SAM.gov)
- Plan: Clear, measurable outcomes tied to HUD's strategic goals (housing stability, economic opportunity)
- Compliance: Address NEPA, Fair Housing Act, Section 3, Davis-Bacon, and URA as applicable

HUD-SPECIFIC REQUIREMENTS:
- Cite the specific HUD Notice of Funding Opportunity (NOFO) requirements
- Address Affirmatively Furthering Fair Housing (AFFH) — required for all HUD programs
- Reference local Consolidated Plan / Annual Action Plan alignment
- For CDBG: at least 70% of funds must benefit low/moderate-income persons (LMI); document LMI area or clientele benefit
- For HOME: 15% set-aside for CHDO (Community Housing Development Organization) activities if applicable
- For CoC: Align with local CoC Collaborative Applicant's priorities and HUD's Housing First policy
- For Choice Neighborhoods: Document distressed public or HUD-assisted housing, neighborhood transformation strategy

KEY REFERENCES: HUD NOFO, 24 CFR Part 570 (CDBG), 24 CFR Part 92 (HOME), 2 CFR Part 200 (Uniform Guidance)
Reference URL patterns: https://www.hud.gov/program_offices/comm_planning/""",

    "HHS": """HHS grants span ACF, HRSA, SAMHSA, CDC, NIH, and other agencies. Tailor language to the specific office and program.

EVALUATION CRITERIA (ACF/SAMHSA/HRSA programs):
- Need: Epidemiological data, population vulnerability, gaps in current services
- Approach: Evidence-based or evidence-informed models; implementation fidelity
- Capacity: Organizational history, staff qualifications, financial management systems
- Evaluation: Logic model, measurable outcomes, data collection plan
- Partnerships: Community partnerships, MOUs, letters of support

HHS-SPECIFIC REQUIREMENTS:
- Reference the Healthy People 2030 goals aligned with your project
- For SAMHSA: Address co-occurring disorders, trauma-informed care, recovery-oriented systems
- For HRSA: Address health workforce, rural/underserved populations, primary care
- For ACF: Child welfare, Head Start, TANF, family self-sufficiency language
- Address HHS equity plan and disparities data for target population
- Note: HHS programs rarely allow profit/fee; check the specific NOFO

KEY REFERENCES: NOFO requirements, 45 CFR Part 75 (HHS Uniform Admin Requirements), Healthy People 2030""",

    "USDA": """USDA grants (NIFA, RD, AMS, etc.) focus on agriculture, rural development, food systems, and natural resources.

EVALUATION CRITERIA:
- Scientific/Technical Merit (NIFA): Research rigor, innovation, relevance to USDA mission areas
- Community Impact (Rural Development): Economic development, job creation, quality of life in rural areas
- Feasibility: Demonstrated capacity, partnership network, realistic timeline

USDA-SPECIFIC REQUIREMENTS:
- For NIFA: Align with USDA's Agriculture and Food Research Initiative (AFRI) priority areas
- For Rural Development (RD): Document rural eligibility (population <50,000 or per program definition)
- For Community Facilities: Demonstrate essential community service, financial feasibility
- For Business & Industry (B&I): Job creation/retention numbers, local economic impact
- Address climate-smart agriculture and food security where applicable
- Reference Farm Bill authorities for relevant programs

KEY REFERENCES: USDA NIFA grant programs (https://www.nifa.usda.gov), 7 CFR relevant parts""",

    "EPA": """EPA grants (STAG, RCRA, Brownfields, Environmental Justice, etc.) focus on environmental protection and public health.

EVALUATION CRITERIA:
- Environmental Results: Quantifiable environmental and public health improvements
- Technical Merit: Sound scientific approach, innovative methodology
- Community Benefit: Priority for overburdened communities (EJ areas per EJSCREEN)
- Sustainability: Long-term environmental stewardship, institutional capacity

EPA-SPECIFIC REQUIREMENTS:
- For Brownfields: Clearly identify contaminated property, reuse plans, community engagement
- For Environmental Justice: Use EJSCREEN to document community vulnerability; address cumulative impacts
- For Clean Water Act / Safe Drinking Water Act programs: Reference specific statutory authority
- Address EPA's Strategic Plan goal alignment
- Quality Assurance Project Plan (QAPP) may be required for research/monitoring grants
- Intergovernmental Review (E.O. 12372) may apply

KEY REFERENCES: EPA NOFO, 2 CFR Part 200, relevant program regulations (40 CFR)""",

    "DOL": """DOL grants (ETA, OSHA, ODEP, WHD-related) focus on workforce development, worker protection, and employment.

EVALUATION CRITERIA:
- Workforce Need: Labor market data, skills gap documentation, target population needs
- Program Design: Evidence-based training models, employer partnerships, credential attainment
- Capacity: Prior workforce grant performance, staff expertise, partner MOUs
- Outcomes: Job placement rates, wage gains, credential attainment, employer satisfaction

DOL-SPECIFIC REQUIREMENTS:
- For WIOA grants: Align with local Workforce Innovation and Opportunity Act plans
- For Apprenticeship: Address Registered Apprenticeship standards (29 CFR Part 29)
- Reference BLS labor market data to document occupational demand
- Address equity and underserved populations (women, minorities, individuals with disabilities, ex-offenders)
- Performance measures aligned with WIOA common metrics (employment, retention, earnings, credentials)

KEY REFERENCES: WIOA, 29 CFR relevant parts, ETA guidance letters, BLS Occupational Outlook Handbook""",

    "SBA": """SBA programs (SBIR, STTR, 7(a), SCORE, WOSB, etc.) support small business development and innovation.

NOTE: If this is an SBA SBIR/STTR program, use the SBIR-specific guidance. For other SBA programs:

EVALUATION CRITERIA:
- Business Viability: Market opportunity, revenue projections, financial health
- Management Capacity: Relevant experience, team qualifications
- Community Impact: Job creation, underserved community benefit
- Innovation/Competitiveness: What differentiates this business or approach

KEY REFERENCES: SBA program-specific requirements, SBA size standards (13 CFR Part 121)""",

    "EDA": """EDA (Economic Development Administration) grants fund regional economic development and resilience.

EVALUATION CRITERIA:
- Job Creation/Retention: Quantified private investment leverage and jobs supported
- Regional Impact: Alignment with local Comprehensive Economic Development Strategy (CEDS)
- Capacity: Organizational capacity, matching funds, sustainability
- Innovation: Advanced manufacturing, entrepreneurship, technology-based economic development

EDA-SPECIFIC REQUIREMENTS:
- Must align with local EDA-approved CEDS — document this alignment explicitly
- Address EDA's Good Jobs Challenge priorities if applicable
- Matching funds typically required (non-federal match ≥50% for most programs)
- Economic impact analysis with jobs and investment metrics

KEY REFERENCES: EDA NOFO, 13 CFR Part 302, local CEDS""",

    "NEA": """NEA (National Endowment for the Arts) grants support artistic excellence, access, and creative placemaking.

EVALUATION CRITERIA:
- Artistic Excellence: Quality of work, artist credentials, peer recognition
- Artistic Merit: Innovation, distinctive voice, contribution to the field
- Access & Engagement: Reaching underserved communities, geographic diversity
- Impact: Measurable community outcomes, artist development

NEA-SPECIFIC REQUIREMENTS:
- Match requirement: NEA grants require dollar-for-dollar non-federal match
- Address NEA's Strategic Plan goals: Creativity, Strengthening Communities, Arts Education
- Document the art discipline and proposed activities clearly
- Artists or organizations must demonstrate a track record of artistic achievement

KEY REFERENCES: NEA NOFO, 2 CFR Part 200, NEA Strategic Plan""",

    "NSF_NONPROFIT": """NSF Standard Research grants for academic/research institutions emphasize intellectual merit and broader impacts.

See NSF guidance above — the same two criteria (Intellectual Merit + Broader Impacts) apply.
For non-SBIR NSF grants: no commercialization section is needed; embed broader societal impact throughout.""",
}


def _build_ground_truth_block(proposal: Any) -> str:
    """Build the grant selection ground truth block injected into every prompt."""
    lines = ["=== GRANT SELECTION GROUND TRUTH (from wizard Steps 1/2/3) ==="]
    lines.append("These are the authoritative inputs for this proposal. All generated content MUST match these exactly.")

    beneficiary = getattr(proposal, "beneficiary_type", None)
    funder      = getattr(proposal, "funder_class", None)
    program     = getattr(proposal, "program_label", None)
    size        = getattr(proposal, "program_size", None)
    grantor     = getattr(proposal, "grantor_name", None)
    agency      = getattr(proposal, "agency", None) or "OTHER"
    grant_type  = getattr(proposal, "grant_type", None) or "federal_other"

    lines.append(f"• Step 1 — Applicant Type: {beneficiary or '(not specified)'}")
    lines.append(f"• Step 2 — Funder Class: {funder or '(not specified)'}")
    lines.append(f"• Step 3 — Agency / Funder: {agency}")
    if program:
        lines.append(f"• Grant Program: {program}")
    if size:
        lines.append(f"• Typical Award Size: {size}")
    if grantor:
        lines.append(f"• Grantor / Funder Name: {grantor}")
    lines.append(f"• Grant Type Code: {grant_type}")
    lines.append("")
    lines.append("OVERRIDE RULE: If the applicant has entered any information above, it takes precedence over")
    lines.append("any defaults or assumptions. Do NOT call this an 'SBIR' proposal unless grant_type is 'sbir' or 'sttr'.")
    return "\n".join(lines)


def _build_pi_block(profile: dict, grant_type: str = "federal_other") -> str:
    is_sbir = grant_type in ("sbir", "sttr")
    pi_name = profile.get("pi_name") or "[MISSING: PI Name — required for this proposal]"
    pi_degree = profile.get("pi_degree") or "[MISSING: PI degree/credentials]"
    pi_affil = profile.get("pi_affiliation") or ""
    pi_creds = profile.get("pi_credentials") or "[MISSING: PI background/credentials narrative]"
    pi_pubs = profile.get("pi_publications")
    pi_awards = profile.get("pi_prior_sbir_awards")

    block = f"Principal Investigator / Lead: {pi_name}, {pi_degree}"
    if pi_affil:
        block += f" ({pi_affil})"
    block += f"\nBackground: {pi_creds}"
    if pi_pubs is not None:
        block += f"\nPublications: {pi_pubs} peer-reviewed publications"
    if pi_awards is not None and is_sbir:
        block += f"\nPrior Federal R&D Awards: {pi_awards}"
    return block


def _build_team_block(profile: dict) -> str:
    members = profile.get("team_members") or []
    if not members:
        return "[MISSING: Key Personnel — add team members in Company Profile to personalize this section]"
    lines = []
    for m in members:
        name = m.get("name", "Unknown")
        title = m.get("title", "")
        role = m.get("role", "")
        creds = m.get("credentials", "")
        effort = m.get("effort_pct", 0)
        yrs = m.get("years_exp", 0)
        line = f"- {name}"
        if title:
            line += f", {title}"
        if role:
            line += f" ({role})"
        if creds:
            line += f": {creds}"
        if effort:
            line += f" — {effort}% effort"
        if yrs:
            line += f", {yrs} years experience"
        lines.append(line)
    return "Key Personnel:\n" + "\n".join(lines)


def _build_facilities_block(profile: dict) -> str:
    facilities = profile.get("facilities") or []
    if not facilities:
        return "[MISSING: Facilities description — add lab/equipment details in Company Profile]"
    lines = []
    for f in facilities:
        name = f.get("name", "Facility")
        ftype = f.get("type", "")
        desc = f.get("description", "")
        certs = f.get("certifications") or []
        sqft = f.get("sq_footage")
        loc = f.get("location", "")
        line = f"- {name}"
        if ftype:
            line += f" ({ftype})"
        if sqft:
            line += f", {sqft:,} sq ft"
        if loc:
            line += f", {loc}"
        if desc:
            line += f": {desc}"
        if certs:
            line += f". Certifications: {', '.join(certs)}"
        lines.append(line)
    return "Facilities & Equipment:\n" + "\n".join(lines)


def _build_partners_block(profile: dict, phase: str, agency: str, grant_type: str = "federal_other") -> str:
    partners = profile.get("partners") or []
    is_sttr = grant_type == "sttr"
    has_research_inst = any(
        p.get("type") in ("research_institution",) for p in partners
    )

    lines = []
    if is_sttr and not has_research_inst:
        lines.append("[MISSING: Research Institution Partner — REQUIRED for STTR (≥40% of effort must go to the research institution)]")

    if not partners:
        if not lines:
            return "No external partners identified."
        return "\n".join(lines)

    for p in partners:
        name = p.get("name", "Partner")
        ptype = p.get("type", "")
        role = p.get("role", "")
        pi = p.get("pi_name", "")
        effort = p.get("effort_pct", 0)
        line = f"- {name}"
        if ptype:
            line += f" ({ptype.replace('_', ' ').title()})"
        if pi:
            line += f", PI: {pi}"
        if role:
            line += f" — Role: {role}"
        if effort:
            line += f" ({effort}% effort)"
        lines.append(line)
    return "Partners & Collaborators:\n" + "\n".join(lines)


def _build_past_performance_block(profile: dict) -> str:
    awards = profile.get("past_performance") or []
    if not awards:
        return "No prior federal awards listed. This is the organization's first application for this program area."
    lines = ["Prior Grant / Award Performance:"]
    for a in awards:
        title = a.get("title", "Project")
        agency = a.get("agency", "")
        num = a.get("award_number", "")
        amt = a.get("amount", "")
        period = a.get("period", "")
        outcome = a.get("outcome", "")
        relevance = a.get("relevance", "")
        line = f"- {title}"
        if agency:
            line += f" ({agency}"
            if num:
                line += f" #{num}"
            line += ")"
        if amt:
            line += f", {amt}"
        if period:
            line += f", {period}"
        if outcome:
            line += f" — {outcome}"
        if relevance:
            line += f". Relevance: {relevance}"
        lines.append(line)
    return "\n".join(lines)


def _build_capabilities_block(profile: dict) -> str:
    caps = profile.get("company_capabilities") or ""
    techs = profile.get("core_technologies") or []
    industry = profile.get("industry") or ""
    if not caps and not techs:
        return "[MISSING: Company capabilities narrative — describe your core competencies in Company Profile]"
    block = ""
    if caps:
        block += f"Core Competencies: {caps}"
    if techs:
        block += f"\nKey Technologies: {', '.join(techs)}"
    if industry:
        block += f"\nIndustry: {industry}"
    return block


def _select_profile_context(section_id: str, section_title: str, profile: dict, phase: str, agency: str, grant_type: str = "federal_other") -> str:
    """Build the profile context block relevant to this specific section."""
    sid = section_id.lower()
    stitle = section_title.lower()
    combined = sid + " " + stitle

    active_keys: set = set()
    for keyword, keys in SECTION_PROFILE_KEYS.items():
        if keyword in combined:
            active_keys.update(keys)

    # Default: always include PI and capabilities
    if not active_keys:
        active_keys = {"pi", "company_capabilities"}

    blocks = []
    if "pi" in active_keys:
        blocks.append(_build_pi_block(profile, grant_type))
    if "team_members" in active_keys:
        blocks.append(_build_team_block(profile))
    if "facilities" in active_keys:
        blocks.append(_build_facilities_block(profile))
    if "partners" in active_keys:
        blocks.append(_build_partners_block(profile, phase, agency, grant_type))
    if "past_performance" in active_keys:
        blocks.append(_build_past_performance_block(profile))
    if "company_capabilities" in active_keys:
        blocks.append(_build_capabilities_block(profile))

    return "\n\n".join(b for b in blocks if b)


# ── Content sanitizer ─────────────────────────────────────────────────────────

_SBIR_PATTERNS = [
    # "SBIR proposal" / "SBIR Phase I proposal" / "SBIR/STTR" etc.
    (re.compile(r'\bSBIR[/\s-]*STTR\b', re.IGNORECASE), "{prog}"),
    (re.compile(r'\bSBIR\s+Phase\s+[I1]+\b', re.IGNORECASE), "{prog}"),
    (re.compile(r'\bSBIR\s+Phase\s+[I2]+\b', re.IGNORECASE), "{prog}"),
    (re.compile(r'\bPhase\s+[I1]+\s+SBIR\b', re.IGNORECASE), "{prog}"),
    (re.compile(r'\bPhase\s+[I2]+\s+SBIR\b', re.IGNORECASE), "{prog}"),
    (re.compile(r'\bSBIR\s+proposal\b', re.IGNORECASE), "{prog} proposal"),
    (re.compile(r'\bSBIR\s+grant\b', re.IGNORECASE), "{prog} grant"),
    (re.compile(r'\bSBIR\s+guidelines\b', re.IGNORECASE), "{prog} guidelines"),
    (re.compile(r'\bSBIR\s+requirements\b', re.IGNORECASE), "{prog} requirements"),
    (re.compile(r'\bSBIR\s+format\b', re.IGNORECASE), "{prog} format"),
    (re.compile(r'\bSBIR\s+criteria\b', re.IGNORECASE), "{prog} criteria"),
    (re.compile(r'\bSBIR\s+program\b', re.IGNORECASE), "{prog} program"),
    (re.compile(r'\bSBIR\s+funding\b', re.IGNORECASE), "{prog} funding"),
    (re.compile(r'\bSBIR\s+award\b', re.IGNORECASE), "{prog} award"),
    (re.compile(r'\badhering\s+to\s+SBIR\b', re.IGNORECASE), "adhering to {prog}"),
    (re.compile(r'\bunder\s+SBIR\b', re.IGNORECASE), "under {prog}"),
    # Standalone "SBIR" only when not part of a legitimate section name
    (re.compile(r'\bSBIR\b(?!\s*/\s*STTR)', re.IGNORECASE), "{prog}"),
]


def _sanitize_sbir_content(content: str, grant_type: str, grant_label: str) -> str:
    """
    Post-process generated content to strip SBIR/STTR references for non-SBIR grants.
    This is a defense-in-depth measure — the prompts should prevent this, but the
    AI sometimes ignores instructions. For SBIR/STTR proposals, this is a no-op.
    """
    if grant_type in ("sbir", "sttr"):
        return content  # SBIR/STTR — keep all references as-is

    # Use a short program name for replacements
    short_label = grant_label if len(grant_label) < 60 else (grant_label[:55] + "...")
    replacement_map = {"prog": short_label}

    result = content
    for pattern, template in _SBIR_PATTERNS:
        replacement = template.format(**replacement_map)
        result = pattern.sub(replacement, result)
    return result


_log = logging.getLogger(__name__)


def _ai_gen_error(exc: Exception) -> HTTPException:
    """Convert an OpenAI exception into a generic HTTPException (no provider names exposed)."""
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
    _log.error("Unexpected generation error:\n%s", traceback.format_exc())
    return HTTPException(status_code=500, detail="Content generation failed. Please try again or contact support.")


# ── Engine ────────────────────────────────────────────────────────────────────

class ProposalGeneratorEngine:
    """
    Generates proposal section content using GPT-4o.
    Injects company-specific context (PI, team, facilities, partners, past performance)
    and flags missing information with [MISSING: ...] placeholders.
    """

    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    async def generate_section(
        self,
        section_id: str,
        section_title: str,
        proposal: Any,
        company_profile: Optional[Dict] = None,
        additional_context: Optional[str] = None,
        guidance: str = "",
        target_words: int = 0,
    ) -> Dict[str, Any]:
        """Generate content for a single section with full company context injection."""

        profile    = company_profile or {}
        agency     = getattr(proposal, "agency", None) or "OTHER"
        phase      = getattr(proposal, "phase",  None) or "phase_i"
        grant_type = getattr(proposal, "grant_type", None) or "federal_other"
        program    = getattr(proposal, "program_label", None) or ""
        prog_size  = getattr(proposal, "program_size",  None) or ""

        # Human-readable grant label for prompts
        from engines.grant_templates import get_grant_type as _gt
        grant_label = program or _gt(grant_type).get("label", grant_type.replace("_", " ").title())

        # Word count target
        tw = target_words or WORD_TARGETS.get(section_id.lower(), 600)

        # Agency-specific guidance — check both agency code and program-derived key
        agency_note = (
            AGENCY_GUIDANCE.get(agency)
            or AGENCY_GUIDANCE.get(agency.upper())
            or f"Follow the evaluation criteria and formatting requirements for the {grant_label} program. "
               f"Do NOT use SBIR/STTR language unless this is literally an SBIR or STTR grant."
        )

        # Build section-specific company profile context
        profile_block = _select_profile_context(section_id, section_title, profile, phase, agency, grant_type)

        org_name = profile.get("organization_name") or "our organization"

        # Ground truth block
        ground_truth = _build_ground_truth_block(proposal)

        user_prompt = f"""Write the "{section_title}" section for a {grant_label} proposal.

Organization: {org_name}
{f"Award size: {prog_size}" if prog_size else ""}

{ground_truth}

=== APPLICANT PROFILE (use this information — do not invent details not listed here) ===
{profile_block}

=== PROPOSAL CONTEXT ===
Research / Project Focus: {proposal.research_focus or "[MISSING: Research focus not defined]"}
Innovation / Approach: {proposal.innovation_description or "[MISSING: Innovation description not defined]"}
{f"Commercialization Plan: {proposal.commercialization_plan}" if proposal.commercialization_plan else ""}

=== PROGRAM-SPECIFIC GUIDANCE ===
{agency_note}

=== SECTION-SPECIFIC GUIDANCE ===
{guidance or f"Write a compelling, complete {section_title} section appropriate for {grant_label}."}

{f"Additional context from applicant: {additional_context}" if additional_context else ""}

IMPORTANT:
- Keep any [MISSING: ...] placeholders exactly as written if the required information was not provided above.
- Write approximately {tw} words of rich, specific content.
- Where you use facts about this grant program (regulations, eligibility rules, priorities), cite the source inline.
"""

        try:
            response = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": _build_system_prompt(grant_type, program)},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=0.7,
                max_tokens=2500,
            )
        except Exception as exc:
            raise _ai_gen_error(exc)

        content = response.choices[0].message.content.strip()
        content = _sanitize_sbir_content(content, grant_type, grant_label)
        word_count    = len(re.findall(r"\w+", content))
        page_estimate = word_count / 250

        # Extract [MISSING: ...] tags as compliance flags
        missing_flags = re.findall(r"\[MISSING:[^\]]+\]", content)

        return {
            "content":       content,
            "word_count":    word_count,
            "page_estimate": round(page_estimate, 2),
            "missing_flags": missing_flags,
        }

    async def regenerate_section(
        self,
        section_id: str,
        section_title: str,
        current_content: str,
        proposal: Any,
        company_profile: Optional[Dict] = None,
        feedback: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Regenerate a section incorporating reviewer feedback."""
        profile    = company_profile or {}
        agency     = getattr(proposal, "agency",     None) or "OTHER"
        phase      = getattr(proposal, "phase",      None) or "phase_i"
        grant_type = getattr(proposal, "grant_type", None) or "federal_other"
        program    = getattr(proposal, "program_label", None) or ""
        org_name   = profile.get("organization_name") or "our organization"
        profile_block = _select_profile_context(section_id, section_title, profile, phase, agency, grant_type)

        from engines.grant_templates import get_grant_type as _gt
        grant_label = program or _gt(grant_type).get("label", grant_type.replace("_", " ").title())
        ground_truth = _build_ground_truth_block(proposal)

        fb_note = f"\n\nReviewer feedback to address:\n{feedback}" if feedback else ""

        prompt = f"""Improve the following "{section_title}" section for a {grant_label} proposal by {org_name}.

{ground_truth}

Current content:
{current_content}
{fb_note}

Applicant Profile:
{profile_block}

Research / Project Focus: {proposal.research_focus or ""}

Rewrite with stronger arguments, more specific details, and improved rigor for this specific grant program.
Keep any [MISSING: ...] placeholders if the required information was not provided.
Do NOT introduce SBIR language unless the grant type is literally SBIR or STTR.
"""
        try:
            response = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": _build_system_prompt(grant_type, program)},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.6,
                max_tokens=2500,
            )
        except Exception as exc:
            raise _ai_gen_error(exc)

        content      = response.choices[0].message.content.strip()
        content      = _sanitize_sbir_content(content, grant_type, grant_label)
        word_count   = len(re.findall(r"\w+", content))
        missing_flags = re.findall(r"\[MISSING:[^\]]+\]", content)

        return {
            "content":       content,
            "word_count":    word_count,
            "page_estimate": round(word_count / 250, 2),
            "missing_flags": missing_flags,
        }
