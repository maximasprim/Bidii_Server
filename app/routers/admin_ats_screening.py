import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.ats import ATSAuditAction, ATSAuditLog, ATSConfiguration, ATSRecommendation, ATSScreeningResult
from app.models.career_application import CareerApplication, CareerApplicationStatus
from app.schemas.admin import PageMeta
from app.schemas.ats import (
    ATSScreenAllResponse,
    ATSScreenResponse,
    ATSScreeningResultRead,
    ATSStats,
    CareerApplicationWithATS,
    PaginatedATSApplications,
)
from app.services.ats_scoring import score_application
from app.services.auth import get_current_admin

logger = logging.getLogger("bidii.admin_ats_screening")

router = APIRouter(
    prefix="/api/admin/ats/screening", tags=["admin-ats-screening"], dependencies=[Depends(get_current_admin)]
)


def _to_result_read(result: ATSScreeningResult) -> ATSScreeningResultRead:
    data = ATSScreeningResultRead.model_validate(result)
    data.score_percentage = round(
        (result.total_score / result.max_possible_score * 100) if result.max_possible_score > 0 else 0.0, 2
    )
    return data


def _run_screening(
    db: Session, application: CareerApplication, config: ATSConfiguration, admin_id: str | None
) -> ATSScreeningResult:
    outcome = score_application(application, config)

    result = db.query(ATSScreeningResult).filter(ATSScreeningResult.application_id == application.id).first()
    previous_recommendation = result.system_recommendation.value if result else None

    if result is None:
        result = ATSScreeningResult(application_id=application.id)
        db.add(result)

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

    db.add(
        ATSAuditLog(
            application_id=application.id,
            admin_id=admin_id,
            action=ATSAuditAction.screened,
            details={
                "previous_recommendation": previous_recommendation,
                "new_recommendation": outcome.recommendation.value,
                "score_percentage": outcome.score_percentage,
                "failed_mandatory_count": len(outcome.failed_mandatory),
            },
        )
    )

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


@router.post("/applications/{application_id}/screen", response_model=ATSScreenResponse)
def screen_application(
    application_id: str,
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

    result = _run_screening(db, application, config, current_admin.id)
    db.commit()
    db.refresh(result)

    logger.info("Admin %r screened application %r (score=%s%%)", current_admin.username, application_id, result.total_score)
    return ATSScreenResponse(data=_to_result_read(result))


@router.post("/jobs/{job_id}/screen-all", response_model=ATSScreenAllResponse)
def screen_all_for_job(
    job_id: str,
    rescore_all: bool = Query(False, description="If true, re-screen every application including already-screened ones."),
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> ATSScreenAllResponse:
    config = db.query(ATSConfiguration).filter(ATSConfiguration.job_id == job_id).first()
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This job has no ATS configuration yet.")
    if not config.is_scoring_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Automatic scoring is disabled for this job.")

    applications = db.query(CareerApplication).filter(CareerApplication.job_id == job_id).all()
    if not rescore_all:
        already_screened_ids = {
            row[0]
            for row in db.query(ATSScreeningResult.application_id)
            .filter(ATSScreeningResult.application_id.in_([a.id for a in applications]))
            .all()
        }
        applications = [a for a in applications if a.id not in already_screened_ids]

    results = [_run_screening(db, app, config, current_admin.id) for app in applications]
    db.commit()
    for result in results:
        db.refresh(result)

    logger.info(
        "Admin %r batch-screened %d application(s) for job %r", current_admin.username, len(results), job_id
    )
    return ATSScreenAllResponse(
        message=f"Screened {len(results)} application(s).",
        screened_count=len(results),
        results=[_to_result_read(r) for r in results],
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
    sort_by: str = Query("date", pattern="^(date|score)$"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
) -> PaginatedATSApplications:
    """
    Career applications enriched with their latest ATS screening result (if
    any). This is a read-only view built on top of career_applications +
    ats_screening_results — it never modifies either table, and an
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
