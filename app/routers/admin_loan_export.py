"""
Exports loan applications grouped by branch, as either a downloadable
.xlsx workbook or a live Google Sheets document - see
app/services/loan_export.py for the actual file/spreadsheet building.

Deliberately a separate router file rather than more additions to the
already-large admin.py: nothing in admin.py is touched by this feature.
The one thing intentionally shared with admin.py is _scoped_loan_applications
below, which mirrors list_loan_applications' role-based scoping exactly -
same role tests, same behavior - so a role that can only see some
applications there can't see more of them through this export.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.loan_application import LoanApplication
from app.routers.admin import AGENT_ROLES
from app.services.auth import get_current_admin
from app.services.loan_application_presenter import to_loan_application_read_list
from app.services.loan_export import (
    GoogleSheetsNotConfigured,
    available_export_columns,
    build_loan_applications_workbook,
    export_to_google_sheets,
)
from app.services.role_permissions import require_menu_access

logger = logging.getLogger("bidii.admin_loan_export")

router = APIRouter(
    prefix="/api/admin/loan-applications/export",
    tags=["admin-loan-export"],
    dependencies=[Depends(get_current_admin), Depends(require_menu_access("/admin/loan-applications"))],
)


def _scoped_loan_applications(
    db: Session,
    current_admin: AdminUser,
    status_filter: str | None,
    product_slug: str | None,
) -> list[LoanApplication]:
    """Same role-based visibility as list_loan_applications in admin.py -
    see that function's docstring for what each role can see. Kept as its
    own small helper (rather than importing that endpoint function itself)
    since that one also handles pagination/response-shaping this export
    doesn't need."""
    query = db.query(LoanApplication)
    if current_admin.role == "branch_office_admin":
        query = query.filter(LoanApplication.assigned_branch_id.in_(current_admin.managed_branch_ids or []))
    elif current_admin.role in AGENT_ROLES:
        query = query.filter(LoanApplication.assigned_loan_officer_id == current_admin.id)
    elif current_admin.role != "admin":
        query = query.filter(False)  # noqa: E712 - unrecognised role sees nothing, not everything

    if status_filter:
        query = query.filter(LoanApplication.status == status_filter)
    if product_slug:
        query = query.filter(LoanApplication.product_slug == product_slug)
    return query.order_by(LoanApplication.created_at.desc()).all()


@router.get("/columns")
def list_loan_export_columns() -> list[dict[str, str]]:
    """The columns the .xlsx export can include, in sheet order - lets the
    frontend render a picker without duplicating this list by hand."""
    return available_export_columns()


@router.get("/xlsx")
def export_loan_applications_xlsx(
    status_filter: str | None = Query(None, alias="status"),
    product_slug: str | None = None,
    columns: list[str] | None = Query(
        None,
        description=(
            "Which columns to include, by key (see GET /columns) - e.g. "
            "?columns=full_name&columns=phone. Omit for every column."
        ),
    ),
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> StreamingResponse:
    """
    Downloads every loan application this admin can see as one .xlsx
    workbook - an "All Applications" sheet, plus one sheet per branch.
    Needs no external setup (unlike the Google Sheets export below) since
    openpyxl just builds the file in memory.
    """
    records = _scoped_loan_applications(db, current_admin, status_filter, product_slug)
    applications = to_loan_application_read_list(db, records)
    workbook_bytes = build_loan_applications_workbook(applications, columns)

    filename = f"bidii-loan-applications-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.xlsx"
    logger.info(
        "Admin %r exported %d loan application(s) to .xlsx",
        current_admin.username,
        len(applications),
    )
    return StreamingResponse(
        iter([workbook_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/google-sheets")
def export_loan_applications_to_google_sheets(
    share_with_email: str | None = Query(
        None,
        description=(
            "Google account to share the created spreadsheet with. Defaults to this "
            "admin's own email on file; required if that isn't set."
        ),
    ),
    status_filter: str | None = Query(None, alias="status"),
    product_slug: str | None = None,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> dict:
    """
    Same data as the .xlsx export above, but created live as a Google
    Sheets document and shared with share_with_email (or the requesting
    admin's own email if they have one on file) - returns
    {"spreadsheet_url": ...} for the frontend to open directly. Requires
    GOOGLE_SERVICE_ACCOUNT_JSON to be configured on the backend; see
    app/services/loan_export.py for what that needs to be.
    """
    target_email = share_with_email or current_admin.email
    if not target_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "No email to share the spreadsheet with - this admin account has none on "
                "file. Pass share_with_email explicitly."
            ),
        )

    records = _scoped_loan_applications(db, current_admin, status_filter, product_slug)
    applications = to_loan_application_read_list(db, records)
    title = f"Bidii Credit - Loan Applications ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})"

    try:
        spreadsheet_url = export_to_google_sheets(applications, title, target_email)
    except GoogleSheetsNotConfigured as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception:
        logger.exception("Google Sheets export failed for admin %r", current_admin.username)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google Sheets export failed.",
        )

    logger.info(
        "Admin %r exported %d loan application(s) to Google Sheets, shared with %r",
        current_admin.username,
        len(applications),
        target_email,
    )
    return {"spreadsheet_url": spreadsheet_url, "shared_with": target_email, "count": len(applications)}