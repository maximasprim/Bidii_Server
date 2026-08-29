import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.ats import (
    ATSAIProviderName,
    ATSAuditAction,
    ATSAuditLog,
    ATSConfiguration,
    ATSEvaluationMode,
    ATSRecommendation,
    ATSScreeningResult,
)
from app.models.career_application import CareerApplication, CareerApplicationStatus
from app.models.job_opening import JobOpening
from app.schemas.admin import PageMeta
from app.schemas.ats import (
    ATSScreenAllResponse,
    ATSScreenResponse,
    ATSScreeningResultRead,
    ATSStats,
    CareerApplicationWithATS,
    PaginatedATSApplications,
)
from app.services.ai_providers.base import AIProviderError
from app.services.ai_providers.factory import default_model_for
from app.services.ats_ai_evaluation import evaluate_candidate_with_ai
from app.services.ats_scoring import bucket_recommendation, score_application
from app.services.auth import get_current_admin
from app.services.cv_text_extraction import extract_cv_text
from app.services.notifications import maybe_auto_notify
from app.services.role_permissions import require_menu_access

logger = logging.getLogger("bidii.admin_ats_screening")

router = APIRouter(
    prefix="/api/admin/ats/screening",
    tags=["admin-ats-screening"],
    dependencies=[Depends(get_current_admin), Depends(require_menu_access("/admin/ats"))],
)


class ScreeningUnavailableError(Exception):
    """AI evaluation failed and this job has no weighted criteria to fall back to."""


def _to_result_read(result: ATSScreeningResult) -> ATSScreeningResultRead:
    data = ATSScreeningResultRead.model_validate(result)
    data.score_percentage = round(
        (result.total_score / result.max_possible_score * 100) if result.max_possible_score > 0 else 0.0, 2
    )
    return data


def _get_or_create_result(db: Session, application_id: str) -> tuple[ATSScreeningResult, str | None]:
    result = db.query(ATSScreeningResult).filter(ATSScreeningResult.application_id == application_id).first()
    previous_recommendation = result.system_recommendation.value if result else None
    if result is None:
        result = ATSScreeningResult(application_id=application_id)
        db.add(result)
    return result, previous_recommendation


def _run_weighted_screening(
    db: Session,
    application: CareerApplication,
    config: ATSConfiguration,
    admin_id: str | None,
    manual_method_override: str | None = None,
) -> ATSScreeningResult:
    """The weighted-scoring engine (app/services/ats_scoring.py) — now CV-aware, see that module's docstring."""
    cv_text = extract_cv_text(application.cv_stored_filename) if application.cv_stored_filename else None
    outcome = score_application(application, config, cv_text=cv_text)
    result, previous_recommendation = _get_or_create_result(db, application.id)

    result.config_id = config.id
    result.total_score = outcome.total_score
    result.max_possible_score = outcome.max_possible_score
    result.system_recommendation = outcome.recommendation
    result.matched_criteria = outcome.matched
    result.missing_criteria = outcome.missing
    result.failed_mandatory_criteria = outcome.failed_mandatory
    result.has_failed_mandatory = bool(outcome.failed_mandatory)
    result.auto_scored = True
    result.scored_at = datetime.now(timezone.utc)
    result.evaluation_method = ATSEvaluationMode.weighted
    result.ai_provider = None
    result.ai_model = None
    result.ai_strengths = []
    result.ai_weaknesses = []
    result.ai_explanation = None
    # Cleared here, then re-set by the AI-fallback caller right after this
    # returns — so a genuine fallback still shows its banner, but a plain
    # weighted re-screen (including a manual override away from AI) doesn't
    # keep showing a stale "AI was unavailable" notice from a previous run.
    result.ai_fallback_reason = None

    details = {
        "evaluation_method": "weighted",
        "previous_recommendation": previous_recommendation,
        "new_recommendation": outcome.recommendation.value,
        "score_percentage": outcome.score_percentage,
        "failed_mandatory_count": len(outcome.failed_mandatory),
        "cv_text_used": outcome.cv_text_used,
    }
    if manual_method_override:
        details["manual_override"] = manual_method_override
    db.add(ATSAuditLog(application_id=application.id, admin_id=admin_id, action=ATSAuditAction.screened, details=details))

    if outcome.should_auto_reject and application.status != CareerApplicationStatus.rejected:
        application.status = CareerApplicationStatus.rejected
        db.add(
            ATSAuditLog(
                application_id=application.id,
                admin_id=admin_id,
                action=ATSAuditAction.auto_rejected,
                details={"reason": "Failed one or more mandatory criteria with auto-reject enabled."},
            )
        )

    return result


