import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from app.config import get_settings
from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.branch import Branch
from app.models.career_application import CareerApplication, CareerApplicationStatus
from app.models.contact import ContactMessage
from app.models.loan_application import LoanApplication, LoanApplicationStatus
from app.schemas.admin import (
    DashboardStats,
    LoanApplicationAssignRequest,
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
from app.services.auth import get_current_admin, hash_password, require_roles
from app.services.loan_application_presenter import to_loan_application_read, to_loan_application_read_list
from app.services.notifications import maybe_auto_notify
from app.services.role_permissions import require_menu_access
from app.services.storage import supabase, BUCKET

settings = get_settings()
logger = logging.getLogger("bidii.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(get_current_admin)])


def _page_meta(page: int, page_size: int, total: int) -> PageMeta:
    total_pages = max(1, math.ceil(total / page_size))
    return PageMeta(page=page, page_size=page_size, total=total, total_pages=total_pages)


@router.get("/stats", response_model=DashboardStats, dependencies=[Depends(require_menu_access("/admin"))])
def get_dashboard_stats(
    db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)
) -> DashboardStats:
    """
    admin/hr/marketing_manager see company-wide figures, unrestricted -
    unchanged from before. branch_office_admin and loan_officer see only
    their own branch's loan figures - contacts and career-application
    stats are zeroed out for them entirely, since those aren't areas
    either role has menu access to anyway (see DEFAULT_MENU_ACCESS) and
    showing them numbers for data they can't open would just be
    confusing, not useful.

    "Their own branch's loan figures" means: branch_office_admin sees
    every application across all of managed_branch_ids (their whole
    area), loan_officer sees every application at their single home
    branch (branch_id) - not narrowed further to only applications
    assigned to them personally. That's a deliberate difference from the
    Loan Applications list page, which does scope a loan_officer down to
    just their own assigned queue - the list is "what do I need to work
    on", the Overview here is "how is my branch doing", and those are
    reasonably different questions with different scopes.
    """
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)

    branch_scoped = current_admin.role in ("branch_office_admin", "loan_officer")
    loan_base = db.query(LoanApplication)
    if current_admin.role == "branch_office_admin":
        loan_base = loan_base.filter(LoanApplication.assigned_branch_id.in_(current_admin.managed_branch_ids or []))
    elif current_admin.role == "loan_officer":
        loan_base = loan_base.filter(LoanApplication.assigned_branch_id == current_admin.branch_id)

    total_contacts = 0 if branch_scoped else (db.query(func.count(ContactMessage.id)).scalar() or 0)
    total_loans = loan_base.with_entities(func.count(LoanApplication.id)).scalar() or 0
    total_careers = 0 if branch_scoped else (db.query(func.count(CareerApplication.id)).scalar() or 0)

    loan_status_rows = (
        loan_base.with_entities(LoanApplication.status, func.count(LoanApplication.id))
        .group_by(LoanApplication.status)
        .all()
    )
    loan_by_status = {status_.value: count for status_, count in loan_status_rows}
    for s in LoanApplicationStatus:
        loan_by_status.setdefault(s.value, 0)

    if branch_scoped:
        career_by_status: dict[str, int] = {s.value: 0 for s in CareerApplicationStatus}
    else:
        career_status_rows = (
            db.query(CareerApplication.status, func.count(CareerApplication.id))
            .group_by(CareerApplication.status)
            .all()
        )
        career_by_status = {status_.value: count for status_, count in career_status_rows}
        for s in CareerApplicationStatus:
            career_by_status.setdefault(s.value, 0)

    product_rows = (
        loan_base.with_entities(
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
        0
        if branch_scoped
        else (db.query(func.count(ContactMessage.id)).filter(ContactMessage.created_at >= seven_days_ago).scalar() or 0)
    )
    loans_recent = (
        loan_base.with_entities(func.count(LoanApplication.id))
        .filter(LoanApplication.created_at >= seven_days_ago)
        .scalar()
        or 0
    )
    careers_recent = (
        0
        if branch_scoped
        else (db.query(func.count(CareerApplication.id)).filter(CareerApplication.created_at >= seven_days_ago).scalar() or 0)
    )

    total_amount = loan_base.with_entities(func.sum(LoanApplication.amount)).scalar() or 0

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


@router.get(
    "/loan-applications",
    response_model=PaginatedLoanApplications,
    dependencies=[Depends(require_menu_access("/admin/loan-applications"))],
)
def list_loan_applications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    product_slug: str | None = None,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> PaginatedLoanApplications:
    """
    What a role sees here differs, since this same page/endpoint serves
    three different jobs (see app/services/role_permissions.py's comment
    on the "branch_office_admin" menu entry):
    - admin: every application, unrestricted - unchanged from before.
    - branch_office_admin: only applications routed to one of their
      managed_branch_ids - their inbox to triage and assign to officers.
    - loan_officer: only applications already assigned specifically to
      them - this is a real, intentional behavior change from before,
      when a loan_officer saw every application with no scoping at all
      (there was no assignment concept yet to scope by).
    Any other role reaching here (shouldn't be possible given the menu
    gate above, but defaults matter) sees nothing, not everything.
    """
    query = db.query(LoanApplication)
    if current_admin.role == "branch_office_admin":
        query = query.filter(LoanApplication.assigned_branch_id.in_(current_admin.managed_branch_ids or []))
    elif current_admin.role == "loan_officer":
        query = query.filter(LoanApplication.assigned_loan_officer_id == current_admin.id)
    elif current_admin.role != "admin":
        query = query.filter(False)  # noqa: E712 - safe default: unrecognised role sees nothing, not everything

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
        items=to_loan_application_read_list(db, items),
    )


