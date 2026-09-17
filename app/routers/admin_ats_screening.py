import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, get_db
from app.models.admin_user import AdminUser
from app.models.ats import (
    ATSAIProviderName,
    ATSAuditAction,
    ATSAuditLog,
    ATSBatchJob,
    ATSBatchJobStatus,
    ATSConfiguration,
    ATSEvaluationMode,
    ATSRecommendation,
    ATSScreeningResult,
)
from app.models.career_application import CareerApplication, CareerApplicationStatus
from app.models.job_opening import JobOpening
from app.schemas.admin import PageMeta
from app.schemas.ats import (
    ATSBatchJobStartResponse,
    ATSBatchJobStatusResponse,
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

# Maps an in-progress async batch's ATSBatchJob.id -> the threading.Event
# its worker pool watches (see _run_batch_screening_job). Lets
# cancel_batch_screening, running in a *different* request/thread, signal
# a specific running batch to stop. Entries are added when a batch starts
# and removed when it finishes (cancelled, completed, or rate-limit
# stopped) - this dict never grows unbounded. In-process only, same
# limitation as the progress lock: a backend restart mid-batch loses the
# ability to signal that batch (the DB's cancel_requested flag still
# records the request, there's just nothing left running to receive it).
_active_batch_stop_events: dict[str, threading.Event] = {}
_active_batch_stop_events_lock = threading.Lock()


class ScreeningUnavailableError(Exception):
    """AI evaluation failed and this job has no weighted criteria to fall back to."""


def _reconcile_if_orphaned(db: Session, batch_job: ATSBatchJob) -> ATSBatchJob:
    """
    A batch_job row can be left stuck at status=='running' forever if the
    process actually driving it (_run_batch_screening_job below) never
    reaches its own finalize step - most commonly this backend being
    killed/restarted mid-batch, but also any bug that somehow escapes that
    function's blanket exception handling. _active_batch_stop_events only
    holds entries for batches actively running *in this process*; a
    'running' row with no matching entry here has nothing left able to
    move it to 'completed'/'cancelled', poll it, or respond to a cancel
    request against it (cancel_requested gets set, but there's no
    stop_event left to signal - see that field's docstring).

    Without this, the admin UI's poll loop (which only stops once status
    != 'running') spins forever after a restart, and "Stop batch
    screening" against an orphaned batch silently does nothing.

    Detected lazily, wherever a batch_job's status is read, and finalized
    as 'failed' (not 'cancelled' - no admin asked for this, the process
    just disappeared) with a stopped_reason explaining what happened.
    Already-screened candidates are untouched; re-running the batch picks
    up where it left off. Safe to call repeatedly - a no-op once the row
    isn't 'running', or while it's genuinely still running in this process.
    """
    if batch_job.status != ATSBatchJobStatus.running:
        return batch_job
    with _active_batch_stop_events_lock:
        still_active = batch_job.id in _active_batch_stop_events
    if still_active:
        return batch_job

    batch_job.status = ATSBatchJobStatus.failed
    batch_job.finished_at = datetime.now(timezone.utc)
    batch_job.stopped_reason = (
        "Interrupted - the backend process running this batch stopped (e.g. a restart or crash) before "
        "it could finish. Already-screened candidates were kept; re-run this batch to pick up where it "
        "left off (already-screened candidates are skipped unless you rescore all)."
    )
    db.add(batch_job)
    db.commit()
    db.refresh(batch_job)
    logger.warning(
        "Batch %r for job %r was orphaned (stuck at 'running' with no active worker in this process) - marked failed.",
        batch_job.id,
        batch_job.job_id,
    )
    return batch_job


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
    """The weighted-scoring engine (app/services/ats_scoring.py) - now CV-aware, see that module's docstring."""
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
    # returns - so a genuine fallback still shows its banner, but a plain
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
    Dispatches to AI or weighted evaluation. Uses `mode` if given - an
    explicit one-off override so a candidate can be re-screened with the
    *other* engine without changing the job's saved evaluation_mode - and
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


def _run_batch_screening_job(
    batch_job_id: str,
    job_id: str,
    application_ids: list[str],
    admin_id: str | None,
    method: ATSEvaluationMode | None,
) -> None:
    """
    Runs in the background (via FastAPI's BackgroundTasks, which executes
    sync callables in their own thread) after screen_all_for_job_async
    below has already returned its response. Opens its own DB session -
    the request's session is closed by the time this runs - and fans
    candidates out across a small thread pool, since the AI provider SDK
    calls inside _run_screening are blocking, not async; asyncio wouldn't
    actually run them concurrently, but threads do.

    Every candidate gets its own SessionLocal() too, so no SQLAlchemy
    session is ever touched by more than one thread at once - sessions
    aren't thread-safe. Progress on the shared ATSBatchJob row is updated
    under `progress_lock` so concurrent workers can't lose an update to
    `completed`/`failed_count`. This only guards against races within this
    one process; if this backend is ever run with multiple worker
    processes, each would need its own coordination (not the case today).

    One candidate's failure (AI + no weighted fallback, missing CV, etc.)
    never aborts the batch - it's recorded in `failures` and the run moves
    on, exactly like the existing synchronous screen_all_for_job. The run
    stops dispatching new candidates (already-started ones still finish)
    in two cases: repeated rate-limit failures in a row (past
    ai_batch_rate_limit_stop_threshold - likely a daily quota, no point
    burning the rest of it on requests that will also fail), or an admin
    cancelling via POST .../cancel. Either way `stop_event` is what
    actually halts dispatch; which one happened is told apart at the end
    by checking the DB's cancel_requested flag, which only the cancel
    endpoint sets.
    """
    settings = get_settings()
    progress_lock = threading.Lock()
    stop_event = threading.Event()
    with _active_batch_stop_events_lock:
        _active_batch_stop_events[batch_job_id] = stop_event
    consecutive_rate_limit_failures = [0]  # single-item list = mutable counter closures can update

    def screen_one(application_id: str) -> None:
        if stop_event.is_set():
            return
        worker_db = SessionLocal()
        full_name = None
        is_rate_limit_failure = False
        try:
            # The in-process stop_event above is a fast path that only
            # works if the cancel request happens to land on this same
            # process. If this backend ever runs more than one worker
            # process, a cancel request could hit a different one and
            # that event would never fire here - this DB read is the
            # actually-authoritative check, since the flag it reads is
            # visible from any process. Cheap: one indexed lookup,
            # already paying for a DB round trip per candidate anyway.
            if worker_db.query(ATSBatchJob.cancel_requested).filter(ATSBatchJob.id == batch_job_id).scalar():
                stop_event.set()  # also fast-stops same-process queued items immediately
                return

            application = worker_db.query(CareerApplication).filter(CareerApplication.id == application_id).first()
            worker_job = worker_db.query(JobOpening).filter(JobOpening.id == job_id).first()
            worker_config = worker_db.query(ATSConfiguration).filter(ATSConfiguration.job_id == job_id).first()
            if application is None:
                raise ScreeningUnavailableError("This application no longer exists.")
            full_name = application.full_name
            if worker_job is None or worker_config is None:
                raise ScreeningUnavailableError("The job posting or its ATS configuration no longer exists.")

            previous_status = application.status
            try:
                _run_screening(worker_db, application, worker_job, worker_config, admin_id, mode=method)
                worker_db.commit()
                if application.status != previous_status:
                    maybe_auto_notify(worker_db, application, application.status.value)
            except ScreeningUnavailableError as exc:
                worker_db.rollback()
                is_rate_limit_failure = "rate limit" in str(exc).lower()
                raise
        except ScreeningUnavailableError as exc:
            with progress_lock:
                batch_job = worker_db.query(ATSBatchJob).filter(ATSBatchJob.id == batch_job_id).first()
                if batch_job is None:
                    return
                batch_job.failed_count += 1
                batch_job.failures = [
                    *batch_job.failures,
                    {"application_id": application_id, "full_name": full_name, "error": str(exc)},
                ]
                if is_rate_limit_failure:
                    consecutive_rate_limit_failures[0] += 1
                    if consecutive_rate_limit_failures[0] >= settings.ai_batch_rate_limit_stop_threshold:
                        stop_event.set()
                        batch_job.stopped_reason = (
                            "Stopped early after repeated AI provider rate-limit errors - likely a daily quota "
                            "limit. Remaining candidates were not screened; re-run this batch later to pick up "
                            "where it left off (already-screened candidates are skipped unless you rescore all)."
                        )
                else:
                    consecutive_rate_limit_failures[0] = 0
                worker_db.commit()
        except Exception as exc:  # never let one candidate's unexpected bug kill the whole batch
            logger.exception("Unexpected error screening application %r in batch %r", application_id, batch_job_id)
            with progress_lock:
                batch_job = worker_db.query(ATSBatchJob).filter(ATSBatchJob.id == batch_job_id).first()
                if batch_job is not None:
                    batch_job.failed_count += 1
                    batch_job.failures = [
                        *batch_job.failures,
                        {"application_id": application_id, "full_name": full_name, "error": str(exc)},
                    ]
                    worker_db.commit()
        else:
            with progress_lock:
                batch_job = worker_db.query(ATSBatchJob).filter(ATSBatchJob.id == batch_job_id).first()
                if batch_job is not None:
                    batch_job.completed += 1
                    worker_db.commit()
        finally:
            worker_db.close()

    max_workers = max(1, min(settings.ai_batch_max_workers, len(application_ids)))
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(screen_one, application_ids))
    finally:
        with _active_batch_stop_events_lock:
            _active_batch_stop_events.pop(batch_job_id, None)

    finalize_db = SessionLocal()
    try:
        batch_job = finalize_db.query(ATSBatchJob).filter(ATSBatchJob.id == batch_job_id).first()
        if batch_job is not None:
            batch_job.status = ATSBatchJobStatus.cancelled if batch_job.cancel_requested else ATSBatchJobStatus.completed
            batch_job.finished_at = datetime.now(timezone.utc)
            if batch_job.cancel_requested and not batch_job.stopped_reason:
                batch_job.stopped_reason = "Cancelled by an admin. Already-started candidates finished; the rest were skipped."
            finalize_db.commit()
        logger.info(
            "Batch %r for job %r finished: %d/%d completed, %d failed%s",
            batch_job_id,
            job_id,
            batch_job.completed if batch_job else -1,
            len(application_ids),
            batch_job.failed_count if batch_job else -1,
            " (stopped early)" if stop_event.is_set() else "",
        )
    finally:
        finalize_db.close()


