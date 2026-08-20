import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.ats import ATSConfiguration, ATSCriterion, ATSEvaluationMode
from app.models.job_opening import JobOpening
from app.schemas.ats import (
    ATSConfigurationCreate,
    ATSConfigurationListResponse,
    ATSConfigurationRead,
    ATSConfigurationResponse,
    ATSConfigurationUpdate,
    ATSConfigurationWithJob,
    ATSCriterionCreate,
    ATSCriterionRead,
    ATSCriterionResponse,
    ATSCriterionUpdate,
)
from app.services.auth import get_current_admin

logger = logging.getLogger("bidii.admin_ats_config")

router = APIRouter(prefix="/api/admin/ats/config", tags=["admin-ats-config"], dependencies=[Depends(get_current_admin)])


def _get_job_or_404(db: Session, job_id: str) -> JobOpening:
    job = db.query(JobOpening).filter(JobOpening.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found.")
    return job


def _get_config_with_criteria(db: Session, job_id: str) -> ATSConfiguration | None:
    return (
        db.query(ATSConfiguration)
        .options(selectinload(ATSConfiguration.criteria))
        .filter(ATSConfiguration.job_id == job_id)
        .first()
    )


def _config_to_read(config: ATSConfiguration) -> ATSConfigurationRead:
    return ATSConfigurationRead.model_validate(config)


@router.get("", response_model=ATSConfigurationListResponse)
def list_configurations(db: Session = Depends(get_db)) -> ATSConfigurationListResponse:
    """All jobs that currently have an ATS configuration, for the ATS overview screen."""
    configs = db.query(ATSConfiguration).options(selectinload(ATSConfiguration.criteria)).all()
    items: list[ATSConfigurationWithJob] = []
    for config in configs:
        job = db.query(JobOpening).filter(JobOpening.id == config.job_id).first()
        if job is None:
            continue
        items.append(
            ATSConfigurationWithJob(
                **_config_to_read(config).model_dump(),
                job_title=job.title,
                job_slug=job.slug,
                job_is_open=job.is_open,
            )
        )
    return ATSConfigurationListResponse(items=items)


@router.get("/jobs/{job_id}", response_model=ATSConfigurationResponse)
def get_job_configuration(job_id: str, db: Session = Depends(get_db)) -> ATSConfigurationResponse:
    _get_job_or_404(db, job_id)
    config = _get_config_with_criteria(db, job_id)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This job has no ATS configuration yet. Create one to enable screening.",
        )
    return ATSConfigurationResponse(data=_config_to_read(config))


@router.post("/jobs/{job_id}", response_model=ATSConfigurationResponse, status_code=status.HTTP_201_CREATED)
def create_job_configuration(
    job_id: str,
    payload: ATSConfigurationCreate,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> ATSConfigurationResponse:
    """
    Creates (or replaces, if one already exists) the ATS configuration for a
    job posting, including its initial criteria list. Safe to call again
    later — an existing configuration's criteria are replaced wholesale,
    same as JobOpeningUpdate treats requirements/responsibilities lists.
    """
    _get_job_or_404(db, job_id)
    existing = db.query(ATSConfiguration).filter(ATSConfiguration.job_id == job_id).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This job already has an ATS configuration. Use PATCH to update it, or manage its criteria directly.",
        )

    config = ATSConfiguration(
        job_id=job_id,
        is_scoring_enabled=payload.is_scoring_enabled,
        auto_reject_enabled=payload.auto_reject_enabled,
        minimum_recommend_score=payload.minimum_recommend_score,
        minimum_review_score=payload.minimum_review_score,
        evaluation_mode=payload.evaluation_mode,
        ai_provider=payload.ai_provider,
        ai_model=payload.ai_model,
    )
    db.add(config)
    db.flush()  # assigns config.id without committing yet

    for criterion_payload in payload.criteria:
        db.add(
            ATSCriterion(
                config_id=config.id,
                category=criterion_payload.category,
                label=criterion_payload.label,
                description=criterion_payload.description,
                match_keywords=criterion_payload.match_keywords,
                weight=criterion_payload.weight,
                is_required=criterion_payload.is_required,
            )
        )

    db.commit()
    db.refresh(config)
    config = _get_config_with_criteria(db, job_id)

    logger.info("Admin %r created ATS configuration for job %r", current_admin.username, job_id)
    return ATSConfigurationResponse(data=_config_to_read(config))