def _assert_can_touch_application(record: LoanApplication, current_admin: AdminUser) -> None:
    """Shared guard for the status-update and assign endpoints below."""
    if current_admin.role == "admin":
        return
    if current_admin.role == "branch_office_admin":
        if record.assigned_branch_id and record.assigned_branch_id in (current_admin.managed_branch_ids or []):
            return
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This application isn't in one of your managed branches.")
    if current_admin.role == "loan_officer":
        if record.assigned_loan_officer_id == current_admin.id:
            return
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This application isn't assigned to you.")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted.")


@router.patch(
    "/loan-applications/{application_id}",
    response_model=LoanApplicationRead,
    dependencies=[Depends(require_menu_access("/admin/loan-applications"))],
)
def update_loan_application_status(
    application_id: str,
    payload: StatusUpdate,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> LoanApplicationRead:
    record = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan application not found.")
    _assert_can_touch_application(record, current_admin)

    valid_values = {s.value for s in LoanApplicationStatus}
    if payload.status not in valid_values:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Status must be one of: {', '.join(sorted(valid_values))}",
        )

    record.status = LoanApplicationStatus(payload.status)
    db.commit()
    db.refresh(record)
    return to_loan_application_read(db, record)


@router.patch(
    "/loan-applications/{application_id}/assign",
    response_model=LoanApplicationRead,
    dependencies=[Depends(require_menu_access("/admin/loan-applications"))],
)
def assign_loan_application(
    application_id: str,
    payload: LoanApplicationAssignRequest,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> LoanApplicationRead:
    """
    Reassigns a loan application's branch and/or hands it to a specific
    loan officer. Only admin and branch_office_admin can call this -
    loan_officer accounts receive assignments, they don't make them.
    """
    if current_admin.role not in ("admin", "branch_office_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted.")

    record = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan application not found.")
    _assert_can_touch_application(record, current_admin)

    if payload.assigned_branch_id is not None:
        branch = db.query(Branch).filter(Branch.id == payload.assigned_branch_id).first()
        if branch is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="branch_id doesn't match a real branch.")
        if current_admin.role == "branch_office_admin" and branch.id not in (current_admin.managed_branch_ids or []):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You don't manage that branch.")
        record.assigned_branch_id = branch.id
        record.branch_assignment_method = "manual"
        # Reassigning branch clears any existing officer assignment - an
        # officer at the old branch isn't a valid assignee at the new one,
        # and silently leaving it set would be a worse bug than requiring
        # a fresh assignment.
        record.assigned_loan_officer_id = None

    if payload.assigned_loan_officer_id is not None:
        officer = db.query(AdminUser).filter(
            AdminUser.id == payload.assigned_loan_officer_id, AdminUser.role == "loan_officer"
        ).first()
        if officer is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No loan officer with that id.")
        if officer.branch_id != record.assigned_branch_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That loan officer isn't based at this application's assigned branch.",
            )
        record.assigned_loan_officer_id = officer.id

    db.commit()
    db.refresh(record)
    return to_loan_application_read(db, record)


