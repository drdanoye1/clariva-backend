"""
Funding Opportunity Fit Score (FOFS) engine — Funding Opportunity
Intelligence, Phase 2 ("Organization-Specific Matching, Ranking &
Decision Intelligence").

Deliberately NOT an LLM call: every score below is deterministic, plain
Python logic over fields already on FOARecord (including, when present,
the paid Intelligence Report's cached classifications) and OrgContextDB
(the Funding Intelligence Profile). That means every point is explainable
in plain language and reproducible on demand — no black-box number. Per
the product spec, this must never imply a win probability ("73% likely to
win"); it only expresses how well an opportunity's *stated facts* line up
with the organization's *stated profile*. It is one input to a human
decision, never the decision itself (see foa_parser.py's
HUMAN_IN_THE_LOOP_NOTE — the same principle applies here).

Categories and weights (spec, Phase 2):
    Eligibility                     25%
    Strategic / Mission Alignment   20%
    Capability Fit                  15%
    Applicant / Geographic Fit      10%
    Funding Fit                     10%
    Readiness                       10%
    Timing / Resource Feasibility   10%
                                    ----
                                    100%

Grading degrades gracefully: a category with nothing to compare (no
profile field filled in, or no opportunity text available — e.g. the paid
Intelligence Report hasn't been run yet) scores neutral (50) rather than
being penalized, and its explanation says exactly what's missing. An
incomplete profile should look unscored, never like a bad fit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from models.db_models import FOARecord, OrgContextDB

CATEGORY_WEIGHTS: Dict[str, float] = {
    "eligibility":              0.25,
    "strategic_alignment":      0.20,
    "capability_fit":           0.15,
    "applicant_geographic_fit": 0.10,
    "funding_fit":              0.10,
    "readiness":                0.10,
    "timing_feasibility":       0.10,
}

CATEGORY_LABELS: Dict[str, str] = {
    "eligibility":              "Eligibility",
    "strategic_alignment":      "Strategic / Mission Alignment",
    "capability_fit":           "Capability Fit",
    "applicant_geographic_fit": "Applicant / Geographic Fit",
    "funding_fit":              "Funding Fit",
    "readiness":                "Readiness",
    "timing_feasibility":       "Timing / Resource Feasibility",
}

NEUTRAL_SCORE = 50
NEUTRAL_SUFFIX = " Add more detail to your Funding Intelligence Profile to sharpen this category."


@dataclass
class CategoryScore:
    key: str
    label: str
    weight: float
    score: int  # 0-100, this category alone
    explanation: str

    @property
    def weighted_points(self) -> float:
        return self.score * self.weight

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "weight": self.weight,
            "score": self.score,
            "weighted_points": round(self.weighted_points, 2),
            "explanation": self.explanation,
        }


@dataclass
class FitScoreResult:
    overall_score: int  # 0-100
    bucket: str  # Strong Match | Good Match | Possible Match | Low-Priority Match
    recommendation: str  # STRONG PURSUE | PURSUE | CONDITIONAL PURSUE | LOW PRIORITY | NO-GO
    recommendation_reason: str
    categories: List[CategoryScore] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall_score": self.overall_score,
            "bucket": self.bucket,
            "recommendation": self.recommendation,
            "recommendation_reason": self.recommendation_reason,
            "categories": [c.to_dict() for c in self.categories],
        }


def _neutral(key: str, explanation: str) -> CategoryScore:
    return CategoryScore(key, CATEGORY_LABELS[key], CATEGORY_WEIGHTS[key], NEUTRAL_SCORE, explanation + NEUTRAL_SUFFIX)


def _report(record: FOARecord) -> Dict[str, Any]:
    return record.intelligence_report or {}


def _text_blob(*parts: Optional[str]) -> str:
    return " \n ".join(str(p) for p in parts if p).lower()


def _terms_from_alignment_profile(profile: OrgContextDB) -> List[str]:
    """Mission/industry-facing terms — used for Strategic Alignment."""
    terms: List[str] = []
    for v in [profile.industry, profile.mission_statement]:
        if v:
            terms.append(str(v).lower())
    for lst in [profile.industries, profile.core_technologies]:
        for v in (lst or []):
            if v:
                terms.append(str(v).lower())
    return terms


def _terms_from_capability_profile(profile: OrgContextDB) -> List[str]:
    """Capability/technical-delivery terms — used for Capability Fit."""
    terms: List[str] = []
    if profile.company_capabilities:
        terms.append(str(profile.company_capabilities).lower())
    for v in (profile.core_technologies or []):
        if v:
            terms.append(str(v).lower())
    for pp in (profile.past_performance or []):
        if isinstance(pp, dict):
            for key in ("title", "name", "summary", "description"):
                if pp.get(key):
                    terms.append(str(pp[key]).lower())
        elif pp:
            terms.append(str(pp).lower())
    return terms


def _keyword_overlap(profile_terms: List[str], text: str) -> Optional[Tuple[int, List[str]]]:
    """
    Case-insensitive substring overlap between profile terms and opportunity
    text. Returns None (meaning "can't score this, not enough data on one
    side") rather than a score of 0, so a missing profile field or missing
    opportunity text is never mistaken for "no fit."
    """
    if not profile_terms or not text:
        return None
    matched = list(dict.fromkeys(t.strip() for t in profile_terms if t and len(t.strip()) >= 3 and t.strip() in text))
    n = len(matched)
    score = {0: 30, 1: 60, 2: 78}.get(n, 90)
    return score, matched


def _score_eligibility(record: FOARecord, profile: OrgContextDB) -> CategoryScore:
    report = _report(record)
    elig = report.get("eligibility_assessment") or {}
    status = record.eligibility_status or elig.get("status")

    if status:
        status_l = status.lower()
        if status_l == "eligible":
            score, why = 95, "The Intelligence Report classifies this organization type as Eligible."
        elif status_l == "conditional":
            score, why = 60, "The Intelligence Report classifies eligibility as Conditional — some requirements may not be met yet."
        elif status_l == "unlikely":
            score, why = 10, "The Intelligence Report classifies this organization type as Unlikely to be eligible."
        else:  # Requires Verification
            score, why = 50, "The Intelligence Report could not confirm eligibility from the solicitation text alone — verify manually."
        issues = elig.get("issues") or []
        if issues:
            why += f" Open issues: {'; '.join(str(i) for i in issues[:3])}."
        return CategoryScore("eligibility", CATEGORY_LABELS["eligibility"], CATEGORY_WEIGHTS["eligibility"], score, why)

    # No paid Intelligence Report yet — light keyword check against entity_type/certifications.
    elig_text = (record.eligibility_summary or "").lower()
    if not elig_text:
        return _neutral("eligibility", "No eligibility text is available yet for this opportunity (run the Intelligence Report for a confirmed reading).")
    signals = []
    if profile.entity_type and profile.entity_type.lower() in elig_text:
        signals.append(profile.entity_type)
    for cert in (profile.certifications or []):
        if cert and str(cert).lower() in elig_text:
            signals.append(str(cert))
    if signals:
        return CategoryScore("eligibility", CATEGORY_LABELS["eligibility"], CATEGORY_WEIGHTS["eligibility"], 75,
                              f"The eligibility text mentions your organization's {', '.join(signals)}.")
    return _neutral("eligibility", "Could not confirm eligibility from the available text against your profile.")


def _score_strategic_alignment(record: FOARecord, profile: OrgContextDB) -> CategoryScore:
    report = _report(record)
    priorities = report.get("funding_priorities") or []
    text = _text_blob(record.program_title, record.agency, " ".join(str(p) for p in priorities), record.ai_summary)
    terms = _terms_from_alignment_profile(profile)
    result = _keyword_overlap(terms, text)
    if result is None:
        if not terms:
            return _neutral("strategic_alignment", "Add a mission statement, industries, or core technologies to your profile to score strategic alignment.")
        return _neutral("strategic_alignment", "Not enough opportunity text is available yet to compare against your mission/industries.")
    score, matched = result
    why = (f"Overlap found between this opportunity and your profile's mission/industries/technologies: {', '.join(matched[:4])}."
           if matched else
           "No overlap found between this opportunity's stated focus and your profile's mission, industries, or core technologies.")
    return CategoryScore("strategic_alignment", CATEGORY_LABELS["strategic_alignment"], CATEGORY_WEIGHTS["strategic_alignment"], score, why)


def _score_capability_fit(record: FOARecord, profile: OrgContextDB) -> CategoryScore:
    report = _report(record)
    requirements = report.get("requirements") or []
    text = _text_blob(record.program_title, record.eligibility_summary, " ".join(str(r) for r in requirements), record.ai_summary)
    terms = _terms_from_capability_profile(profile)
    result = _keyword_overlap(terms, text)
    if result is None:
        if not terms:
            return _neutral("capability_fit", "Add core technologies, capabilities, or past performance to your profile to score capability fit.")
        return _neutral("capability_fit", "Not enough opportunity text is available yet to compare against your capabilities.")
    score, matched = result
    why = (f"Overlap found between this opportunity's requirements and your capabilities/past performance: {', '.join(matched[:4])}."
           if matched else
           "No overlap found between this opportunity's stated requirements and your profile's capabilities or past performance.")
    return CategoryScore("capability_fit", CATEGORY_LABELS["capability_fit"], CATEGORY_WEIGHTS["capability_fit"], score, why)


def _score_applicant_geographic_fit(record: FOARecord, profile: OrgContextDB) -> CategoryScore:
    report = _report(record)
    elig = report.get("eligibility_assessment") or {}
    text = _text_blob(record.eligibility_summary, elig.get("explanation"))
    if not text:
        return _neutral("applicant_geographic_fit", "No eligibility/applicant-type text is available yet to compare against your service geography.")

    geo_hits = [g for g in (profile.service_geography or []) if g and str(g).strip().lower() in text]
    entity_hit = profile.entity_type if (profile.entity_type and profile.entity_type.lower() in text) else None

    if geo_hits:
        return CategoryScore("applicant_geographic_fit", CATEGORY_LABELS["applicant_geographic_fit"],
                              CATEGORY_WEIGHTS["applicant_geographic_fit"], 85,
                              f"Opportunity text references your service area: {', '.join(str(g) for g in geo_hits)}.")
    if entity_hit:
        return CategoryScore("applicant_geographic_fit", CATEGORY_LABELS["applicant_geographic_fit"],
                              CATEGORY_WEIGHTS["applicant_geographic_fit"], 75,
                              f"Opportunity text references your organization type ({entity_hit}).")
    return CategoryScore("applicant_geographic_fit", CATEGORY_LABELS["applicant_geographic_fit"],
                          CATEGORY_WEIGHTS["applicant_geographic_fit"], 65,
                          "No geographic or applicant-type restriction was found in the available text — likely open nationally, but verify against the full solicitation.")


def _score_funding_fit(record: FOARecord, profile: OrgContextDB) -> CategoryScore:
    prefs = profile.funding_preferences or {}
    min_award = prefs.get("min_award")
    max_award = prefs.get("max_award")
    preferred_agencies = [a.lower() for a in (prefs.get("preferred_agencies") or []) if a]
    preferred_phase = prefs.get("preferred_phase")

    amount = record.estimated_award_ceiling if record.estimated_award_ceiling is not None else record.estimated_award_floor

    if min_award is None and max_award is None and not preferred_agencies and not preferred_phase:
        return _neutral("funding_fit", "No funding-size, agency, or phase preferences are set in your profile.")
    if amount is None and not preferred_agencies:
        return _neutral("funding_fit", "This opportunity does not state an award amount.")

    score, reasons = 70, []
    if amount is not None:
        if min_award is not None and amount < min_award:
            score, reasons = 30, [f"award amount (${amount:,.0f}) is below your stated minimum (${min_award:,.0f})"]
        elif max_award is not None and amount > max_award:
            score, reasons = 55, [f"award amount (${amount:,.0f}) exceeds your stated maximum (${max_award:,.0f}) — may require more capacity than typical for your organization"]
        elif min_award is not None or max_award is not None:
            score, reasons = 90, [f"award amount (${amount:,.0f}) falls within your stated funding range"]

    if preferred_agencies and record.agency and record.agency.lower() in preferred_agencies:
        score = min(100, score + 10)
        reasons.append(f"{record.agency} is one of your preferred funding agencies")
    if preferred_phase and record.phase and preferred_phase.lower().replace("_", " ") in record.phase.lower():
        score = min(100, score + 5)
        reasons.append(f"matches your preferred phase ({record.phase})")

    why = "; ".join(reasons).capitalize() + "." if reasons else "No specific funding-fit signal found for this opportunity."
    return CategoryScore("funding_fit", CATEGORY_LABELS["funding_fit"], CATEGORY_WEIGHTS["funding_fit"], score, why)


def _score_readiness(record: FOARecord, profile: OrgContextDB) -> CategoryScore:
    if not record.deadline:
        return _neutral("readiness", "No deadline is listed for this opportunity yet.")

    deadline = record.deadline if record.deadline.tzinfo else record.deadline.replace(tzinfo=timezone.utc)
    days_left = (deadline - datetime.now(timezone.utc)).days

    if days_left < 0:
        score, why = 5, "This opportunity's deadline has already passed."
    elif days_left < 14:
        score, why = 30, f"Only {days_left} day(s) remain before the deadline — a tight timeline for a competitive proposal."
    elif days_left < 30:
        score, why = 55, f"{days_left} days remain — workable, but a compressed timeline."
    elif days_left < 60:
        score, why = 80, f"{days_left} days remain — reasonable lead time to prepare a proposal."
    else:
        score, why = 95, f"{days_left} days remain — ample lead time to prepare a competitive proposal."

    report = _report(record)
    cost_share = report.get("cost_share") or {}
    tolerance = (profile.funding_preferences or {}).get("cost_share_tolerance")
    if cost_share.get("required") and tolerance == "none":
        score = max(10, score - 30)
        why += " This opportunity requires cost-share/match, which your profile marks as unwanted."

    return CategoryScore("readiness", CATEGORY_LABELS["readiness"], CATEGORY_WEIGHTS["readiness"], score, why)


def _score_timing_feasibility(record: FOARecord, profile: OrgContextDB) -> CategoryScore:
    report = _report(record)
    complexity = record.complexity or (report.get("complexity") or {}).get("level")
    if not complexity:
        return _neutral("timing_feasibility", "Complexity has not been assessed yet for this opportunity (run the Intelligence Report).")

    level = complexity.lower()
    base = {"low": 90, "moderate": 70, "high": 50}.get(level, 30)  # "very high" and unknown -> 30
    why = f"Opportunity complexity is rated {complexity}."

    team_size = len(profile.team_members or [])
    experienced = bool(profile.prior_sbir_experience) or bool(profile.past_performance)
    if level in ("high", "very high"):
        if experienced:
            base = min(100, base + 10)
            why += " Your organization's prior award history supports taking on higher-complexity work."
        elif team_size < 3:
            base = max(0, base - 15)
            why += " Your profile shows a small team and no prior award history for a project at this complexity level."

    return CategoryScore("timing_feasibility", CATEGORY_LABELS["timing_feasibility"], CATEGORY_WEIGHTS["timing_feasibility"], base, why)


def _bucket(overall: int) -> str:
    if overall >= 80:
        return "Strong Match"
    if overall >= 60:
        return "Good Match"
    if overall >= 40:
        return "Possible Match"
    return "Low-Priority Match"


def _recommendation(overall: int, eligibility_score: int) -> Tuple[str, str]:
    # Eligibility is a near-hard gate: a very poor eligibility read overrides
    # an otherwise decent overall score — pursuing something you likely
    # can't legally win is never the right call regardless of alignment.
    if eligibility_score <= 20:
        return ("NO-GO", "Eligibility appears very unlikely based on the available information — "
                         "pursuing this opportunity is not recommended without a major eligibility change.")
    if overall >= 80:
        return ("STRONG PURSUE", "This opportunity scores well across eligibility, strategic fit, and capability — a strong candidate for pursuit.")
    if overall >= 65:
        return ("PURSUE", "This opportunity is a solid fit overall; pursuing it is reasonable.")
    if overall >= 45:
        return ("CONDITIONAL PURSUE", "This opportunity has real strengths but also gaps worth resolving before committing resources.")
    if overall >= 25:
        return ("LOW PRIORITY", "This opportunity has limited fit; pursue only if capacity allows after higher-fit opportunities.")
    return ("NO-GO", "This opportunity has very limited fit with your organization's profile.")


def score_opportunity(record: FOARecord, profile: Optional[OrgContextDB]) -> Optional[FitScoreResult]:
    """
    Compute the Funding Opportunity Fit Score for one record against one
    Funding Intelligence Profile. Returns None if there is no profile to
    score against at all (personal use with no Company Profile filled in,
    or an org with none saved yet) — callers should omit fit fields
    entirely in that case rather than showing a misleading default score.
    """
    if profile is None:
        return None

    categories = [
        _score_eligibility(record, profile),
        _score_strategic_alignment(record, profile),
        _score_capability_fit(record, profile),
        _score_applicant_geographic_fit(record, profile),
        _score_funding_fit(record, profile),
        _score_readiness(record, profile),
        _score_timing_feasibility(record, profile),
    ]
    overall = round(sum(c.weighted_points for c in categories))
    overall = max(0, min(100, overall))
    recommendation, reason = _recommendation(overall, categories[0].score)

    return FitScoreResult(
        overall_score=overall,
        bucket=_bucket(overall),
        recommendation=recommendation,
        recommendation_reason=reason,
        categories=categories,
    )
