import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.ats import ATSAuditAction, ATSAuditLog, ATSRecruiterNote, ATSScreeningResult
from app.models.career_application import CareerApplication
from app.models.job_opening import JobOpening
from app.schemas.career_application import CareerApplicationRead
from app.schemas.job_opening import JobOpeningRead
from app.schemas.ats import (
    ATSAuditLogRead,
    ATSOverrideRequest,
    ATSRecruiterNoteCreate,
    ATSRecruiterNoteRead,
    ATSRecruiterNoteResponse,
    ATSScreenResponse,
    ATSScreeningResultRead,
    ATSVettingDetail,
)
from app.services.auth import get_current_admin

logger = logging.getLogger("bidii.admin_ats_vetting")

router = APIRouter(prefix="/api/admin/ats", tags=["admin-ats-vetting"], dependencies=[Depends(get_current_admin)])


def _get_application_or_404(db: Session, application_id: str) -> CareerApplication:
    application = db.query(CareerApplication).filter(CareerApplication.id == application_id).first()
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Career application not found.")
    return application


def _to_result_read(result: ATSScreeningResult) -> ATSScreeningResultRead:
    data = ATSScreeningResultRead.model_validate(result)
    data.score_percentage = round(
        (result.total_score / result.max_possible_score * 100) if result.max_possible_score > 0 else 0.0, 2
    )
    return data


def _admin_usernames(db: Session, admin_ids: set[str]) -> dict[str, str]:
    admin_ids = {a for a in admin_ids if a}
    if not admin_ids:
        return {}
    rows = db.query(AdminUser.id, AdminUser.username).filter(AdminUser.id.in_(admin_ids)).all()
    return dict(rows)


@router.get("/applications/{application_id}", response_model=ATSVettingDetail)
def get_vetting_detail(application_id: str, db: Session = Depends(get_db)) -> ATSVettingDetail:
    """
    Full candidate vetting view: profile, job applied for, ATS score and
    screening breakdown, recruiter notes, and the audit trail. Reads only —
    an application with no screening yet returns screening: null and simply
    displays normally, same as the existing Career Applications table.
    """
    application = _get_application_or_404(db, application_id)

    job = None
    if application.job_id:
        job = db.query(JobOpening).filter(JobOpening.id == application.job_id).first()

    result = db.query(ATSScreeningResult).filter(ATSScreeningResult.application_id == application_id).first()

    notes = (
        db.query(ATSRecruiterNote)
        .filter(ATSRecruiterNote.application_id == application_id)
        .order_by(ATSRecruiterNote.created_at.desc())
        .all()
    )
    history = (
        db.query(ATSAuditLog)
        .filter(ATSAuditLog.application_id == application_id)
        .order_by(ATSAuditLog.created_at.desc())
        .all()
    )

    usernames = _admin_usernames(db, {n.admin_id for n in notes} | {h.admin_id for h in history if h.admin_id})

    return ATSVettingDetail(
        application=CareerApplicationRead.model_validate(application),
        job=JobOpeningRead.model_validate(job) if job else None,
        screening=_to_result_read(result) if result else None,
        notes=[
            ATSRecruiterNoteRead(
                **ATSRecruiterNoteRead.model_validate(n).model_dump(exclude={"admin_username"}),
                admin_username=usernames.get(n.admin_id),
            )
            for n in notes
        ],
        history=[
            ATSAuditLogRead(
                **ATSAuditLogRead.model_validate(h).model_dump(exclude={"admin_username"}),
                admin_username=usernames.get(h.admin_id) if h.admin_id else None,
            )
            for h in history
        ],
    )


@router.patch("/applications/{application_id}/override", response_model=ATSScreenResponse)
def override_recommendation(
    application_id: str,
    payload: ATSOverrideRequest,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> ATSScreenResponse:
    """
    Lets a recruiter/admin manually set the final recommendation for a
    candidate, independent of (and without erasing) the system's own
    score/recommendation. Requires the candidate to have been screened at
    least once — override is a correction to a screening result, not a
    substitute for running one.
    """
    _get_application_or_404(db, application_id)
    result = db.query(ATSScreeningResult).filter(ATSScreeningResult.application_id == application_id).first()
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This candidate hasn't been screened yet. Run screening before overriding its recommendation.",
        )

    previous_override = result.override_recommendation.value if result.override_recommendation else None

    result.override_recommendation = payload.recommendation
    result.override_reason = payload.reason
    result.override_by = current_admin.id
    result.overridden_at = datetime.now(timezone.utc)

    db.add(
        ATSAuditLog(
            application_id=application_id,
            admin_id=current_admin.id,
            action=ATSAuditAction.recommendation_overridden,
            details={
                "previous_override": previous_override,
                "new_override": payload.recommendation.value,
                "reason": payload.reason,
            },
        )
    )

    db.commit()
    db.refresh(result)

    logger.info(
        "Admin %r overrode ATS recommendation for application %r to %r",
        current_admin.username,
        application_id,
        payload.recommendation.value,
    )
    return ATSScreenResponse(message="Recommendation overridden.", data=_to_result_read(result))


@router.post("/applications/{application_id}/notes", response_model=ATSRecruiterNoteResponse, status_code=status.HTTP_201_CREATED)
def add_recruiter_note(
    application_id: str,
    payload: ATSRecruiterNoteCreate,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> ATSRecruiterNoteResponse:
    _get_application_or_404(db, application_id)

    note = ATSRecruiterNote(application_id=application_id, admin_id=current_admin.id, note=payload.note)
    db.add(note)
    db.add(
        ATSAuditLog(
            application_id=application_id,
            admin_id=current_admin.id,
            action=ATSAuditAction.note_added,
            details={"note_preview": payload.note[:140]},
        )
    )
    db.commit()
    db.refresh(note)

    logger.info("Admin %r added a recruiter note to application %r", current_admin.username, application_id)
    return ATSRecruiterNoteResponse(
        data=ATSRecruiterNoteRead(
            **ATSRecruiterNoteRead.model_validate(note).model_dump(exclude={"admin_username"}),
            admin_username=current_admin.username,
        )
    )


@router.get("/applications/{application_id}/history", response_model=list[ATSAuditLogRead])
def get_history(application_id: str, db: Session = Depends(get_db)) -> list[ATSAuditLogRead]:
    _get_application_or_404(db, application_id)
    history = (
        db.query(ATSAuditLog)
        .filter(ATSAuditLog.application_id == application_id)
        .order_by(ATSAuditLog.created_at.desc())
        .all()
    )
    usernames = _admin_usernames(db, {h.admin_id for h in history if h.admin_id})
    return [
        ATSAuditLogRead(
            **ATSAuditLogRead.model_validate(h).model_dump(exclude={"admin_username"}),
            admin_username=usernames.get(h.admin_id) if h.admin_id else None,
        )
        for h in history
    ]