@router.get(
    "/loan-applications/branch-officers",
    response_model=list[AdminUserRead],
    dependencies=[Depends(require_menu_access("/admin/loan-applications"))],
)
def list_branch_loan_officers(
    branch_id: str,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> list[AdminUserRead]:
    """Loan officers based at one branch — populates the assignment dropdown for that branch."""
    if current_admin.role == "branch_office_admin" and branch_id not in (current_admin.managed_branch_ids or []):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You don't manage that branch.")
    if current_admin.role not in ("admin", "branch_office_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted.")

    officers = (
        db.query(AdminUser)
        .filter(AdminUser.role == "loan_officer", AdminUser.branch_id == branch_id, AdminUser.is_active.is_(True))
        .order_by(AdminUser.username.asc())
        .all()
    )
    return [AdminUserRead.model_validate(o) for o in officers]


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

    previous_status = record.status
    record.status = CareerApplicationStatus(payload.status)
    db.commit()
    db.refresh(record)

    if record.status != previous_status:
        # Never allowed to fail this request - see maybe_auto_notify's docstring.
        maybe_auto_notify(db, record, record.status.value)

    return CareerApplicationRead.model_validate(record)


# @router.get("/career-applications/{application_id}/cv")
# def download_career_application_cv(application_id: str, db: Session = Depends(get_db)) -> FileResponse:
#     record = db.query(CareerApplication).filter(CareerApplication.id == application_id).first()
#     if record is None:
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Career application not found.")

#     file_path: Path = settings.upload_dir / "careers" / record.cv_stored_filename
#     if not file_path.exists():
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CV file not found on disk.")

#     return FileResponse(
#         path=file_path,
#         filename=record.cv_original_filename,
#         media_type="application/pdf",
#     )
@router.get("/career-applications/{application_id}/cv")
def download_career_application_cv(
    application_id: str,
    db: Session = Depends(get_db),
):
    record = (
        db.query(CareerApplication)
        .filter(CareerApplication.id == application_id)
        .first()
    )

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Career application not found.",
        )

    try:
        file_data = (
            supabase.storage
            .from_(BUCKET)
            .download(record.cv_stored_filename)
        )
    except Exception:
        logger.exception(
            "Failed to download CV from Supabase Storage: %s",
            record.cv_stored_filename,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="CV file not found in storage.",
        )

    return Response(
        content=file_data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{record.cv_original_filename}"'
            )
        },
    )


# ---------------------------------------------------------------------------
# Admin user management — lets a logged-in admin create additional admin
# accounts from the dashboard, instead of every admin sharing one set of
# env-var credentials.
# ---------------------------------------------------------------------------


@router.get("/users", response_model=AdminUserListResponse, dependencies=[Depends(require_roles("admin"))])
def list_admin_users(db: Session = Depends(get_db)) -> AdminUserListResponse:
    users = db.query(AdminUser).order_by(AdminUser.created_at.asc()).all()
    return AdminUserListResponse(items=[AdminUserRead.model_validate(u) for u in users])