@router.post("/jobs/{job_id}/screen-all/async", response_model=ATSBatchJobStartResponse)
def screen_all_for_job_async(
    job_id: str,
    background_tasks: BackgroundTasks,
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
) -> ATSBatchJobStartResponse:
    """
    Non-blocking counterpart to POST /jobs/{job_id}/screen-all above, meant
    for large batches (hundreds to 1000+ candidates) where waiting on a
    single HTTP request for the whole run isn't practical - that request
    would likely hit a timeout (browser, proxy, or host) long before
    finishing. Returns immediately with a batch_job_id; poll
    GET .../screen-all/status/{batch_job_id} to watch progress and see
    each candidate's score land as it's screened.

    The original synchronous /screen-all endpoint above, and the
    single-candidate /applications/{id}/screen endpoint, are both
    completely untouched - this is a new, additive endpoint only.
    """
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

    batch_job = ATSBatchJob(job_id=job_id, admin_id=current_admin.id, total=len(applications))
    db.add(batch_job)
    db.commit()
    db.refresh(batch_job)

    if applications:
        background_tasks.add_task(
            _run_batch_screening_job,
            batch_job.id,
            job_id,
            [a.id for a in applications],
            current_admin.id,
            method,
        )
        message = f"Screening {len(applications)} application(s) in the background."
    else:
        batch_job.status = ATSBatchJobStatus.completed
        batch_job.finished_at = datetime.now(timezone.utc)
        db.commit()
        message = "No unscreened applications to screen."

    logger.info(
        "Admin %r started async batch screening (batch_job=%r) for job %r: %d application(s)",
        current_admin.username,
        batch_job.id,
        job_id,
        len(applications),
    )
    return ATSBatchJobStartResponse(batch_job_id=batch_job.id, total=len(applications), message=message)


