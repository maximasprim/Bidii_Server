"""
ATS scoring engine.

Evaluates each criterion's `match_keywords` (case-insensitive substring
match) against the text an applicant supplies. Originally this was
deliberately limited to submission-time fields (cover note, applied-for
role) to keep the module dependency-free and I/O-free — but AI-suggested
criteria (see app/services/ai_criteria_suggestion.py) draft keywords from
the job's own requirements, which a candidate's CV is far more likely to
literally contain than a short cover note. So this module now also
matches against CV text when the caller has it — extraction itself
still happens elsewhere (app/services/cv_text_extraction.py, called from
the screening router) so this module stays I/O-free and easy to test in
isolation; it just accepts the already-extracted text as a plain string.
A criterion with no match_keywords configured is simply never matched,
which is a reasonable, honest default (there's nothing to auto-evaluate
it against) rather than guessing.

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
    cv_text_used: bool = False


def _criterion_outcome_dict(criterion: ATSCriterion) -> dict:
    return {
        "criterion_id": criterion.id,
        "label": criterion.label,
        "category": criterion.category.value if hasattr(criterion.category, "value") else criterion.category,
        "weight": criterion.weight,
        "is_required": criterion.is_required,
    }


def _searchable_text(application: CareerApplication, cv_text: str | None) -> str:
    """
    The text an application is screened against: the applicant's
    submission-time fields (role, cover note) plus their CV text, when the
    caller was able to extract it. cv_text is optional and best-effort —
    a candidate whose CV couldn't be read still gets scored on whatever
    text is available, same as before this method existed.
    """
    parts = [application.role or "", application.cover_note or "", cv_text or ""]
    return " \n ".join(parts).lower()


def _criterion_matches(criterion: ATSCriterion, text: str) -> bool:
    keywords = criterion.match_keywords or []
    if not keywords:
        return False
    return any(str(keyword).strip().lower() in text for keyword in keywords if str(keyword).strip())


def score_application(
    application: CareerApplication, config: ATSConfiguration, cv_text: str | None = None
) -> ScoringOutcome:
    text = _searchable_text(application, cv_text)
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
        cv_text_used=bool(cv_text),
    )