@router.post(
    "/users",
    response_model=AdminUserCreateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles("admin"))],
)
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

    if payload.branch_id is not None and not db.query(Branch).filter(Branch.id == payload.branch_id).first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="branch_id doesn't match a real branch.")
    if payload.managed_branch_ids:
        found = {b.id for b in db.query(Branch).filter(Branch.id.in_(payload.managed_branch_ids)).all()}
        missing = set(payload.managed_branch_ids) - found
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown branch id(s): {', '.join(missing)}")

    user = AdminUser(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role,
        branch_id=payload.branch_id,
        managed_branch_ids=payload.managed_branch_ids,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    logger.info(
        "Admin user %r created a new %s account: %r", current_admin.username, user.role, user.username
    )

    return AdminUserCreateResponse(data=AdminUserRead.model_validate(user))


@router.delete("/users/{user_id}", response_model=AdminUserRead, dependencies=[Depends(require_roles("admin"))])
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


@router.patch("/users/{user_id}", response_model=AdminUserUpdateResponse, dependencies=[Depends(require_roles("admin"))])
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

    if payload.role is not None:
        user.role = payload.role

    if payload.branch_id is not None:
        if not db.query(Branch).filter(Branch.id == payload.branch_id).first():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="branch_id doesn't match a real branch.")
        user.branch_id = payload.branch_id

    if payload.managed_branch_ids is not None:
        found = {b.id for b in db.query(Branch).filter(Branch.id.in_(payload.managed_branch_ids)).all()}
        missing = set(payload.managed_branch_ids) - found
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown branch id(s): {', '.join(missing)}")
        user.managed_branch_ids = payload.managed_branch_ids

    if payload.is_active is not None and payload.is_active != user.is_active:
        if payload.is_active is False:
            _guard_deactivation(user, current_admin, db)
        user.is_active = payload.is_active

    db.commit()
    db.refresh(user)

    logger.info("Admin user %r updated account %r", current_admin.username, user.username)

    return AdminUserUpdateResponse(data=AdminUserRead.model_validate(user))

# import logging
# import math
# from datetime import datetime, timedelta, timezone
# from pathlib import Path

# from fastapi import APIRouter, Depends, HTTPException, Query, status
# from fastapi.responses import FileResponse
# from sqlalchemy import func
# from sqlalchemy.orm import Session
# from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
# from app.config import get_settings
# from app.database import get_db
# from app.models.admin_user import AdminUser
# from app.models.career_application import CareerApplication, CareerApplicationStatus
# from app.models.contact import ContactMessage
# from app.models.loan_application import LoanApplication, LoanApplicationStatus
# from app.schemas.admin import (
#     DashboardStats,
#     PageMeta,
#     PaginatedCareerApplications,
#     PaginatedContacts,
#     PaginatedLoanApplications,
#     ProductBreakdown,
#     StatusUpdate,
# )
# from app.schemas.admin_user import (
#     AdminUserCreate,
#     AdminUserCreateResponse,
#     AdminUserListResponse,
#     AdminUserRead,
#     AdminUserUpdate,
#     AdminUserUpdateResponse,
# )
# from app.schemas.career_application import CareerApplicationRead
# from app.schemas.contact import ContactRead
# from app.schemas.loan_application import LoanApplicationRead
# from app.services.auth import get_current_admin, hash_password
# from app.services.storage import supabase, BUCKET

# settings = get_settings()
# logger = logging.getLogger("bidii.admin")

# router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(get_current_admin)])


# def _page_meta(page: int, page_size: int, total: int) -> PageMeta:
#     total_pages = max(1, math.ceil(total / page_size))
#     return PageMeta(page=page, page_size=page_size, total=total, total_pages=total_pages)


# @router.get("/stats", response_model=DashboardStats)
# def get_dashboard_stats(db: Session = Depends(get_db)) -> DashboardStats:
#     seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)

#     total_contacts = db.query(func.count(ContactMessage.id)).scalar() or 0
#     total_loans = db.query(func.count(LoanApplication.id)).scalar() or 0
#     total_careers = db.query(func.count(CareerApplication.id)).scalar() or 0

#     loan_status_rows = (
#         db.query(LoanApplication.status, func.count(LoanApplication.id))
#         .group_by(LoanApplication.status)
#         .all()
#     )
#     loan_by_status = {status_.value: count for status_, count in loan_status_rows}
#     for s in LoanApplicationStatus:
#         loan_by_status.setdefault(s.value, 0)

