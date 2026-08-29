"""
ATS scoring engine.

Evaluates each criterion's `match_keywords` against the text an applicant
supplies. Originally this was deliberately limited to submission-time
fields (cover note, applied-for role) to keep the module dependency-free
and I/O-free - but AI-suggested criteria (see
app/services/ai_criteria_suggestion.py) draft keywords from the job's own
requirements, which a candidate's CV is far more likely to literally
contain than a short cover note. So this module now also matches against
CV text when the caller has it - extraction itself still happens
elsewhere (app/services/cv_text_extraction.py, called from the screening
router) so this module stays I/O-free and easy to test in isolation; it
just accepts the already-extracted text as a plain string. A criterion
with no match_keywords configured is simply never matched, which is a
reasonable, honest default (there's nothing to auto-evaluate it against)
rather than guessing.

Matching is a bounded-substring search, not a full NLP pipeline: a
keyword only counts as a match when it isn't sandwiched between other
alphanumeric characters (so "art" doesn't fire inside "smart"), and a
match is discarded if a negation cue ("not", "no", "without", …) appears
in the few words right before it (so "no lending experience" doesn't
count as a match for "lending experience"). Both are heuristics - not a
real negation parser or a semantic matcher - aimed at the two most common
false-positive patterns in practice. They don't fix phrasing mismatches
(a CV that says "handled loan disbursements" still won't match a keyword
of "loan processing"); the way to cover that is to add multiple
match_keywords per criterion covering the different ways an applicant
might phrase it.

Nothing here touches CareerApplication.status except the one explicit,
opt-in auto-reject path described below.
"""

import re
from dataclasses import dataclass, field

from app.models.ats import ATSConfiguration, ATSCriterion, ATSRecommendation
from app.models.career_application import CareerApplication

# Words/endings that negate whatever keyword follows them within
# NEGATION_WINDOW_WORDS words. Deliberately blunt and English-only - it
# catches the common, direct phrasings real cover notes and CVs use
# ("no experience with X", "haven't worked in Y"), not every possible way
# to phrase an absence.
NEGATION_CUES = {
    "no",
    "not",
    "never",
    "without",
    "lack",
    "lacks",
    "lacking",
    "lacked",
    "except",
    "excluding",
    "unable",
    "cannot",
}
NEGATION_WINDOW_WORDS = 6


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
    caller was able to extract it. cv_text is optional and best-effort -
    a candidate whose CV couldn't be read still gets scored on whatever
    text is available, same as before this method existed.
    """
    parts = [application.role or "", application.cover_note or "", cv_text or ""]
    return " \n ".join(parts).lower()


def _keyword_pattern(keyword: str) -> re.Pattern:
    """
    Match `keyword` only when neither adjacent character is alphanumeric.
    Plain regex \\b word-boundaries look like the obvious tool here, but
    they only fire at a transition between a word and non-word character
    - a keyword ending in punctuation (e.g. "C++") has a non-word
    character on both sides of that boundary (the final "+" and the
    space after it), so \\b silently fails to match it at all. Asserting
    the adjacent character (if any) isn't alphanumeric covers ordinary
    words and symbol-bearing keywords alike.
    """
    escaped = re.escape(keyword)
    return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])")


def _is_negated(text: str, match_start: int) -> bool:
    """True if a negation cue appears in the few words right before the match."""
    preceding_words = re.findall(r"[a-z']+", text[:match_start])[-NEGATION_WINDOW_WORDS:]
    return any(word in NEGATION_CUES or word.endswith("n't") for word in preceding_words)


def _criterion_matches(criterion: ATSCriterion, text: str) -> bool:
    keywords = criterion.match_keywords or []
    for keyword in keywords:
        cleaned = str(keyword).strip().lower()
        if not cleaned:
            continue
        for occurrence in _keyword_pattern(cleaned).finditer(text):
            if not _is_negated(text, occurrence.start()):
                return True
    return False


def bucket_recommendation(
    score_percentage: float, has_failed_mandatory: bool, config: ATSConfiguration
) -> ATSRecommendation:
    """
    Single source of truth for turning a score into a recommendation
    bucket. Shared by the weighted engine (score_application, below) and
    the AI engine (see app/services/ats_ai_evaluation.py /
    app/routers/admin_ats_screening.py) so a job's configured thresholds
    mean the same thing regardless of which engine produced the score,
    and a stored recommendation can never disagree with the score that
    produced it - previously AI mode trusted the model's own self-reported
    recommendation label independently of its self-reported score, so the
    two could contradict each other and neither one respected this job's
    configured thresholds. Both now always go through this function.
    """
    if has_failed_mandatory:
        return ATSRecommendation.not_recommended
    if score_percentage >= config.minimum_recommend_score:
        return ATSRecommendation.recommended
    if score_percentage < config.minimum_review_score:
        return ATSRecommendation.not_recommended
    return ATSRecommendation.review


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
    recommendation = bucket_recommendation(score_percentage, bool(failed_mandatory), config)
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


# """
# ATS scoring engine.