def _run_ai_screening(
    db: Session,
    application: CareerApplication,
    job: JobOpening,
    config: ATSConfiguration,
    admin_id: str | None,
    manual_method_override: str | None = None,
) -> ATSScreeningResult:
    """
    Raises whatever app.services.ats_ai_evaluation.evaluate_candidate_with_ai
    raises (AIProviderError family) - callers decide whether to fall back to
    weighted scoring.

    When this job has configured weighted criteria, the AI is asked to
    verdict each one individually and the score/recommendation are
    computed here deterministically (see bucket_recommendation in
    ats_scoring.py) from those verdicts and this job's own configured
    weights/thresholds - never trusted from the model's own self-reported
    score or recommendation label, so the two can no longer disagree with
    each other. This also means a job with configured mandatory criteria
    AND auto_reject_enabled now auto-rejects on a failed mandatory
    criterion in AI mode too, exactly like weighted mode already did -
    previously AI mode had no concept of "mandatory" at all, so
    auto_reject_enabled was silently a no-op for any job screened with
    AI. A job with no configured criteria still gets a free-text
    evaluation and never auto-rejects, since there's nothing "mandatory"
    to fail in that case.
    """
    settings = get_settings()
    provider_name = config.ai_provider.value if config.ai_provider else None
    if not provider_name:
        raise AIProviderError("This job is set to AI Evaluation but no AI provider is selected.")
    model = config.ai_model or default_model_for(provider_name)

    ai_result = evaluate_candidate_with_ai(
        job=job,
        application=application,
        config=config,
        provider_name=provider_name,
        model=model,
        timeout_seconds=settings.ai_request_timeout_seconds,
    )

    has_failed_mandatory = bool(ai_result.failed_mandatory_criteria)
    has_inconsistent = bool(ai_result.inconsistent_criteria)
    if ai_result.criteria_aware and has_inconsistent:
        # At least one criterion's verdict didn't reproduce across the two
        # evaluation runs (see ats_ai_evaluation.py) - the score itself is
        # still computed treating that criterion as unmet (conservative),
        # but the overall recommendation is forced to "review" rather than
        # trusting bucket_recommendation on a score that's partly built on
        # a disagreement. This also means auto-reject never fires purely
        # because of an inconsistent (as opposed to a consistently failed)
        # mandatory criterion - see the auto-reject check below.
        recommendation = ATSRecommendation.review
    else:
        recommendation = bucket_recommendation(ai_result.score_percentage, has_failed_mandatory, config)

    result, previous_recommendation = _get_or_create_result(db, application.id)
    result.config_id = config.id
    result.total_score = ai_result.score_percentage
    result.max_possible_score = 100.0
    result.system_recommendation = recommendation
    if ai_result.criteria_aware:
        result.matched_criteria = ai_result.matched_criteria
        result.missing_criteria = ai_result.missing_criteria
        result.failed_mandatory_criteria = ai_result.failed_mandatory_criteria
    else:
        result.matched_criteria = [{"label": r.label, "detail": r.detail} for r in ai_result.matched_requirements]
        result.missing_criteria = [{"label": r.label, "detail": r.detail} for r in ai_result.missing_requirements]
        result.failed_mandatory_criteria = []
    result.has_failed_mandatory = has_failed_mandatory
    result.auto_scored = True
    result.scored_at = datetime.now(timezone.utc)
    result.evaluation_method = ATSEvaluationMode.ai
    result.ai_provider = ATSAIProviderName(ai_result.provider)
    result.ai_model = ai_result.model
    result.ai_strengths = ai_result.strengths
    result.ai_weaknesses = ai_result.weaknesses
    result.ai_explanation = ai_result.explanation
    result.ai_fallback_reason = None

    db.add(
        ATSAuditLog(
            application_id=application.id,
            admin_id=admin_id,
            action=ATSAuditAction.screened,
            details={
                "evaluation_method": "ai",
                "provider": ai_result.provider,
                "model": ai_result.model,
                "criteria_aware": ai_result.criteria_aware,
                "inconsistent_criteria_count": len(ai_result.inconsistent_criteria),
                "previous_recommendation": previous_recommendation,
                "new_recommendation": recommendation.value,
                "score_percentage": ai_result.score_percentage,
                "cv_text_used": ai_result.cv_text_used,
                **({"manual_override": manual_method_override} if manual_method_override else {}),
            },
        )
    )

    if (
        ai_result.criteria_aware
        and config.auto_reject_enabled
        and has_failed_mandatory
        and application.status != CareerApplicationStatus.rejected
    ):
        application.status = CareerApplicationStatus.rejected
        db.add(
            ATSAuditLog(
                application_id=application.id,
                admin_id=admin_id,
                action=ATSAuditAction.auto_rejected,
                details={"reason": "Failed one or more mandatory criteria (AI evaluation) with auto-reject enabled."},
            )
        )

    return result