#     career_status_rows = (
#         db.query(CareerApplication.status, func.count(CareerApplication.id))
#         .group_by(CareerApplication.status)
#         .all()
#     )
#     career_by_status = {status_.value: count for status_, count in career_status_rows}
#     for s in CareerApplicationStatus:
#         career_by_status.setdefault(s.value, 0)

#     product_rows = (
#         db.query(
#             LoanApplication.product_slug,
#             LoanApplication.product_name,
#             func.count(LoanApplication.id),
#             func.sum(LoanApplication.amount),
#         )
#         .group_by(LoanApplication.product_slug, LoanApplication.product_name)
#         .all()
#     )
#     by_product = [
#         ProductBreakdown(
#             product_slug=slug,
#             product_name=name,
#             count=count,
#             total_amount_requested=float(total or 0),
#         )
#         for slug, name, count, total in product_rows
#     ]

#     contacts_recent = (
#         db.query(func.count(ContactMessage.id)).filter(ContactMessage.created_at >= seven_days_ago).scalar() or 0
#     )
#     loans_recent = (
#         db.query(func.count(LoanApplication.id)).filter(LoanApplication.created_at >= seven_days_ago).scalar() or 0
#     )
#     careers_recent = (
#         db.query(func.count(CareerApplication.id)).filter(CareerApplication.created_at >= seven_days_ago).scalar() or 0
#     )

#     total_amount = db.query(func.sum(LoanApplication.amount)).scalar() or 0

#     return DashboardStats(
#         total_contacts=total_contacts,
#         total_loan_applications=total_loans,
#         total_career_applications=total_careers,
#         loan_applications_by_status=loan_by_status,
#         career_applications_by_status=career_by_status,
#         loan_applications_by_product=by_product,
#         contacts_last_7_days=contacts_recent,
#         loan_applications_last_7_days=loans_recent,
#         career_applications_last_7_days=careers_recent,
#         total_amount_requested_all_time=float(total_amount),
#     )


# @router.get("/contacts", response_model=PaginatedContacts)
# def list_contacts(
#     page: int = Query(1, ge=1),
#     page_size: int = Query(20, ge=1, le=100),
#     subject: str | None = None,
#     db: Session = Depends(get_db),
# ) -> PaginatedContacts:
#     query = db.query(ContactMessage)
#     if subject:
#         query = query.filter(ContactMessage.subject == subject)
#     total = query.count()
#     items = (
#         query.order_by(ContactMessage.created_at.desc())
#         .offset((page - 1) * page_size)
#         .limit(page_size)
#         .all()
#     )
#     return PaginatedContacts(
#         meta=_page_meta(page, page_size, total),
#         items=[ContactRead.model_validate(i) for i in items],
#     )


# @router.get("/loan-applications", response_model=PaginatedLoanApplications)
# def list_loan_applications(
#     page: int = Query(1, ge=1),
#     page_size: int = Query(20, ge=1, le=100),
#     status_filter: str | None = Query(None, alias="status"),
#     product_slug: str | None = None,
#     db: Session = Depends(get_db),
# ) -> PaginatedLoanApplications:
#     query = db.query(LoanApplication)
#     if status_filter:
#         query = query.filter(LoanApplication.status == status_filter)
#     if product_slug:
#         query = query.filter(LoanApplication.product_slug == product_slug)
#     total = query.count()
#     items = (
#         query.order_by(LoanApplication.created_at.desc())
#         .offset((page - 1) * page_size)
#         .limit(page_size)
#         .all()
#     )
#     return PaginatedLoanApplications(
#         meta=_page_meta(page, page_size, total),
#         items=[LoanApplicationRead.model_validate(i) for i in items],
#     )


# @router.patch("/loan-applications/{application_id}", response_model=LoanApplicationRead)
# def update_loan_application_status(
#     application_id: str, payload: StatusUpdate, db: Session = Depends(get_db)
# ) -> LoanApplicationRead:
#     record = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
#     if record is None:
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan application not found.")

