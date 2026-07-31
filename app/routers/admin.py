import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.career_application import CareerApplication, CareerApplicationStatus
from app.models.contact import ContactMessage
from app.models.loan_application import LoanApplication, LoanApplicationStatus
from app.schemas.admin import (
    DashboardStats,
    PageMeta,
    PaginatedCareerApplications,
    PaginatedContacts,
    PaginatedLoanApplications,
    ProductBreakdown,
    StatusUpdate,
)
from app.schemas.admin_user import (
    AdminUserCreate,
    AdminUserCreateResponse,
    AdminUserListResponse,
    AdminUserRead,
    AdminUserUpdate,
    AdminUserUpdateResponse,
)
from app.schemas.career_application import CareerApplicationRead
from app.schemas.contact import ContactRead
from app.schemas.loan_application import LoanApplicationRead
from app.services.auth import get_current_admin, hash_password

settings = get_settings()
logger = logging.getLogger("bidii.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(get_current_admin)])


def _page_meta(page: int, page_size: int, total: int) -> PageMeta:
    total_pages = max(1, math.ceil(total / page_size))
    return PageMeta(page=page, page_size=page_size, total=total, total_pages=total_pages)


@router.get("/stats", response_model=DashboardStats)
def get_dashboard_stats(db: Session = Depends(get_db)) -> DashboardStats:
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)

    total_contacts = db.query(func.count(ContactMessage.id)).scalar() or 0
    total_loans = db.query(func.count(LoanApplication.id)).scalar() or 0
    total_careers = db.query(func.count(CareerApplication.id)).scalar() or 0

    loan_status_rows = (
        db.query(LoanApplication.status, func.count(LoanApplication.id))
        .group_by(LoanApplication.status)
        .all()
    )
    loan_by_status = {status_.value: count for status_, count in loan_status_rows}
    for s in LoanApplicationStatus:
        loan_by_status.setdefault(s.value, 0)

    career_status_rows = (
        db.query(CareerApplication.status, func.count(CareerApplication.id))
        .group_by(CareerApplication.status)
        .all()
    )
    career_by_status = {status_.value: count for status_, count in career_status_rows}
    for s in CareerApplicationStatus:
        career_by_status.setdefault(s.value, 0)

    product_rows = (
        db.query(
            LoanApplication.product_slug,
            LoanApplication.product_name,
            func.count(LoanApplication.id),
            func.sum(LoanApplication.amount),
        )
        .group_by(LoanApplication.product_slug, LoanApplication.product_name)
        .all()
    )
    by_product = [
        ProductBreakdown(
            product_slug=slug,
            product_name=name,
            count=count,
            total_amount_requested=float(total or 0),
        )
        for slug, name, count, total in product_rows
    ]

    contacts_recent = (
        db.query(func.count(ContactMessage.id)).filter(ContactMessage.created_at >= seven_days_ago).scalar() or 0
    )
    loans_recent = (
        db.query(func.count(LoanApplication.id)).filter(LoanApplication.created_at >= seven_days_ago).scalar() or 0
    )
    careers_recent = (
        db.query(func.count(CareerApplication.id)).filter(CareerApplication.created_at >= seven_days_ago).scalar() or 0
    )

    total_amount = db.query(func.sum(LoanApplication.amount)).scalar() or 0

    return DashboardStats(
        total_contacts=total_contacts,
        total_loan_applications=total_loans,
        total_career_applications=total_careers,
        loan_applications_by_status=loan_by_status,
        career_applications_by_status=career_by_status,
        loan_applications_by_product=by_product,
        contacts_last_7_days=contacts_recent,
        loan_applications_last_7_days=loans_recent,
        career_applications_last_7_days=careers_recent,
        total_amount_requested_all_time=float(total_amount),
    )


