"""
AI-related admin endpoints that aren't specific to ATS screening:
- provider status (used by both ATS Configuration's provider picker and the
  Job Listings "Generate with AI" button)
- AI job posting draft generation

Kept separate from admin_ats_config.py / admin_ats_screening.py /
admin_jobs.py so none of those files need to grow beyond their existing
concern — this router is the only new surface for "AI, outside of scoring
one candidate."
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.job_opening import JobOpening
from app.schemas.ats import (
    AICriteriaGenerateRequest,
    AICriteriaGenerateResponse,
    AICriteriaSuggestionData,
    AIJobDraftData,
    AIJobGenerateRequest,
    AIJobGenerateResponse,
    AISuggestedCriterionData,
    ATSAIProvidersResponse,
    ATSAIProviderStatus,
)
from app.services.ai_criteria_suggestion import suggest_criteria_with_ai
from app.services.ai_job_generation import generate_job_draft_with_ai
from app.services.ai_providers.base import AIProviderError, AIProviderNotConfiguredError
from app.services.ai_providers.factory import default_model_for, provider_status
from app.services.auth import get_current_admin

logger = logging.getLogger("bidii.admin_ai")

router = APIRouter(prefix="/api/admin/ai", tags=["admin-ai"], dependencies=[Depends(get_current_admin)])


@router.get("/providers", response_model=ATSAIProvidersResponse)
def get_ai_provider_status() -> ATSAIProvidersResponse:
    """
    Whether OpenAI/Gemini have server-side API keys configured, and each
    provider's default model. Powers the AI Evaluation provider picker in
    ATS Configuration and the model picker next to "Generate with AI" on
    the Job Listings page — never exposes the keys themselves, only a
    configured: true/false boolean per provider.
    """
    return ATSAIProvidersResponse(
        providers={name: ATSAIProviderStatus(**status_data) for name, status_data in provider_status().items()}
    )


@router.post("/jobs/generate", response_model=AIJobGenerateResponse)
def generate_job_draft(
    payload: AIJobGenerateRequest,
    current_admin: AdminUser = Depends(get_current_admin),
) -> AIJobGenerateResponse:
    """
    Generates a job posting draft (summary, description, responsibilities,
    requirements) from a title alone. Returns the draft only — this never
    writes to job_openings. The admin's existing "Create Job" form is
    populated with the result on the frontend, stays fully editable, and is
    saved/published through the existing POST /api/admin/jobs endpoint
    exactly as a manually-typed posting would be.
    """
    model = payload.model or default_model_for(payload.provider.value)
    try:
        draft = generate_job_draft_with_ai(title=payload.title, provider_name=payload.provider.value, model=model)
    except AIProviderNotConfiguredError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except AIProviderError as exc:
        logger.warning("AI job draft generation failed for title %r: %s", payload.title, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    logger.info("Admin %r generated an AI job draft for title %r via %s", current_admin.username, payload.title, draft.provider)
    return AIJobGenerateResponse(
        data=AIJobDraftData(
            summary=draft.summary,
            description=draft.description,
            responsibilities=draft.responsibilities,
            requirements=draft.requirements,
            provider=draft.provider,
            model=draft.model,
        )
    )


@router.post("/ats/criteria/generate/{job_id}", response_model=AICriteriaGenerateResponse)
def generate_ats_criteria(
    job_id: str,
    payload: AICriteriaGenerateRequest,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> AICriteriaGenerateResponse:
    """
    Suggests a starter set of Weighted Scoring criteria from a job's own
    posted description/requirements/responsibilities. Returns the
    suggestions only — nothing is written to ats_criteria here. The admin
    reviews/edits each one on the frontend and adds it through the existing
    POST /api/admin/ats/config/{config_id}/criteria endpoint, exactly as a
    manually-typed criterion would be.
    """
    job = db.query(JobOpening).filter(JobOpening.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found.")

    model = payload.model or default_model_for(payload.provider.value)
    try:
        suggestion = suggest_criteria_with_ai(job=job, provider_name=payload.provider.value, model=model)
    except AIProviderNotConfiguredError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except AIProviderError as exc:
        logger.warning("AI criteria suggestion failed for job %r: %s", job_id, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    logger.info(
        "Admin %r generated %d AI-suggested ATS criteria for job %r via %s",
        current_admin.username,
        len(suggestion.criteria),
        job_id,
        suggestion.provider,
    )
    return AICriteriaGenerateResponse(
        data=AICriteriaSuggestionData(
            criteria=[
                AISuggestedCriterionData(
                    category=c.category,
                    label=c.label,
                    description=c.description,
                    match_keywords=c.match_keywords,
                    weight=c.weight,
                    is_required=c.is_required,
                )
                for c in suggestion.criteria
            ],
            provider=suggestion.provider,
            model=suggestion.model,
        )
    )
