"""
Orchestrates AI generation of the *formal* Job Description document's
role-specific content — the letterhead-style document with a Key
Responsibilities/%-of-time/Performance-Criteria table, not the plain job
posting draft app/services/ai_job_generation.py produces for the create-job
form. See app/services/jd_pdf.py for how the result becomes a PDF matching
the company's fixed JD layout.
"""

from app.models.job_opening import JobOpening
from app.services.ai_providers.base import AIFormalJDDraft
from app.services.ai_providers.factory import get_provider

DEFAULT_TIMEOUT_SECONDS = 30


def generate_formal_jd_with_ai(
    *, job: JobOpening, provider_name: str, model: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
) -> AIFormalJDDraft:
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
    return provider.generate_formal_jd(job_context=job_context, model=model, timeout_seconds=timeout_seconds)