#     valid_values = {s.value for s in LoanApplicationStatus}
#     if payload.status not in valid_values:
#         raise HTTPException(
#             status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
#             detail=f"Status must be one of: {', '.join(sorted(valid_values))}",
#         )

#     record.status = LoanApplicationStatus(payload.status)
#     db.commit()
#     db.refresh(record)
#     return LoanApplicationRead.model_validate(record)


# @router.get("/career-applications", response_model=PaginatedCareerApplications)
# def list_career_applications(
#     page: int = Query(1, ge=1),
#     page_size: int = Query(20, ge=1, le=100),
#     status_filter: str | None = Query(None, alias="status"),
#     job_id: str | None = None,
#     db: Session = Depends(get_db),
# ) -> PaginatedCareerApplications:
#     query = db.query(CareerApplication)
#     if status_filter:
#         query = query.filter(CareerApplication.status == status_filter)
#     if job_id:
#         query = query.filter(CareerApplication.job_id == job_id)
#     total = query.count()
#     items = (
#         query.order_by(CareerApplication.created_at.desc())
#         .offset((page - 1) * page_size)
#         .limit(page_size)
#         .all()
#     )
#     return PaginatedCareerApplications(
#         meta=_page_meta(page, page_size, total),
#         items=[CareerApplicationRead.model_validate(i) for i in items],
#     )


# @router.patch("/career-applications/{application_id}", response_model=CareerApplicationRead)
# def update_career_application_status(
#     application_id: str, payload: StatusUpdate, db: Session = Depends(get_db)
# ) -> CareerApplicationRead:
#     record = db.query(CareerApplication).filter(CareerApplication.id == application_id).first()
#     if record is None:
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Career application not found.")

#     valid_values = {s.value for s in CareerApplicationStatus}
#     if payload.status not in valid_values:
#         raise HTTPException(
#             status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
#             detail=f"Status must be one of: {', '.join(sorted(valid_values))}",
#         )

#     record.status = CareerApplicationStatus(payload.status)
#     db.commit()
#     db.refresh(record)
#     return CareerApplicationRead.model_validate(record)


# # @router.get("/career-applications/{application_id}/cv")
# # def download_career_application_cv(application_id: str, db: Session = Depends(get_db)) -> FileResponse:
# #     record = db.query(CareerApplication).filter(CareerApplication.id == application_id).first()
# #     if record is None:
# #         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Career application not found.")

# #     file_path: Path = settings.upload_dir / "careers" / record.cv_stored_filename
# #     if not file_path.exists():
# #         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CV file not found on disk.")

# #     return FileResponse(
# #         path=file_path,
# #         filename=record.cv_original_filename,
# #         media_type="application/pdf",
# #     )
# @router.get("/career-applications/{application_id}/cv")
# def download_career_application_cv(
#     application_id: str,
#     db: Session = Depends(get_db),
# ):
#     record = (
#         db.query(CareerApplication)
#         .filter(CareerApplication.id == application_id)
#         .first()
#     )

#     if record is None:
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail="Career application not found.",
#         )

#     try:
#         file_data = (
#             supabase.storage
#             .from_(BUCKET)
#             .download(record.cv_stored_filename)
#         )
#     except Exception:
#         logger.exception(
#             "Failed to download CV from Supabase Storage: %s",
#             record.cv_stored_filename,
#         )
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail="CV file not found in storage.",
#         )

#     return Response(
#         content=file_data,
#         media_type="application/pdf",
#         headers={
#             "Content-Disposition": (
#                 f'attachment; filename="{record.cv_original_filename}"'
#             )
#         },
#     )


# # ---------------------------------------------------------------------------
# # Admin user management - lets a logged-in admin create additional admin
# # accounts from the dashboard, instead of every admin sharing one set of
# # env-var credentials.
# # ---------------------------------------------------------------------------


# @router.get("/users", response_model=AdminUserListResponse)
# def list_admin_users(db: Session = Depends(get_db)) -> AdminUserListResponse:
#     users = db.query(AdminUser).order_by(AdminUser.created_at.asc()).all()
#     return AdminUserListResponse(items=[AdminUserRead.model_validate(u) for u in users])


