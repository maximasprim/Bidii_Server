"""
ATS scoring engine.

Deliberately dependency-free (no CV/PDF text extraction) so the ATS module
stays isolated and safe to deploy incrementally: it evaluates each
criterion's `match_keywords` (case-insensitive substring match) against the
text an applicant already gives us at submission time — their cover note
and the role/title they applied for. This is an MVP scoring strategy; a
criterion with no match_keywords configured is simply never matched, which
is a reasonable, honest default (there's nothing to auto-evaluate it
against) rather than guessing.

Nothing here touches CareerApplication.status except the one explicit,
opt-in auto-reject path described below.
"""

from dataclasses import dataclass, field

from app.models.ats import ATSConfiguration, ATSCriterion, ATSRecommendation
from app.models.career_application import CareerApplication


@dataclass
class ScoringOutcome:
    total_score: float
    max_possible_score: float
    score_percentage: float
    recommendation: ATSRecommendation
    matched: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    failed_mandatory: list[dict] = field(default_factory=list)
    should_auto_reject: bool = False


def _criterion_outcome_dict(criterion: ATSCriterion) -> dict:
    return {
        "criterion_id": criterion.id,
        "label": criterion.label,
        "category": criterion.category.value if hasattr(criterion.category, "value") else criterion.category,
        "weight": criterion.weight,
        "is_required": criterion.is_required,
    }


def _searchable_text(application: CareerApplication) -> str:
    """
    The text an application is screened against. Kept to fields supplied
    directly by the applicant at submission time — cover_note and the
    resolved role — so this never depends on parsing the uploaded CV file.
    """
    parts = [application.role or "", application.cover_note or ""]
    return " \n ".join(parts).lower()


def _criterion_matches(criterion: ATSCriterion, text: str) -> bool:
    keywords = criterion.match_keywords or []
    if not keywords:
        return False
    return any(str(keyword).strip().lower() in text for keyword in keywords if str(keyword).strip())


def score_application(application: CareerApplication, config: ATSConfiguration) -> ScoringOutcome:
    text = _searchable_text(application)
    criteria: list[ATSCriterion] = list(config.criteria)

    matched: list[dict] = []
    missing: list[dict] = []
    failed_mandatory: list[dict] = []

    total_score = 0.0
    max_possible_score = 0.0

    for criterion in criteria:
        max_possible_score += criterion.weight
        outcome = _criterion_outcome_dict(criterion)
        if _criterion_matches(criterion, text):
            total_score += criterion.weight
            matched.append(outcome)
        else:
            missing.append(outcome)
            if criterion.is_required:
                failed_mandatory.append(outcome)

    score_percentage = (total_score / max_possible_score * 100) if max_possible_score > 0 else 0.0

    if failed_mandatory:
        recommendation = ATSRecommendation.not_recommended
    elif score_percentage >= config.minimum_recommend_score:
        recommendation = ATSRecommendation.recommended
    elif score_percentage < config.minimum_review_score:
        recommendation = ATSRecommendation.not_recommended
    else:
        recommendation = ATSRecommendation.review

    should_auto_reject = bool(config.auto_reject_enabled and failed_mandatory)

    return ScoringOutcome(
        total_score=round(total_score, 2),
        max_possible_score=round(max_possible_score, 2),
        score_percentage=round(score_percentage, 2),
        recommendation=recommendation,
        matched=matched,
        missing=missing,
        failed_mandatory=failed_mandatory,
        should_auto_reject=should_auto_reject,
    )
