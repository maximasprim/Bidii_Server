"""
Orchestrates AI job-draft generation. Used by the "Generate with AI" button
in the admin Job Listings create form — the draft this returns is never
saved to the database by this module or anything it calls; the router that
uses this only ever returns the draft in an API response for the frontend
to drop into the (editable) create-job form.
"""

from app.services.ai_providers.base import AIJobDraft
from app.services.ai_providers.factory import get_provider

DEFAULT_TIMEOUT_SECONDS = 30


def generate_job_draft_with_ai(
    *, title: str, provider_name: str, model: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
) -> AIJobDraft:
    """Raises the same AIProviderError family as evaluate_candidate_with_ai — see ats_ai_evaluation.py."""
    provider = get_provider(provider_name)
    return provider.generate_job_draft(title=title, model=model, timeout_seconds=timeout_seconds)