def _run_screening(
    db: Session,
    application: CareerApplication,
    job: JobOpening,
    config: ATSConfiguration,
    admin_id: str | None,
    mode: ATSEvaluationMode | None = None,
) -> ATSScreeningResult:
    """
    Dispatches to AI or weighted evaluation. Uses `mode` if given — an
    explicit one-off override so a candidate can be re-screened with the
    *other* engine without changing the job's saved evaluation_mode — and
    falls back to config.evaluation_mode when `mode` is None (the normal,
    non-override path). If AI evaluation is selected but fails for any
    reason (not configured, timeout, rate limit, invalid response, any
    other provider error), this automatically falls back to weighted
    scoring using the job's configured criteria - the existing weighted
    engine is always the safety net, exactly as it was before AI
    evaluation existed. Only raises ScreeningUnavailableError if AI fails
    AND the job has no weighted criteria to fall back to either.
    """
    effective_mode = mode if mode is not None else config.evaluation_mode
    manual_override = mode.value if (mode is not None and mode != config.evaluation_mode) else None

    if effective_mode != ATSEvaluationMode.ai:
        return _run_weighted_screening(db, application, config, admin_id, manual_method_override=manual_override)

    try:
        return _run_ai_screening(db, application, job, config, admin_id, manual_method_override=manual_override)
    except AIProviderError as exc:
        logger.warning("AI evaluation failed for application %r, falling back if possible: %s", application.id, exc)
        db.add(
            ATSAuditLog(
                application_id=application.id,
                admin_id=admin_id,
                action=ATSAuditAction.ai_evaluation_failed,
                details={"provider": config.ai_provider.value if config.ai_provider else None, "error": str(exc)},
            )
        )
        if not config.criteria:
            raise ScreeningUnavailableError(
                f"AI evaluation failed ({exc}) and this job has no weighted criteria configured as a fallback. "
                "Add weighted criteria in ATS Configuration, or fix the AI provider setup, then try again."
            ) from exc

        result = _run_weighted_screening(db, application, config, admin_id, manual_method_override=manual_override)
        result.ai_fallback_reason = str(exc)
        db.add(
            ATSAuditLog(
                application_id=application.id,
                admin_id=admin_id,
                action=ATSAuditAction.ai_fallback_to_weighted,
                details={"reason": str(exc)},
            )
        )
        return result


@router.post("/applications/{application_id}/screen", response_model=ATSScreenResponse)
def screen_application(
    application_id: str,
    method: ATSEvaluationMode | None = Query(
        None,
        description=(
            "Override which engine runs for this screening only (weighted or ai), without changing the "
            "job's saved evaluation_mode. Omit to use the job's currently configured method."
        ),
    ),
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> ATSScreenResponse:
    application = db.query(CareerApplication).filter(CareerApplication.id == application_id).first()
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Career application not found.")
    if application.job_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This is a general application with no specific job posting, so it can't be scored against job criteria.",
        )

    config = db.query(ATSConfiguration).filter(ATSConfiguration.job_id == application.job_id).first()
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="The job this candidate applied for has no ATS configuration yet."
        )
    if not config.is_scoring_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Automatic scoring is disabled for this job.")
    if method == ATSEvaluationMode.ai and config.ai_provider is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select an AI provider (OpenAI or Gemini) for this job in ATS Configuration before re-screening with AI.",
        )

    job = db.query(JobOpening).filter(JobOpening.id == application.job_id).first()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The job this candidate applied for no longer exists.")

    previous_status = application.status
    try:
        result = _run_screening(db, application, job, config, current_admin.id, mode=method)
    except ScreeningUnavailableError as exc:
        db.commit()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    db.commit()
    db.refresh(result)

    if application.status != previous_status:
        maybe_auto_notify(db, application, application.status.value)

    logger.info(
        "Admin %r screened application %r via %s (score=%s%%)%s",
        current_admin.username,
        application_id,
        result.evaluation_method.value,
        result.total_score,
        f" [manual override: {method.value}]" if method is not None else "",
    )
    message = "Application screened."
    if result.ai_fallback_reason:
        message = "AI evaluation was unavailable, so weighted scoring was used instead."
    return ATSScreenResponse(message=message, data=_to_result_read(result))