@router.get("/jobs/{job_id}/screen-all/status/{batch_job_id}", response_model=ATSBatchJobStatusResponse)
def get_batch_screening_status(
    job_id: str,
    batch_job_id: str,
    db: Session = Depends(get_db),
) -> ATSBatchJobStatusResponse:
    """Poll target for the admin UI while an async batch (above) runs."""
    batch_job = (
        db.query(ATSBatchJob).filter(ATSBatchJob.id == batch_job_id, ATSBatchJob.job_id == job_id).first()
    )
    if batch_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch screening job not found.")
    batch_job = _reconcile_if_orphaned(db, batch_job)
    return ATSBatchJobStatusResponse.model_validate(batch_job)


@router.get("/jobs/{job_id}/screen-all/active", response_model=ATSBatchJobStatusResponse | None)
def get_active_batch_screening(
    job_id: str,
    db: Session = Depends(get_db),
) -> ATSBatchJobStatusResponse | None:
    """
    Is there a batch already running for this job? Called on page load /
    when the selected job changes, so a page refresh (or opening the page
    from a different tab) resumes watching an in-progress batch instead of
    losing track of it - the admin UI has no other client-side memory of
    which batch_job_id was running. Returns null (not 404) when there's
    nothing running - "no active batch" isn't an error case.
    """
    batch_job = (
        db.query(ATSBatchJob)
        .filter(ATSBatchJob.job_id == job_id, ATSBatchJob.status == ATSBatchJobStatus.running)
        .order_by(ATSBatchJob.created_at.desc())
        .first()
    )
    if batch_job is None:
        return None
    batch_job = _reconcile_if_orphaned(db, batch_job)
    if batch_job.status != ATSBatchJobStatus.running:
        # It was orphaned and just got finalized above - there's no longer
        # an active batch to resume watching, same as if none existed.
        return None
    return ATSBatchJobStatusResponse.model_validate(batch_job)


