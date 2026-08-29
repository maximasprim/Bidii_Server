"""
Orchestrates a single AI-based candidate evaluation. This is the only
module the ATS screening router talks to for AI evaluation — it doesn't
know about OpenAI, Gemini, or CV text extraction directly, just this
function's return value or the AIProviderError it might raise.
"""

from app.models.ats import ATSConfiguration
from app.models.career_application import CareerApplication
from app.models.job_opening import JobOpening
from app.services.ai_providers.base import AIEvaluationResult
from app.services.ai_providers.factory import get_provider
from app.services.cv_text_extraction import extract_cv_text

DEFAULT_TIMEOUT_SECONDS = 30

INCONSISTENT_VERDICT_DETAIL = (
    "Two independent AI evaluations disagreed on this criterion - treated as unmet pending human review."
)


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
    (see app/services/ai_providers/base.py) — the caller (the screening
    router) decides what to do on failure, typically falling back to
    weighted scoring. This function never falls back itself so that
    decision stays visible and testable in one place.

    When `config` has weighted criteria configured, those are passed
    through to the model so it evaluates the candidate against this job's
    actual configured criteria (with their weights and required flags)
    instead of just the job posting's free-text description — see
    app/services/ai_providers/prompts.py for how this changes the prompt
    and response shape. A job with no configured criteria still gets a
    useful evaluation via the older free-text fallback (a single call,
    since there's no discrete per-criterion verdict to check for
    agreement against).

    Criteria-aware evaluations are run TWICE and reconciled (see
    _reconcile_criteria_aware_runs below): an LLM's verdict on a
    borderline criterion isn't guaranteed to be the same from one call to
    the next even at a low temperature, and this evaluation can drive a
    real score, a stored recommendation, and — if the job has auto-reject
    enabled — an actual rejection. Silently trusting whichever run
    happened to come back isn't good enough for that; requiring the two
    to agree turns an unnoticed coin-flip into a flagged, visible "needs
    human review" instead. This doubles the AI provider calls (and cost)
    for criteria-aware screening - a deliberate trade given what's riding
    on the result.
    """
    provider = get_provider(provider_name)  # raises AIProviderNotConfiguredError if no key

    cv_text = extract_cv_text(application.cv_stored_filename)  # None on any failure — handled gracefully downstream

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
    return _reconcile_criteria_aware_runs(first, second, criteria)


def _reconcile_criteria_aware_runs(
    first: AIEvaluationResult, second: AIEvaluationResult, criteria: list[dict]
) -> AIEvaluationResult:
    """
    Combines two independent criteria-aware evaluations of the same
    candidate. A criterion only counts as "met" when both runs agree it
    was met; when they disagree, it's recorded as missing with a detail
    explaining why (visible in the admin UI's existing "Missing
    criteria" list - no frontend change needed) and added to
    inconsistent_criteria so the caller can force the overall
    recommendation to "review" rather than trust a score partly built on
    disagreement.
    """
    met_first = {c["criterion_id"] for c in first.matched_criteria}
    met_second = {c["criterion_id"] for c in second.matched_criteria}
    detail_by_id = {c["criterion_id"]: c["detail"] for c in (*first.matched_criteria, *first.missing_criteria)}

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
        first_met = c["id"] in met_first
        second_met = c["id"] in met_second

        if first_met != second_met:
            outcome["detail"] = INCONSISTENT_VERDICT_DETAIL
            missing.append(outcome)
            inconsistent.append(outcome)
            continue

        outcome["detail"] = detail_by_id.get(c["id"], "")
        if first_met:
            total_score += c["weight"]
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
# module the ATS screening router talks to for AI evaluation — it doesn't
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
#     (see app/services/ai_providers/base.py) — the caller (the screening
#     router) decides what to do on failure, typically falling back to
#     weighted scoring. This function never falls back itself so that
#     decision stays visible and testable in one place.
#     """
#     provider = get_provider(provider_name)  # raises AIProviderNotConfiguredError if no key

#     cv_text = extract_cv_text(application.cv_stored_filename)  # None on any failure — handled gracefully downstream

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