@router.post("/jobs/{job_id}/screen-all", response_model=ATSScreenAllResponse)
def screen_all_for_job(
    job_id: str,
    rescore_all: bool = Query(False, description="If true, re-screen every application including already-screened ones."),
    method: ATSEvaluationMode | None = Query(
        None,
        description=(
            "Override which engine runs for every application in this batch, without changing the job's "
            "saved evaluation_mode. Omit to use the job's currently configured method."
        ),
    ),
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> ATSScreenAllResponse:
    config = db.query(ATSConfiguration).filter(ATSConfiguration.job_id == job_id).first()
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This job has no ATS configuration yet.")
    if not config.is_scoring_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Automatic scoring is disabled for this job.")
    if method == ATSEvaluationMode.ai and config.ai_provider is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select an AI provider (OpenAI or Gemini) for this job in ATS Configuration before re-screening with AI.",
        )

    job = db.query(JobOpening).filter(JobOpening.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found.")

    applications = db.query(CareerApplication).filter(CareerApplication.job_id == job_id).all()
    if not rescore_all:
        already_screened_ids = {
            row[0]
            for row in db.query(ATSScreeningResult.application_id)
            .filter(ATSScreeningResult.application_id.in_([a.id for a in applications]))
            .all()
        }
        applications = [a for a in applications if a.id not in already_screened_ids]

    results: list[ATSScreeningResult] = []
    failures: list[dict] = []
    previous_statuses = {a.id: a.status for a in applications}
    for application in applications:
        try:
            results.append(_run_screening(db, application, job, config, current_admin.id, mode=method))
        except ScreeningUnavailableError as exc:
            # One candidate's AI+fallback failure doesn't abort the whole
            # batch - every other application in this job still gets screened.
            failures.append({"application_id": application.id, "full_name": application.full_name, "error": str(exc)})

    db.commit()
    for result in results:
        db.refresh(result)
    for application in applications:
        db.refresh(application)
        if application.status != previous_statuses.get(application.id):
            maybe_auto_notify(db, application, application.status.value)

    logger.info(
        "Admin %r batch-screened %d application(s) for job %r (%d failed)",
        current_admin.username,
        len(results),
        job_id,
        len(failures),
    )
    message = f"Screened {len(results)} application(s)."
    if failures:
        message += f" {len(failures)} couldn't be screened - see details."
    return ATSScreenAllResponse(
        message=message,
        screened_count=len(results),
        results=[_to_result_read(r) for r in results],
        failed=failures,
    )


@router.get("/applications", response_model=PaginatedATSApplications)
def list_screened_applications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    job_id: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    recommendation: ATSRecommendation | None = None,
    min_score: float | None = Query(None, ge=0, le=100),
    max_score: float | None = Query(None, ge=0, le=100),
    mandatory_failed: bool | None = Query(None, description="True = only candidates who failed a mandatory criterion."),
    evaluation_method: ATSEvaluationMode | None = None,
    sort_by: str = Query("date", pattern="^(date|score)$"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
) -> PaginatedATSApplications:
    """
    Career applications enriched with their latest ATS screening result (if
    any). This is a read-only view built on top of career_applications +
    ats_screening_results - it never modifies either table, and an
    application with no screening result yet still shows up normally
    (screening: null) when no ATS filters are applied.
    """
    query = db.query(CareerApplication).outerjoin(
        ATSScreeningResult, ATSScreeningResult.application_id == CareerApplication.id
    )

    if job_id:
        query = query.filter(CareerApplication.job_id == job_id)
    if status_filter:
        query = query.filter(CareerApplication.status == status_filter)
    if recommendation is not None:
        query = query.filter(ATSScreeningResult.system_recommendation == recommendation)
    if min_score is not None:
        query = query.filter(ATSScreeningResult.max_possible_score > 0).filter(
            (ATSScreeningResult.total_score / ATSScreeningResult.max_possible_score * 100) >= min_score
        )
    if max_score is not None:
        query = query.filter(ATSScreeningResult.max_possible_score > 0).filter(
            (ATSScreeningResult.total_score / ATSScreeningResult.max_possible_score * 100) <= max_score
        )
    if mandatory_failed is True:
        query = query.filter(ATSScreeningResult.has_failed_mandatory.is_(True))
    elif mandatory_failed is False:
        query = query.filter(
            (ATSScreeningResult.has_failed_mandatory.is_(False)) | (ATSScreeningResult.has_failed_mandatory.is_(None))
        )
    if evaluation_method is not None:
        query = query.filter(ATSScreeningResult.evaluation_method == evaluation_method)

    total = query.count()

    if sort_by == "score":
        order_col = ATSScreeningResult.total_score
        query = query.order_by(order_col.asc() if sort_dir == "asc" else order_col.desc())
    else:
        order_col = CareerApplication.created_at
        query = query.order_by(order_col.asc() if sort_dir == "asc" else order_col.desc())

    applications = query.offset((page - 1) * page_size).limit(page_size).all()

    result_by_app = {
        r.application_id: r
        for r in db.query(ATSScreeningResult).filter(
            ATSScreeningResult.application_id.in_([a.id for a in applications])
        )
    }

    items = []
    for app in applications:
        item = CareerApplicationWithATS.model_validate(app)
        result = result_by_app.get(app.id)
        item.screening = _to_result_read(result) if result else None
        items.append(item)

    total_pages = max(1, -(-total // page_size))
    return PaginatedATSApplications(
        meta=PageMeta(page=page, page_size=page_size, total=total, total_pages=total_pages), items=items
    )


@router.get("/stats", response_model=ATSStats)
def get_ats_stats(job_id: str | None = None, db: Session = Depends(get_db)) -> ATSStats:
    app_query = db.query(CareerApplication)
    if job_id:
        app_query = app_query.filter(CareerApplication.job_id == job_id)
    total_applications = app_query.count()

    result_query = db.query(ATSScreeningResult)
    if job_id:
        result_query = result_query.join(
            CareerApplication, CareerApplication.id == ATSScreeningResult.application_id
        ).filter(CareerApplication.job_id == job_id)
    results = result_query.all()

    total_screened = len(results)
    recommended = sum(1 for r in results if r.system_recommendation == ATSRecommendation.recommended)
    review = sum(1 for r in results if r.system_recommendation == ATSRecommendation.review)
    not_recommended = sum(1 for r in results if r.system_recommendation == ATSRecommendation.not_recommended)

    percentages = [
        (r.total_score / r.max_possible_score * 100) for r in results if r.max_possible_score > 0
    ]
    average_score = round(sum(percentages) / len(percentages), 2) if percentages else 0.0

    return ATSStats(
        total_applications=total_applications,
        total_screened=total_screened,
        total_unscreened=max(0, total_applications - total_screened),
        recommended_count=recommended,
        review_count=review,
        not_recommended_count=not_recommended,
        average_score_percentage=average_score,
    )


# import logging
# from datetime import datetime, timezone

# from fastapi import APIRouter, Depends, HTTPException, Query, status
# from sqlalchemy.orm import Session

# from app.config import get_settings
# from app.database import get_db
# from app.models.admin_user import AdminUser
# from app.models.ats import (
#     ATSAIProviderName,
#     ATSAuditAction,
#     ATSAuditLog,
#     ATSConfiguration,
#     ATSEvaluationMode,
#     ATSRecommendation,
#     ATSScreeningResult,
# )
# from app.models.career_application import CareerApplication, CareerApplicationStatus
# from app.models.job_opening import JobOpening
# from app.schemas.admin import PageMeta
# from app.schemas.ats import (
#     ATSScreenAllResponse,
#     ATSScreenResponse,
#     ATSScreeningResultRead,
#     ATSStats,
#     CareerApplicationWithATS,
#     PaginatedATSApplications,
# )
# from app.services.ai_providers.base import AIProviderError
# from app.services.ai_providers.factory import default_model_for
# from app.services.ats_ai_evaluation import evaluate_candidate_with_ai
# from app.services.ats_scoring import score_application
# from app.services.auth import get_current_admin
# from app.services.cv_text_extraction import extract_cv_text

# logger = logging.getLogger("bidii.admin_ats_screening")

# router = APIRouter(
#     prefix="/api/admin/ats/screening", tags=["admin-ats-screening"], dependencies=[Depends(get_current_admin)]
# )


# class ScreeningUnavailableError(Exception):
#     """AI evaluation failed and this job has no weighted criteria to fall back to."""


# def _to_result_read(result: ATSScreeningResult) -> ATSScreeningResultRead:
#     data = ATSScreeningResultRead.model_validate(result)
#     data.score_percentage = round(
#         (result.total_score / result.max_possible_score * 100) if result.max_possible_score > 0 else 0.0, 2
#     )
#     return data


# def _get_or_create_result(db: Session, application_id: str) -> tuple[ATSScreeningResult, str | None]:
#     result = db.query(ATSScreeningResult).filter(ATSScreeningResult.application_id == application_id).first()
#     previous_recommendation = result.system_recommendation.value if result else None
#     if result is None:
#         result = ATSScreeningResult(application_id=application_id)
#         db.add(result)
#     return result, previous_recommendation


# def _run_weighted_screening(
#     db: Session,
#     application: CareerApplication,
#     config: ATSConfiguration,
#     admin_id: str | None,
#     manual_method_override: str | None = None,
# ) -> ATSScreeningResult:
#     """The weighted-scoring engine (app/services/ats_scoring.py) — now CV-aware, see that module's docstring."""
#     cv_text = extract_cv_text(application.cv_stored_filename) if application.cv_stored_filename else None
#     outcome = score_application(application, config, cv_text=cv_text)
#     result, previous_recommendation = _get_or_create_result(db, application.id)

#     result.config_id = config.id
#     result.total_score = outcome.total_score
#     result.max_possible_score = outcome.max_possible_score
#     result.system_recommendation = outcome.recommendation
#     result.matched_criteria = outcome.matched
#     result.missing_criteria = outcome.missing
#     result.failed_mandatory_criteria = outcome.failed_mandatory
#     result.has_failed_mandatory = bool(outcome.failed_mandatory)
#     result.auto_scored = True
#     result.scored_at = datetime.now(timezone.utc)
#     result.evaluation_method = ATSEvaluationMode.weighted
#     result.ai_provider = None
#     result.ai_model = None
#     result.ai_strengths = []
#     result.ai_weaknesses = []
#     result.ai_explanation = None
#     # Cleared here, then re-set by the AI-fallback caller right after this
#     # returns — so a genuine fallback still shows its banner, but a plain
#     # weighted re-screen (including a manual override away from AI) doesn't
#     # keep showing a stale "AI was unavailable" notice from a previous run.
#     result.ai_fallback_reason = None

#     details = {
#         "evaluation_method": "weighted",
#         "previous_recommendation": previous_recommendation,
#         "new_recommendation": outcome.recommendation.value,
#         "score_percentage": outcome.score_percentage,
#         "failed_mandatory_count": len(outcome.failed_mandatory),
#         "cv_text_used": outcome.cv_text_used,
#     }
#     if manual_method_override:
#         details["manual_override"] = manual_method_override
#     db.add(ATSAuditLog(application_id=application.id, admin_id=admin_id, action=ATSAuditAction.screened, details=details))

#     if outcome.should_auto_reject and application.status != CareerApplicationStatus.rejected:
#         application.status = CareerApplicationStatus.rejected
#         db.add(
#             ATSAuditLog(
#                 application_id=application.id,
#                 admin_id=admin_id,
#                 action=ATSAuditAction.auto_rejected,
#                 details={"reason": "Failed one or more mandatory criteria with auto-reject enabled."},
#             )
#         )

#     return result


# def _run_ai_screening(
#     db: Session,
#     application: CareerApplication,
#     job: JobOpening,
#     config: ATSConfiguration,
#     admin_id: str | None,
#     manual_method_override: str | None = None,
# ) -> ATSScreeningResult:
#     """
#     Raises whatever app.services.ats_ai_evaluation.evaluate_candidate_with_ai
#     raises (AIProviderError family) - callers decide whether to fall back to
#     weighted scoring. Never touches CareerApplication.status: AI assists
#     vetting, it never auto-rejects.
#     """
#     settings = get_settings()
#     provider_name = config.ai_provider.value if config.ai_provider else None
#     if not provider_name:
#         raise AIProviderError("This job is set to AI Evaluation but no AI provider is selected.")
#     model = config.ai_model or default_model_for(provider_name)

#     ai_result = evaluate_candidate_with_ai(
#         job=job,
#         application=application,
#         provider_name=provider_name,
#         model=model,
#         timeout_seconds=settings.ai_request_timeout_seconds,
#     )

#     result, previous_recommendation = _get_or_create_result(db, application.id)
#     result.config_id = config.id
#     result.total_score = ai_result.score_percentage
#     result.max_possible_score = 100.0
#     result.system_recommendation = ATSRecommendation(ai_result.recommendation)
#     result.matched_criteria = [{"label": r.label, "detail": r.detail} for r in ai_result.matched_requirements]
#     result.missing_criteria = [{"label": r.label, "detail": r.detail} for r in ai_result.missing_requirements]
#     # AI evaluation doesn't use the weighted engine's required/weight concept,
#     # so it never marks "failed mandatory criteria" or drives auto-reject -
#     # see module docstring: AI assists vetting, it never auto-rejects.
#     result.failed_mandatory_criteria = []
#     result.has_failed_mandatory = False
#     result.auto_scored = True
#     result.scored_at = datetime.now(timezone.utc)
#     result.evaluation_method = ATSEvaluationMode.ai
#     result.ai_provider = ATSAIProviderName(ai_result.provider)
#     result.ai_model = ai_result.model
#     result.ai_strengths = ai_result.strengths
#     result.ai_weaknesses = ai_result.weaknesses
#     result.ai_explanation = ai_result.explanation
#     result.ai_fallback_reason = None

#     db.add(
#         ATSAuditLog(
#             application_id=application.id,
#             admin_id=admin_id,
#             action=ATSAuditAction.screened,
#             details={
#                 "evaluation_method": "ai",
#                 "provider": ai_result.provider,
#                 "model": ai_result.model,
#                 "previous_recommendation": previous_recommendation,
#                 "new_recommendation": ai_result.recommendation,
#                 "score_percentage": ai_result.score_percentage,
#                 "cv_text_used": ai_result.cv_text_used,
#                 **({"manual_override": manual_method_override} if manual_method_override else {}),
#             },
#         )
#     )

#     return result


# def _run_screening(
#     db: Session,
#     application: CareerApplication,
#     job: JobOpening,
#     config: ATSConfiguration,
#     admin_id: str | None,
#     mode: ATSEvaluationMode | None = None,
# ) -> ATSScreeningResult:
#     """
#     Dispatches to AI or weighted evaluation. Uses `mode` if given — an
#     explicit one-off override so a candidate can be re-screened with the
#     *other* engine without changing the job's saved evaluation_mode — and
#     falls back to config.evaluation_mode when `mode` is None (the normal,
#     non-override path). If AI evaluation is selected but fails for any
#     reason (not configured, timeout, rate limit, invalid response, any
#     other provider error), this automatically falls back to weighted
#     scoring using the job's configured criteria - the existing weighted
#     engine is always the safety net, exactly as it was before AI
#     evaluation existed. Only raises ScreeningUnavailableError if AI fails
#     AND the job has no weighted criteria to fall back to either.
#     """
#     effective_mode = mode if mode is not None else config.evaluation_mode
#     manual_override = mode.value if (mode is not None and mode != config.evaluation_mode) else None

#     if effective_mode != ATSEvaluationMode.ai:
#         return _run_weighted_screening(db, application, config, admin_id, manual_method_override=manual_override)

#     try:
#         return _run_ai_screening(db, application, job, config, admin_id, manual_method_override=manual_override)
#     except AIProviderError as exc:
#         logger.warning("AI evaluation failed for application %r, falling back if possible: %s", application.id, exc)
#         db.add(
#             ATSAuditLog(
#                 application_id=application.id,
#                 admin_id=admin_id,
#                 action=ATSAuditAction.ai_evaluation_failed,
#                 details={"provider": config.ai_provider.value if config.ai_provider else None, "error": str(exc)},
#             )
#         )
#         if not config.criteria:
#             raise ScreeningUnavailableError(
#                 f"AI evaluation failed ({exc}) and this job has no weighted criteria configured as a fallback. "
#                 "Add weighted criteria in ATS Configuration, or fix the AI provider setup, then try again."
#             ) from exc

#         result = _run_weighted_screening(db, application, config, admin_id, manual_method_override=manual_override)
#         result.ai_fallback_reason = str(exc)
#         db.add(
#             ATSAuditLog(
#                 application_id=application.id,
#                 admin_id=admin_id,
#                 action=ATSAuditAction.ai_fallback_to_weighted,
#                 details={"reason": str(exc)},
#             )
#         )
#         return result


# @router.post("/applications/{application_id}/screen", response_model=ATSScreenResponse)
# def screen_application(
#     application_id: str,
#     method: ATSEvaluationMode | None = Query(
#         None,
#         description=(
#             "Override which engine runs for this screening only (weighted or ai), without changing the "
#             "job's saved evaluation_mode. Omit to use the job's currently configured method."
#         ),
#     ),
#     db: Session = Depends(get_db),
#     current_admin: AdminUser = Depends(get_current_admin),
# ) -> ATSScreenResponse:
#     application = db.query(CareerApplication).filter(CareerApplication.id == application_id).first()
#     if application is None:
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Career application not found.")
#     if application.job_id is None:
#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST,
#             detail="This is a general application with no specific job posting, so it can't be scored against job criteria.",
#         )

#     config = db.query(ATSConfiguration).filter(ATSConfiguration.job_id == application.job_id).first()
#     if config is None:
#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST, detail="The job this candidate applied for has no ATS configuration yet."
#         )
#     if not config.is_scoring_enabled:
#         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Automatic scoring is disabled for this job.")
#     if method == ATSEvaluationMode.ai and config.ai_provider is None:
#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST,
#             detail="Select an AI provider (OpenAI or Gemini) for this job in ATS Configuration before re-screening with AI.",
#         )

#     job = db.query(JobOpening).filter(JobOpening.id == application.job_id).first()
#     if job is None:
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The job this candidate applied for no longer exists.")

#     try:
#         result = _run_screening(db, application, job, config, current_admin.id, mode=method)
#     except ScreeningUnavailableError as exc:
#         db.commit()
#         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

#     db.commit()
#     db.refresh(result)

#     logger.info(
#         "Admin %r screened application %r via %s (score=%s%%)%s",
#         current_admin.username,
#         application_id,
#         result.evaluation_method.value,
#         result.total_score,
#         f" [manual override: {method.value}]" if method is not None else "",
#     )
#     message = "Application screened."
#     if result.ai_fallback_reason:
#         message = "AI evaluation was unavailable, so weighted scoring was used instead."
#     return ATSScreenResponse(message=message, data=_to_result_read(result))


# @router.post("/jobs/{job_id}/screen-all", response_model=ATSScreenAllResponse)
# def screen_all_for_job(
#     job_id: str,
#     rescore_all: bool = Query(False, description="If true, re-screen every application including already-screened ones."),
#     method: ATSEvaluationMode | None = Query(
#         None,
#         description=(
#             "Override which engine runs for every application in this batch, without changing the job's "
#             "saved evaluation_mode. Omit to use the job's currently configured method."
#         ),
#     ),
#     db: Session = Depends(get_db),
#     current_admin: AdminUser = Depends(get_current_admin),
# ) -> ATSScreenAllResponse:
#     config = db.query(ATSConfiguration).filter(ATSConfiguration.job_id == job_id).first()
#     if config is None:
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This job has no ATS configuration yet.")
#     if not config.is_scoring_enabled:
#         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Automatic scoring is disabled for this job.")
#     if method == ATSEvaluationMode.ai and config.ai_provider is None:
#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST,
#             detail="Select an AI provider (OpenAI or Gemini) for this job in ATS Configuration before re-screening with AI.",
#         )

#     job = db.query(JobOpening).filter(JobOpening.id == job_id).first()
#     if job is None:
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found.")

#     applications = db.query(CareerApplication).filter(CareerApplication.job_id == job_id).all()
#     if not rescore_all:
#         already_screened_ids = {
#             row[0]
#             for row in db.query(ATSScreeningResult.application_id)
#             .filter(ATSScreeningResult.application_id.in_([a.id for a in applications]))
#             .all()
#         }
#         applications = [a for a in applications if a.id not in already_screened_ids]

#     results: list[ATSScreeningResult] = []
#     failures: list[dict] = []
#     for application in applications:
#         try:
#             results.append(_run_screening(db, application, job, config, current_admin.id, mode=method))
#         except ScreeningUnavailableError as exc:
#             # One candidate's AI+fallback failure doesn't abort the whole
#             # batch - every other application in this job still gets screened.
#             failures.append({"application_id": application.id, "full_name": application.full_name, "error": str(exc)})

#     db.commit()
#     for result in results:
#         db.refresh(result)

#     logger.info(
#         "Admin %r batch-screened %d application(s) for job %r (%d failed)",
#         current_admin.username,
#         len(results),
#         job_id,
#         len(failures),
#     )
#     message = f"Screened {len(results)} application(s)."
#     if failures:
#         message += f" {len(failures)} couldn't be screened - see details."
#     return ATSScreenAllResponse(
#         message=message,
#         screened_count=len(results),
#         results=[_to_result_read(r) for r in results],
#         failed=failures,
#     )


# @router.get("/applications", response_model=PaginatedATSApplications)
# def list_screened_applications(
#     page: int = Query(1, ge=1),
#     page_size: int = Query(20, ge=1, le=100),
#     job_id: str | None = None,
#     status_filter: str | None = Query(None, alias="status"),
#     recommendation: ATSRecommendation | None = None,
#     min_score: float | None = Query(None, ge=0, le=100),
#     max_score: float | None = Query(None, ge=0, le=100),
#     mandatory_failed: bool | None = Query(None, description="True = only candidates who failed a mandatory criterion."),
#     evaluation_method: ATSEvaluationMode | None = None,
#     sort_by: str = Query("date", pattern="^(date|score)$"),
#     sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
#     db: Session = Depends(get_db),
# ) -> PaginatedATSApplications:
#     """
#     Career applications enriched with their latest ATS screening result (if
#     any). This is a read-only view built on top of career_applications +
#     ats_screening_results - it never modifies either table, and an
#     application with no screening result yet still shows up normally
#     (screening: null) when no ATS filters are applied.
#     """
#     query = db.query(CareerApplication).outerjoin(
#         ATSScreeningResult, ATSScreeningResult.application_id == CareerApplication.id
#     )

#     if job_id:
#         query = query.filter(CareerApplication.job_id == job_id)
#     if status_filter:
#         query = query.filter(CareerApplication.status == status_filter)
#     if recommendation is not None:
#         query = query.filter(ATSScreeningResult.system_recommendation == recommendation)
#     if min_score is not None:
#         query = query.filter(ATSScreeningResult.max_possible_score > 0).filter(
#             (ATSScreeningResult.total_score / ATSScreeningResult.max_possible_score * 100) >= min_score
#         )
#     if max_score is not None:
#         query = query.filter(ATSScreeningResult.max_possible_score > 0).filter(
#             (ATSScreeningResult.total_score / ATSScreeningResult.max_possible_score * 100) <= max_score
#         )
#     if mandatory_failed is True:
#         query = query.filter(ATSScreeningResult.has_failed_mandatory.is_(True))
#     elif mandatory_failed is False:
#         query = query.filter(
#             (ATSScreeningResult.has_failed_mandatory.is_(False)) | (ATSScreeningResult.has_failed_mandatory.is_(None))
#         )
#     if evaluation_method is not None:
#         query = query.filter(ATSScreeningResult.evaluation_method == evaluation_method)

#     total = query.count()

#     if sort_by == "score":
#         order_col = ATSScreeningResult.total_score
#         query = query.order_by(order_col.asc() if sort_dir == "asc" else order_col.desc())
#     else:
#         order_col = CareerApplication.created_at
#         query = query.order_by(order_col.asc() if sort_dir == "asc" else order_col.desc())

#     applications = query.offset((page - 1) * page_size).limit(page_size).all()

#     result_by_app = {
#         r.application_id: r
#         for r in db.query(ATSScreeningResult).filter(
#             ATSScreeningResult.application_id.in_([a.id for a in applications])
#         )
#     }

#     items = []
#     for app in applications:
#         item = CareerApplicationWithATS.model_validate(app)
#         result = result_by_app.get(app.id)
#         item.screening = _to_result_read(result) if result else None
#         items.append(item)

#     total_pages = max(1, -(-total // page_size))
#     return PaginatedATSApplications(
#         meta=PageMeta(page=page, page_size=page_size, total=total, total_pages=total_pages), items=items
#     )


# @router.get("/stats", response_model=ATSStats)
# def get_ats_stats(job_id: str | None = None, db: Session = Depends(get_db)) -> ATSStats:
#     app_query = db.query(CareerApplication)
#     if job_id:
#         app_query = app_query.filter(CareerApplication.job_id == job_id)
#     total_applications = app_query.count()

#     result_query = db.query(ATSScreeningResult)
#     if job_id:
#         result_query = result_query.join(
#             CareerApplication, CareerApplication.id == ATSScreeningResult.application_id
#         ).filter(CareerApplication.job_id == job_id)
#     results = result_query.all()

#     total_screened = len(results)
#     recommended = sum(1 for r in results if r.system_recommendation == ATSRecommendation.recommended)
#     review = sum(1 for r in results if r.system_recommendation == ATSRecommendation.review)
#     not_recommended = sum(1 for r in results if r.system_recommendation == ATSRecommendation.not_recommended)

#     percentages = [
#         (r.total_score / r.max_possible_score * 100) for r in results if r.max_possible_score > 0
#     ]
#     average_score = round(sum(percentages) / len(percentages), 2) if percentages else 0.0

#     return ATSStats(
#         total_applications=total_applications,
#         total_screened=total_screened,
#         total_unscreened=max(0, total_applications - total_screened),
#         recommended_count=recommended,
#         review_count=review,
#         not_recommended_count=not_recommended,
#         average_score_percentage=average_score,
#     )