@router.post("/jobs/{job_id}/screen-all/{batch_job_id}/cancel", response_model=ATSBatchJobStatusResponse)
def cancel_batch_screening(
    job_id: str,
    batch_job_id: str,
    db: Session = Depends(get_db),
) -> ATSBatchJobStatusResponse:
    """
    Requests early stop of a running async batch. This is a *graceful*
    stop, not an instant kill: candidates whose AI call is already
    in-flight at the moment this is called still finish and get recorded
    (safer than aborting a write mid-flight) - only candidates that
    haven't started yet are skipped. Calling this on a batch that isn't
    currently running is a harmless no-op; it just returns that batch's
    current (already-final) status.
    """
    batch_job = (
        db.query(ATSBatchJob).filter(ATSBatchJob.id == batch_job_id, ATSBatchJob.job_id == job_id).first()
    )
    if batch_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch screening job not found.")

    batch_job = _reconcile_if_orphaned(db, batch_job)

    if batch_job.status == ATSBatchJobStatus.running:
        batch_job.cancel_requested = True
        db.commit()
        db.refresh(batch_job)
        with _active_batch_stop_events_lock:
            stop_event = _active_batch_stop_events.get(batch_job_id)
        if stop_event is not None:
            stop_event.set()

    return ATSBatchJobStatusResponse.model_validate(batch_job)


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
# import threading
# from concurrent.futures import ThreadPoolExecutor
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
#     ATSBatchJob,
#     ATSBatchJobStatus,
#     ATSConfiguration,
#     ATSEvaluationMode,
#     ATSRecommendation,
#     ATSScreeningResult,
# )
# from app.models.career_application import CareerApplication, CareerApplicationStatus
# from app.models.job_opening import JobOpening
# from app.schemas.admin import PageMeta
# from app.schemas.ats import (
#     ATSBatchJobStartResponse,
#     ATSBatchJobStatusResponse,
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
# from app.services.ats_scoring import bucket_recommendation, score_application
# from app.services.auth import get_current_admin
# from app.services.cv_text_extraction import extract_cv_text
# from app.services.notifications import maybe_auto_notify
# from app.services.role_permissions import require_menu_access

# logger = logging.getLogger("bidii.admin_ats_screening")

# router = APIRouter(
#     prefix="/api/admin/ats/screening",
#     tags=["admin-ats-screening"],
#     dependencies=[Depends(get_current_admin), Depends(require_menu_access("/admin/ats"))],
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
#     """The weighted-scoring engine (app/services/ats_scoring.py) - now CV-aware, see that module's docstring."""
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
#     # returns - so a genuine fallback still shows its banner, but a plain
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
#     weighted scoring.

