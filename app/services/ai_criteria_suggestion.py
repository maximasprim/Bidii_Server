"""
Orchestrates AI screening-criteria suggestion for one job posting. Used by
the "Suggest with AI" button in ATS Configuration's Screening Criteria
section — the suggestions this returns are never saved to the database by
this module or anything it calls; the router that uses this only ever
returns them in an API response for the frontend to show as editable
drafts the admin can individually add via the existing
POST /api/admin/ats/config/{config_id}/criteria endpoint.
"""

from app.models.job_opening import JobOpening
from app.services.ai_providers.base import AICriteriaSuggestion
from app.services.ai_providers.factory import get_provider

DEFAULT_TIMEOUT_SECONDS = 30


def suggest_criteria_with_ai(
    *, job: JobOpening, provider_name: str, model: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
) -> AICriteriaSuggestion:
    """Raises the same AIProviderError family as evaluate_candidate_with_ai — see ats_ai_evaluation.py."""
    provider = get_provider(provider_name)
    job_context = {
        "title": job.title,
        "department": job.department,
        "location": job.location,
        "employment_type": job.type,
        "description": job.description,
        "requirements": job.requirements,
        "responsibilities": job.responsibilities,
    }
    return provider.suggest_screening_criteria(job_context=job_context, model=model, timeout_seconds=timeout_seconds)