# @router.post("/users", response_model=AdminUserCreateResponse, status_code=status.HTTP_201_CREATED)
# def create_admin_user(
#     payload: AdminUserCreate,
#     db: Session = Depends(get_db),
#     current_admin: AdminUser = Depends(get_current_admin),
# ) -> AdminUserCreateResponse:
#     existing = db.query(AdminUser).filter(AdminUser.username == payload.username).first()
#     if existing is not None:
#         raise HTTPException(
#             status_code=status.HTTP_409_CONFLICT,
#             detail=f'An admin with username "{payload.username}" already exists.',
#         )

#     user = AdminUser(username=payload.username, password_hash=hash_password(payload.password))
#     db.add(user)
#     db.commit()
#     db.refresh(user)

#     logger.info("Admin user %r created a new admin account: %r", current_admin.username, user.username)

#     return AdminUserCreateResponse(data=AdminUserRead.model_validate(user))


# @router.delete("/users/{user_id}", response_model=AdminUserRead)
# def deactivate_admin_user(
#     user_id: str,
#     db: Session = Depends(get_db),
#     current_admin: AdminUser = Depends(get_current_admin),
# ) -> AdminUserRead:
#     """
#     Soft-deletes (deactivates) an admin account rather than hard-deleting it,
#     preserving an audit trail of who created what. Guards against locking
#     everyone out: you can't deactivate yourself, and you can't deactivate
#     the last remaining active admin.
#     """
#     user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
#     if user is None:
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin user not found.")

#     _guard_deactivation(user, current_admin, db)

#     user.is_active = False
#     db.commit()
#     db.refresh(user)
#     return AdminUserRead.model_validate(user)


# def _guard_deactivation(user: AdminUser, current_admin: AdminUser, db: Session) -> None:
#     """
#     Shared by DELETE /users/{id} and PATCH /users/{id} (when it sets
#     is_active=False) so both paths enforce the same lockout prevention:
#     you can't deactivate yourself, and the last remaining active admin
#     can't be deactivated by anyone. Compares by ID, not username - usernames
#     can change, IDs don't.
#     """
#     if user.id == current_admin.id:
#         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You can't deactivate your own account.")

#     active_count = db.query(func.count(AdminUser.id)).filter(AdminUser.is_active.is_(True)).scalar() or 0
#     if user.is_active and active_count <= 1:
#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST,
#             detail="Can't deactivate the last remaining active admin account.",
#         )


# @router.patch("/users/{user_id}", response_model=AdminUserUpdateResponse)
# def update_admin_user(
#     user_id: str,
#     payload: AdminUserUpdate,
#     db: Session = Depends(get_db),
#     current_admin: AdminUser = Depends(get_current_admin),
# ) -> AdminUserUpdateResponse:
#     """
#     Updates one or more fields on an admin account: username, password,
#     and/or is_active (so this endpoint also covers reactivating a
#     previously-deactivated admin, not just editing active ones).
#     Only fields actually present in the request body are changed.
#     """
#     user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
#     if user is None:
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin user not found.")

#     if payload.username is not None and payload.username != user.username:
#         existing = db.query(AdminUser).filter(AdminUser.username == payload.username).first()
#         if existing is not None:
#             raise HTTPException(
#                 status_code=status.HTTP_409_CONFLICT,
#                 detail=f'An admin with username "{payload.username}" already exists.',
#             )
#         user.username = payload.username
#         # No session-invalidation workaround needed here - the JWT subject
#         # is current_admin.id, which doesn't change when a username does.

#     if payload.password is not None:
#         user.password_hash = hash_password(payload.password)

#     if payload.is_active is not None and payload.is_active != user.is_active:
#         if payload.is_active is False:
#             _guard_deactivation(user, current_admin, db)
#         user.is_active = payload.is_active

#     db.commit()
#     db.refresh(user)

#     logger.info("Admin user %r updated account %r", current_admin.username, user.username)

#     return AdminUserUpdateResponse(data=AdminUserRead.model_validate(user))