#     When this job has configured weighted criteria, the AI is asked to
#     verdict each one individually and the score/recommendation are
#     computed here deterministically (see bucket_recommendation in
#     ats_scoring.py) from those verdicts and this job's own configured
#     weights/thresholds - never trusted from the model's own self-reported
#     score or recommendation label, so the two can no longer disagree with
#     each other. This also means a job with configured mandatory criteria
#     AND auto_reject_enabled now auto-rejects on a failed mandatory
#     criterion in AI mode too, exactly like weighted mode already did -
#     previously AI mode had no concept of "mandatory" at all, so
#     auto_reject_enabled was silently a no-op for any job screened with
#     AI. A job with no configured criteria still gets a free-text
#     evaluation and never auto-rejects, since there's nothing "mandatory"
#     to fail in that case.
#     """
#     settings = get_settings()
#     provider_name = config.ai_provider.value if config.ai_provider else None
#     if not provider_name:
#         raise AIProviderError("This job is set to AI Evaluation but no AI provider is selected.")
#     model = config.ai_model or default_model_for(provider_name)

#     ai_result = evaluate_candidate_with_ai(
#         job=job,
#         application=application,
#         config=config,
#         provider_name=provider_name,
#         model=model,
#         timeout_seconds=settings.ai_request_timeout_seconds,
#     )

#     has_failed_mandatory = bool(ai_result.failed_mandatory_criteria)
#     has_inconsistent = bool(ai_result.inconsistent_criteria)
#     if ai_result.criteria_aware and has_inconsistent:
#         # At least one criterion's verdict didn't reproduce across the two
#         # evaluation runs (see ats_ai_evaluation.py) - the score itself is
#         # still computed treating that criterion as unmet (conservative),
#         # but the overall recommendation is forced to "review" rather than
#         # trusting bucket_recommendation on a score that's partly built on
#         # a disagreement. This also means auto-reject never fires purely
#         # because of an inconsistent (as opposed to a consistently failed)
#         # mandatory criterion - see the auto-reject check below.
#         recommendation = ATSRecommendation.review
#     else:
#         recommendation = bucket_recommendation(ai_result.score_percentage, has_failed_mandatory, config)

#     result, previous_recommendation = _get_or_create_result(db, application.id)
#     result.config_id = config.id
#     result.total_score = ai_result.score_percentage
#     result.max_possible_score = 100.0
#     result.system_recommendation = recommendation
#     if ai_result.criteria_aware:
#         result.matched_criteria = ai_result.matched_criteria
#         result.missing_criteria = ai_result.missing_criteria
#         result.failed_mandatory_criteria = ai_result.failed_mandatory_criteria
#     else:
#         result.matched_criteria = [{"label": r.label, "detail": r.detail} for r in ai_result.matched_requirements]
#         result.missing_criteria = [{"label": r.label, "detail": r.detail} for r in ai_result.missing_requirements]
#         result.failed_mandatory_criteria = []
#     result.has_failed_mandatory = has_failed_mandatory
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
#                 "criteria_aware": ai_result.criteria_aware,
#                 "inconsistent_criteria_count": len(ai_result.inconsistent_criteria),
#                 "previous_recommendation": previous_recommendation,
#                 "new_recommendation": recommendation.value,
#                 "score_percentage": ai_result.score_percentage,
#                 "cv_text_used": ai_result.cv_text_used,
#                 **({"manual_override": manual_method_override} if manual_method_override else {}),
#             },
#         )
#     )

#     if (
#         ai_result.criteria_aware
#         and config.auto_reject_enabled
#         and has_failed_mandatory
#         and application.status != CareerApplicationStatus.rejected
#     ):
#         application.status = CareerApplicationStatus.rejected
#         db.add(
#             ATSAuditLog(
#                 application_id=application.id,
#                 admin_id=admin_id,
#                 action=ATSAuditAction.auto_rejected,
#                 details={"reason": "Failed one or more mandatory criteria (AI evaluation) with auto-reject enabled."},
#             )
#         )

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
#     Dispatches to AI or weighted evaluation. Uses `mode` if given - an
#     explicit one-off override so a candidate can be re-screened with the
#     *other* engine without changing the job's saved evaluation_mode - and
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

#     previous_status = application.status
#     try:
#         result = _run_screening(db, application, job, config, current_admin.id, mode=method)
#     except ScreeningUnavailableError as exc:
#         db.commit()
#         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

#     db.commit()
#     db.refresh(result)

#     if application.status != previous_status:
#         maybe_auto_notify(db, application, application.status.value)

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
#     previous_statuses = {a.id: a.status for a in applications}
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
#     for application in applications:
#         db.refresh(application)
#         if application.status != previous_statuses.get(application.id):
#             maybe_auto_notify(db, application, application.status.value)

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