@router.patch("/{config_id}", response_model=ATSConfigurationResponse)
def update_configuration(
    config_id: str,
    payload: ATSConfigurationUpdate,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> ATSConfigurationResponse:
    config = db.query(ATSConfiguration).filter(ATSConfiguration.id == config_id).first()
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ATS configuration not found.")

    if payload.is_scoring_enabled is not None:
        config.is_scoring_enabled = payload.is_scoring_enabled
    if payload.auto_reject_enabled is not None:
        config.auto_reject_enabled = payload.auto_reject_enabled
    if payload.minimum_recommend_score is not None:
        config.minimum_recommend_score = payload.minimum_recommend_score
    if payload.minimum_review_score is not None:
        config.minimum_review_score = payload.minimum_review_score
    if payload.evaluation_mode is not None:
        config.evaluation_mode = payload.evaluation_mode
    if payload.ai_provider is not None:
        config.ai_provider = payload.ai_provider
    if payload.ai_model is not None:
        config.ai_model = payload.ai_model

    if config.evaluation_mode == ATSEvaluationMode.ai and config.ai_provider is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select an AI provider (OpenAI or Gemini) before switching this job to AI Evaluation.",
)

    db.commit()
    db.refresh(config)
    config = _get_config_with_criteria(db, config.job_id)

    logger.info("Admin %r updated ATS configuration %r", current_admin.username, config_id)
    return ATSConfigurationResponse(data=_config_to_read(config))


@router.post("/{config_id}/criteria", response_model=ATSCriterionResponse, status_code=status.HTTP_201_CREATED)
def add_criterion(
    config_id: str,
    payload: ATSCriterionCreate,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> ATSCriterionResponse:
    config = db.query(ATSConfiguration).filter(ATSConfiguration.id == config_id).first()
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ATS configuration not found.")

    criterion = ATSCriterion(
        config_id=config_id,
        category=payload.category,
        label=payload.label,
        description=payload.description,
        match_keywords=payload.match_keywords,
        weight=payload.weight,
        is_required=payload.is_required,
    )
    db.add(criterion)
    db.commit()
    db.refresh(criterion)

    logger.info("Admin %r added ATS criterion %r to config %r", current_admin.username, criterion.label, config_id)
    return ATSCriterionResponse(data=ATSCriterionRead.model_validate(criterion))


@router.patch("/criteria/{criterion_id}", response_model=ATSCriterionResponse)
def update_criterion(
    criterion_id: str,
    payload: ATSCriterionUpdate,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> ATSCriterionResponse:
    criterion = db.query(ATSCriterion).filter(ATSCriterion.id == criterion_id).first()
    if criterion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Criterion not found.")

    if payload.category is not None:
        criterion.category = payload.category
    if payload.label is not None:
        criterion.label = payload.label
    if payload.description is not None:
        criterion.description = payload.description
    if payload.match_keywords is not None:
        criterion.match_keywords = payload.match_keywords
    if payload.weight is not None:
        criterion.weight = payload.weight
    if payload.is_required is not None:
        criterion.is_required = payload.is_required

    db.commit()
    db.refresh(criterion)

    logger.info("Admin %r updated ATS criterion %r", current_admin.username, criterion_id)
    return ATSCriterionResponse(data=ATSCriterionRead.model_validate(criterion))


@router.delete("/criteria/{criterion_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_criterion(
    criterion_id: str,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> None:
    criterion = db.query(ATSCriterion).filter(ATSCriterion.id == criterion_id).first()
    if criterion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Criterion not found.")

    db.delete(criterion)
    db.commit()
    logger.info("Admin %r deleted ATS criterion %r", current_admin.username, criterion_id)
