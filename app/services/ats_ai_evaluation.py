"""
Orchestrates a single AI-based candidate evaluation. This is the only
module the ATS screening router talks to for AI evaluation - it doesn't
know about OpenAI, Gemini, or CV text extraction directly, just this
function's return value or the AIProviderError it might raise.
"""

from app.models.ats import ATSConfiguration, ATSStrictness
from app.models.career_application import CareerApplication
from app.models.job_opening import JobOpening
from app.services.ai_providers.base import AIEvaluationResult
from app.services.ai_providers.factory import get_provider
from app.services.cv_text_extraction import extract_cv_text

DEFAULT_TIMEOUT_SECONDS = 30

INCONSISTENT_VERDICT_DETAIL = "Two independent AI evaluations disagreed on this criterion."


def evaluate_candidate_with_ai(
    *,
    job: JobOpening,
    application: CareerApplication,
    config: ATSConfiguration,
    provider_name: str,
    model: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> AIEvaluationResult:
    """
    Raises AIProviderNotConfiguredError / AIProviderTimeoutError /
    AIProviderRateLimitError / AIProviderInvalidResponseError / AIProviderError
    (see app/services/ai_providers/base.py) - the caller (the screening
    router) decides what to do on failure, typically falling back to
    weighted scoring. This function never falls back itself so that
    decision stays visible and testable in one place.

    When `config` has weighted criteria configured, those are passed
    through to the model so it evaluates the candidate against this job's
    actual configured criteria (with their weights and required flags)
    instead of just the job posting's free-text description - see
    app/services/ai_providers/prompts.py for how this changes the prompt
    and response shape. A job with no configured criteria still gets a
    useful evaluation via the older free-text fallback (a single call,
    since there's no discrete per-criterion verdict to check for
    agreement against).

    Criteria-aware evaluations are run TWICE and reconciled (see
    _reconcile_criteria_aware_runs below): an LLM's verdict on a
    borderline criterion isn't guaranteed to be the same from one call to
    the next even at a low temperature, and this evaluation can drive a
    real score, a stored recommendation, and - if the job has auto-reject
    enabled - an actual rejection. Silently trusting whichever run
    happened to come back isn't good enough for that; requiring the two
    to agree turns an unnoticed coin-flip into a flagged, visible "needs
    human review" instead. This doubles the AI provider calls (and cost)
    for criteria-aware screening - a deliberate trade given what's riding
    on the result.
    """
    provider = get_provider(provider_name)  # raises AIProviderNotConfiguredError if no key

    cv_text = extract_cv_text(application.cv_stored_filename)  # None on any failure - handled gracefully downstream

    job_context = {
        "title": job.title,
        "department": job.department,
        "location": job.location,
        "employment_type": job.type,
        "description": job.description,
        "requirements": job.requirements,
        "responsibilities": job.responsibilities,
    }
    candidate_context = {
        # full_name is deliberately NOT included here. Kenyan names often
        # signal ethnicity/region, and there's no legitimate reason the
        # screening judgement needs identity - the model only needs to
        # assess the content of the application. See
        # app/services/ai_providers/prompts.py, which never asks for or
        # prints a name in the evaluation prompt. (Note: this doesn't
        # scrub a name the candidate's own CV text might contain - the
        # system prompt instructs the model to disregard it if so, but
        # that's an instruction, not a guarantee.)
        "role_applied_for": application.role,
        "cover_note": application.cover_note,
        "cv_text": cv_text,
    }
    criteria = [
        {
            "id": c.id,
            "label": c.label,
            "category": c.category.value if hasattr(c.category, "value") else c.category,
            "description": c.description,
            "weight": c.weight,
            "is_required": c.is_required,
        }
        for c in (config.criteria if config else [])
    ] or None  # None (not []) so downstream code can tell "no criteria configured" from "empty list"

    if criteria is None:
        return provider.evaluate_candidate(
            job_context=job_context,
            candidate_context=candidate_context,
            model=model,
            timeout_seconds=timeout_seconds,
            criteria=None,
        )

    first = provider.evaluate_candidate(
        job_context=job_context, candidate_context=candidate_context, model=model, timeout_seconds=timeout_seconds, criteria=criteria
    )
    second = provider.evaluate_candidate(
        job_context=job_context, candidate_context=candidate_context, model=model, timeout_seconds=timeout_seconds, criteria=criteria
    )
    return _reconcile_criteria_aware_runs(first, second, criteria, config.strictness)


_PARTIAL_CREDIT_BY_STRICTNESS = {
    ATSStrictness.strict: 0.0,
    ATSStrictness.balanced: 0.5,
    ATSStrictness.lenient: 1.0,
}

_DISAGREEMENT_COMBINE_BY_STRICTNESS = {
    # On disagreement between the two runs, strict takes the harsher of
    # the two credits, lenient takes the more generous one, and balanced
    # splits the difference. Matches _PARTIAL_CREDIT_BY_STRICTNESS's own
    # direction so the setting reads consistently end to end.
    ATSStrictness.strict: min,
    ATSStrictness.balanced: lambda a, b: (a + b) / 2,
    ATSStrictness.lenient: max,
}


def _credit_for_status(status: str, strictness: ATSStrictness) -> float:
    """Weight fraction (0.0-1.0) a single verdict earns, per the job's configured strictness."""
    if status == "met":
        return 1.0
    if status == "partial":
        return _PARTIAL_CREDIT_BY_STRICTNESS[strictness]
    return 0.0  # not_met, or the model didn't address this criterion at all


def _reconcile_criteria_aware_runs(
    first: AIEvaluationResult, second: AIEvaluationResult, criteria: list[dict], strictness: ATSStrictness
) -> AIEvaluationResult:
    """
    Combines two independent criteria-aware evaluations of the same
    candidate. Each run's per-criterion verdict (met/partial/not_met) is
    converted to a credit fraction via _credit_for_status - "partial"
    earns 0%, 50%, or 100% credit depending on the job's configured
    strictness (see ATSStrictness in app/models/ats.py), never treated
    as an automatic zero the way it used to be.

    When both runs agree on a verdict, that verdict's credit is used
    directly. When they disagree, the two credits are combined per
    strictness (strict: harsher of the two wins; balanced: averaged;
    lenient: the more generous one wins) and the criterion is flagged in
    inconsistent_criteria so the caller can still force the overall
    recommendation to "review" - strictness controls how much credit a
    disagreement gets, not whether it's still surfaced for human
    attention.

    A criterion only lands in matched_criteria (full credit, 1.0) or
    counts toward is_required's failed_mandatory_criteria gate on
    anything less than full credit - a mandatory criterion still needs
    complete, not partial, evidence to be considered satisfied, at any
    strictness level. Partial credit below 1.0 still contributes its
    fraction to total_score either way.
    """
    status_first = {c["criterion_id"]: c["status"] for c in (*first.matched_criteria, *first.missing_criteria)}
    status_second = {c["criterion_id"]: c["status"] for c in (*second.matched_criteria, *second.missing_criteria)}
    detail_by_id = {c["criterion_id"]: c["detail"] for c in (*first.matched_criteria, *first.missing_criteria)}
    combine = _DISAGREEMENT_COMBINE_BY_STRICTNESS[strictness]

    matched: list[dict] = []
    missing: list[dict] = []
    failed_mandatory: list[dict] = []
    inconsistent: list[dict] = []
    total_score = 0.0
    max_possible_score = 0.0

    for c in criteria:
        max_possible_score += c["weight"]
        outcome = {
            "criterion_id": c["id"],
            "label": c["label"],
            "category": c["category"],
            "weight": c["weight"],
            "is_required": c["is_required"],
        }
        s_first = status_first.get(c["id"], "not_met")
        s_second = status_second.get(c["id"], "not_met")
        credit_first = _credit_for_status(s_first, strictness)
        credit_second = _credit_for_status(s_second, strictness)

        if s_first == s_second:
            credit = credit_first
            outcome["detail"] = detail_by_id.get(c["id"], "")
        else:
            credit = combine(credit_first, credit_second)
            outcome["detail"] = (
                f"{INCONSISTENT_VERDICT_DETAIL} ({int(round(credit * 100))}% credit applied per this job's "
                "strictness setting.)"
            )
            inconsistent.append(outcome)

        outcome["credit"] = round(credit, 2)
        total_score += credit * c["weight"]

        if credit >= 1.0:
            matched.append(outcome)
        else:
            missing.append(outcome)
            if c["is_required"]:
                failed_mandatory.append(outcome)

    score_percentage = (total_score / max_possible_score * 100) if max_possible_score > 0 else 0.0

    return AIEvaluationResult(
        score_percentage=round(score_percentage, 2),
        recommendation="",  # authoritative value is computed by the caller via bucket_recommendation()
        strengths=first.strengths,
        weaknesses=first.weaknesses,
        explanation=first.explanation,
        provider=first.provider,
        model=first.model,
        cv_text_used=first.cv_text_used,
        matched_criteria=matched,
        missing_criteria=missing,
        failed_mandatory_criteria=failed_mandatory,
        criteria_aware=True,
        inconsistent_criteria=inconsistent,
    )


# """
# Orchestrates a single AI-based candidate evaluation. This is the only
# module the ATS screening router talks to for AI evaluation - it doesn't
# know about OpenAI, Gemini, or CV text extraction directly, just this
# function's return value or the AIProviderError it might raise.
# """

# from app.models.career_application import CareerApplication
# from app.models.job_opening import JobOpening
# from app.services.ai_providers.base import AIEvaluationResult
# from app.services.ai_providers.factory import get_provider
# from app.services.cv_text_extraction import extract_cv_text

# DEFAULT_TIMEOUT_SECONDS = 30


# def evaluate_candidate_with_ai(
#     *,
#     job: JobOpening,
#     application: CareerApplication,
#     provider_name: str,
#     model: str,
#     timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
# ) -> AIEvaluationResult:
#     """
#     Raises AIProviderNotConfiguredError / AIProviderTimeoutError /
#     AIProviderRateLimitError / AIProviderInvalidResponseError / AIProviderError
#     (see app/services/ai_providers/base.py) - the caller (the screening
#     router) decides what to do on failure, typically falling back to
#     weighted scoring. This function never falls back itself so that
#     decision stays visible and testable in one place.
#     """
#     provider = get_provider(provider_name)  # raises AIProviderNotConfiguredError if no key

#     cv_text = extract_cv_text(application.cv_stored_filename)  # None on any failure - handled gracefully downstream

#     job_context = {
#         "title": job.title,
#         "department": job.department,
#         "location": job.location,
#         "employment_type": job.type,
#         "description": job.description,
#         "requirements": job.requirements,
#         "responsibilities": job.responsibilities,
#     }
#     candidate_context = {
#         "full_name": application.full_name,
#         "role_applied_for": application.role,
#         "cover_note": application.cover_note,
#         "cv_text": cv_text,
#     }

#     return provider.evaluate_candidate(
#         job_context=job_context, candidate_context=candidate_context, model=model, timeout_seconds=timeout_seconds
#     )