# Evaluates each criterion's `match_keywords` against the text an applicant
# supplies. Originally this was deliberately limited to submission-time
# fields (cover note, applied-for role) to keep the module dependency-free
# and I/O-free - but AI-suggested criteria (see
# app/services/ai_criteria_suggestion.py) draft keywords from the job's own
# requirements, which a candidate's CV is far more likely to literally
# contain than a short cover note. So this module now also matches against
# CV text when the caller has it - extraction itself still happens
# elsewhere (app/services/cv_text_extraction.py, called from the screening
# router) so this module stays I/O-free and easy to test in isolation; it
# just accepts the already-extracted text as a plain string. A criterion
# with no match_keywords configured is simply never matched, which is a
# reasonable, honest default (there's nothing to auto-evaluate it against)
# rather than guessing.

# Matching is a bounded-substring search, not a full NLP pipeline: a
# keyword only counts as a match when it isn't sandwiched between other
# alphanumeric characters (so "art" doesn't fire inside "smart"), and a
# match is discarded if a negation cue ("not", "no", "without", …) appears
# in the few words right before it (so "no lending experience" doesn't
# count as a match for "lending experience"). Both are heuristics - not a
# real negation parser or a semantic matcher - aimed at the two most common
# false-positive patterns in practice. They don't fix phrasing mismatches
# (a CV that says "handled loan disbursements" still won't match a keyword
# of "loan processing"); the way to cover that is to add multiple
# match_keywords per criterion covering the different ways an applicant
# might phrase it.

# Nothing here touches CareerApplication.status except the one explicit,
# opt-in auto-reject path described below.
# """

# from dataclasses import dataclass, field

# from app.models.ats import ATSConfiguration, ATSCriterion, ATSRecommendation
# from app.models.career_application import CareerApplication


# @dataclass
# class ScoringOutcome:
#     total_score: float
#     max_possible_score: float
#     score_percentage: float
#     recommendation: ATSRecommendation
#     matched: list[dict] = field(default_factory=list)
#     missing: list[dict] = field(default_factory=list)
#     failed_mandatory: list[dict] = field(default_factory=list)
#     should_auto_reject: bool = False
#     cv_text_used: bool = False


# def _criterion_outcome_dict(criterion: ATSCriterion) -> dict:
#     return {
#         "criterion_id": criterion.id,
#         "label": criterion.label,
#         "category": criterion.category.value if hasattr(criterion.category, "value") else criterion.category,
#         "weight": criterion.weight,
#         "is_required": criterion.is_required,
#     }


# def _searchable_text(application: CareerApplication, cv_text: str | None) -> str:
#     """
#     The text an application is screened against: the applicant's
#     submission-time fields (role, cover note) plus their CV text, when the
#     caller was able to extract it. cv_text is optional and best-effort -
#     a candidate whose CV couldn't be read still gets scored on whatever
#     text is available, same as before this method existed.
#     """
#     parts = [application.role or "", application.cover_note or "", cv_text or ""]
#     return " \n ".join(parts).lower()


# def _criterion_matches(criterion: ATSCriterion, text: str) -> bool:
#     keywords = criterion.match_keywords or []
#     if not keywords:
#         return False
#     return any(str(keyword).strip().lower() in text for keyword in keywords if str(keyword).strip())


# def score_application(
#     application: CareerApplication, config: ATSConfiguration, cv_text: str | None = None
# ) -> ScoringOutcome:
#     text = _searchable_text(application, cv_text)
#     criteria: list[ATSCriterion] = list(config.criteria)

#     matched: list[dict] = []
#     missing: list[dict] = []
#     failed_mandatory: list[dict] = []

#     total_score = 0.0
#     max_possible_score = 0.0

#     for criterion in criteria:
#         max_possible_score += criterion.weight
#         outcome = _criterion_outcome_dict(criterion)
#         if _criterion_matches(criterion, text):
#             total_score += criterion.weight
#             matched.append(outcome)
#         else:
#             missing.append(outcome)
#             if criterion.is_required:
#                 failed_mandatory.append(outcome)

#     score_percentage = (total_score / max_possible_score * 100) if max_possible_score > 0 else 0.0

#     if failed_mandatory:
#         recommendation = ATSRecommendation.not_recommended
#     elif score_percentage >= config.minimum_recommend_score:
#         recommendation = ATSRecommendation.recommended
#     elif score_percentage < config.minimum_review_score:
#         recommendation = ATSRecommendation.not_recommended
#     else:
#         recommendation = ATSRecommendation.review

#     should_auto_reject = bool(config.auto_reject_enabled and failed_mandatory)

#     return ScoringOutcome(
#         total_score=round(total_score, 2),
#         max_possible_score=round(max_possible_score, 2),
#         score_percentage=round(score_percentage, 2),
#         recommendation=recommendation,
#         matched=matched,
#         missing=missing,
#         failed_mandatory=failed_mandatory,
#         should_auto_reject=should_auto_reject,
#         cv_text_used=bool(cv_text),
#     )
