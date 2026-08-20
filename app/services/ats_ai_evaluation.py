"""
Orchestrates a single AI-based candidate evaluation. This is the only
module the ATS screening router talks to for AI evaluation — it doesn't
know about OpenAI, Gemini, or CV text extraction directly, just this
function's return value or the AIProviderError it might raise.
"""

from app.models.career_application import CareerApplication
from app.models.job_opening import JobOpening
from app.services.ai_providers.base import AIEvaluationResult
from app.services.ai_providers.factory import get_provider
from app.services.cv_text_extraction import extract_cv_text

DEFAULT_TIMEOUT_SECONDS = 30


def evaluate_candidate_with_ai(
    *,
    job: JobOpening,
    application: CareerApplication,
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
        "full_name": application.full_name,
        "role_applied_for": application.role,
        "cover_note": application.cover_note,
        "cv_text": cv_text,
    }

    return provider.evaluate_candidate(
        job_context=job_context, candidate_context=candidate_context, model=model, timeout_seconds=timeout_seconds
    )