@router.get("/contacts", response_model=PaginatedContacts)
def list_contacts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    subject: str | None = None,
    db: Session = Depends(get_db),
) -> PaginatedContacts:
    query = db.query(ContactMessage)
    if subject:
        query = query.filter(ContactMessage.subject == subject)
    total = query.count()
    items = (
        query.order_by(ContactMessage.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return PaginatedContacts(
        meta=_page_meta(page, page_size, total),
        items=[ContactRead.model_validate(i) for i in items],
    )


@router.get("/loan-applications", response_model=PaginatedLoanApplications)
def list_loan_applications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    product_slug: str | None = None,
    db: Session = Depends(get_db),
) -> PaginatedLoanApplications:
    query = db.query(LoanApplication)
    if status_filter:
        query = query.filter(LoanApplication.status == status_filter)
    if product_slug:
        query = query.filter(LoanApplication.product_slug == product_slug)
    total = query.count()
    items = (
        query.order_by(LoanApplication.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return PaginatedLoanApplications(
        meta=_page_meta(page, page_size, total),
        items=[LoanApplicationRead.model_validate(i) for i in items],
    )


@router.patch("/loan-applications/{application_id}", response_model=LoanApplicationRead)
def update_loan_application_status(
    application_id: str, payload: StatusUpdate, db: Session = Depends(get_db)
) -> LoanApplicationRead:
    record = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan application not found.")

    valid_values = {s.value for s in LoanApplicationStatus}
    if payload.status not in valid_values:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Status must be one of: {', '.join(sorted(valid_values))}",
        )

    record.status = LoanApplicationStatus(payload.status)
    db.commit()
    db.refresh(record)
    return LoanApplicationRead.model_validate(record)


@router.get("/career-applications", response_model=PaginatedCareerApplications)
def list_career_applications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    job_id: str | None = None,
    db: Session = Depends(get_db),
) -> PaginatedCareerApplications:
    query = db.query(CareerApplication)
    if status_filter:
        query = query.filter(CareerApplication.status == status_filter)
    if job_id:
        query = query.filter(CareerApplication.job_id == job_id)
    total = query.count()
    items = (
        query.order_by(CareerApplication.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return PaginatedCareerApplications(
        meta=_page_meta(page, page_size, total),
        items=[CareerApplicationRead.model_validate(i) for i in items],
    )


@router.patch("/career-applications/{application_id}", response_model=CareerApplicationRead)
def update_career_application_status(
    application_id: str, payload: StatusUpdate, db: Session = Depends(get_db)
) -> CareerApplicationRead:
    record = db.query(CareerApplication).filter(CareerApplication.id == application_id).first()
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Career application not found.")

    valid_values = {s.value for s in CareerApplicationStatus}
    if payload.status not in valid_values:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Status must be one of: {', '.join(sorted(valid_values))}",
        )

    record.status = CareerApplicationStatus(payload.status)
    db.commit()
    db.refresh(record)
    return CareerApplicationRead.model_validate(record)


@router.get("/career-applications/{application_id}/cv")
def download_career_application_cv(application_id: str, db: Session = Depends(get_db)) -> FileResponse:
    record = db.query(CareerApplication).filter(CareerApplication.id == application_id).first()
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Career application not found.")

    file_path: Path = settings.upload_dir / "careers" / record.cv_stored_filename
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CV file not found on disk.")

    return FileResponse(
        path=file_path,
        filename=record.cv_original_filename,
        media_type="application/pdf",
    )


# ---------------------------------------------------------------------------
# Admin user management — lets a logged-in admin create additional admin
# accounts from the dashboard, instead of every admin sharing one set of
# env-var credentials.
# ---------------------------------------------------------------------------


@router.get("/users", response_model=AdminUserListResponse)
def list_admin_users(db: Session = Depends(get_db)) -> AdminUserListResponse:
    users = db.query(AdminUser).order_by(AdminUser.created_at.asc()).all()
    return AdminUserListResponse(items=[AdminUserRead.model_validate(u) for u in users])


@router.post("/users", response_model=AdminUserCreateResponse, status_code=status.HTTP_201_CREATED)
def create_admin_user(
    payload: AdminUserCreate,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> AdminUserCreateResponse:
    existing = db.query(AdminUser).filter(AdminUser.username == payload.username).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'An admin with username "{payload.username}" already exists.',
        )

    user = AdminUser(username=payload.username, password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)

    logger.info("Admin user %r created a new admin account: %r", current_admin.username, user.username)

    return AdminUserCreateResponse(data=AdminUserRead.model_validate(user))


@router.delete("/users/{user_id}", response_model=AdminUserRead)
def deactivate_admin_user(
    user_id: str,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> AdminUserRead:
    """
    Soft-deletes (deactivates) an admin account rather than hard-deleting it,
    preserving an audit trail of who created what. Guards against locking
    everyone out: you can't deactivate yourself, and you can't deactivate
    the last remaining active admin.
    """
    user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin user not found.")

    _guard_deactivation(user, current_admin, db)

    user.is_active = False
    db.commit()
    db.refresh(user)
    return AdminUserRead.model_validate(user)


def _guard_deactivation(user: AdminUser, current_admin: AdminUser, db: Session) -> None:
    """
    Shared by DELETE /users/{id} and PATCH /users/{id} (when it sets
    is_active=False) so both paths enforce the same lockout prevention:
    you can't deactivate yourself, and the last remaining active admin
    can't be deactivated by anyone. Compares by ID, not username — usernames
    can change, IDs don't.
    """
    if user.id == current_admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You can't deactivate your own account.")

    active_count = db.query(func.count(AdminUser.id)).filter(AdminUser.is_active.is_(True)).scalar() or 0
    if user.is_active and active_count <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Can't deactivate the last remaining active admin account.",
        )


@router.patch("/users/{user_id}", response_model=AdminUserUpdateResponse)
def update_admin_user(
    user_id: str,
    payload: AdminUserUpdate,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> AdminUserUpdateResponse:
    """
    Updates one or more fields on an admin account: username, password,
    and/or is_active (so this endpoint also covers reactivating a
    previously-deactivated admin, not just editing active ones).
    Only fields actually present in the request body are changed.
    """
    user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin user not found.")

    if payload.username is not None and payload.username != user.username:
        existing = db.query(AdminUser).filter(AdminUser.username == payload.username).first()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'An admin with username "{payload.username}" already exists.',
            )
        user.username = payload.username
        # No session-invalidation workaround needed here — the JWT subject
        # is current_admin.id, which doesn't change when a username does.

    if payload.password is not None:
        user.password_hash = hash_password(payload.password)

    if payload.is_active is not None and payload.is_active != user.is_active:
        if payload.is_active is False:
            _guard_deactivation(user, current_admin, db)
        user.is_active = payload.is_active

    db.commit()
    db.refresh(user)

    logger.info("Admin user %r updated account %r", current_admin.username, user.username)

    return AdminUserUpdateResponse(data=AdminUserRead.model_validate(user))
