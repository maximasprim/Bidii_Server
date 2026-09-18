"""
Builds the loan-applications export used by
app/routers/admin_loan_export.py - one sheet per branch (plus an
"All Applications" overview sheet first), either as a downloadable .xlsx
workbook or as a live Google Sheets document.

Both formats share the same column layout and the same grouping-by-branch
logic (_group_by_branch / COLUMNS / _row_for below), so the two exports
never drift apart into showing different data.
"""

import io
import json
import re
from pathlib import Path
from typing import Callable

from app.config import get_settings
from app.schemas.loan_application import LoanApplicationRead

UNASSIGNED_SHEET_NAME = "Unassigned"

# (stable key for the API, header label shown in the sheet, function to
# pull that column's value out of one application). The key is what the
# frontend's column picker sends back and how /export/columns identifies
# each column - keep it unchanged once shipped, since a saved/bookmarked
# export URL could reference it.
COLUMNS: list[tuple[str, str, Callable[[LoanApplicationRead], object]]] = [
    ("application_id", "Application ID", lambda a: a.id),
    ("full_name", "Full Name", lambda a: a.full_name),
    ("id_number", "ID Number", lambda a: a.id_number),
    ("phone", "Phone", lambda a: a.phone),
    ("email", "Email", lambda a: a.email),
    ("county", "County", lambda a: a.county or ""),
    ("location", "Location", lambda a: a.location or ""),
    ("branch", "Branch", lambda a: a.assigned_branch_name or ""),
    ("product", "Product", lambda a: a.product_name),
    ("amount", "Amount", lambda a: a.amount),
    ("submitted", "Submitted", lambda a: a.created_at.strftime("%Y-%m-%d %H:%M") if a.created_at else ""),
    ("monthly_income", "Monthly Income", lambda a: a.monthly_income),
    ("tier", "Tier / Plan", lambda a: a.tier_label),
    ("term", "Term", lambda a: f"{a.term_value} {a.term_unit}"),
    ("status", "Status", lambda a: a.status.value if hasattr(a.status, "value") else a.status),
    ("estimated_installment", "Estimated Installment", lambda a: a.estimated_installment),
    ("branch_assignment_method", "Branch Assignment Method", lambda a: a.branch_assignment_method or ""),
    ("assigned_loan_officer", "Assigned Loan Officer", lambda a: a.assigned_loan_officer_name or ""),
]


def available_export_columns() -> list[dict[str, str]]:
    """What the frontend's column picker offers, in the same fixed order
    the sheet itself uses - see admin_loan_export.py's GET /columns."""
    return [{"key": key, "label": label} for key, label, _getter in COLUMNS]


def _resolve_columns(
    column_keys: list[str] | None,
) -> list[tuple[str, str, Callable[[LoanApplicationRead], object]]]:
    """None/empty means "everything", same as before this option existed -
    so every existing caller (and the Google Sheets export, which doesn't
    pass this at all) keeps working unchanged. Unknown keys are ignored
    rather than erroring, and the sheet's own column order is always used
    regardless of what order the caller listed keys in."""
    if not column_keys:
        return COLUMNS
    wanted = set(column_keys)
    selected = [c for c in COLUMNS if c[0] in wanted]
    return selected or COLUMNS


def _row_for(
    application: LoanApplicationRead,
    columns: list[tuple[str, str, Callable[[LoanApplicationRead], object]]] = COLUMNS,
) -> list:
    return [getter(application) for _key, _label, getter in columns]


def _group_by_branch(
    applications: list[LoanApplicationRead],
) -> dict[str, list[LoanApplicationRead]]:
    """Groups applications by branch name, in first-seen order, with
    unassigned applications collected under UNASSIGNED_SHEET_NAME rather
    than dropped - every application in the export should end up visible
    on some sheet."""
    groups: dict[str, list[LoanApplicationRead]] = {}
    for application in applications:
        key = application.assigned_branch_name or UNASSIGNED_SHEET_NAME
        groups.setdefault(key, []).append(application)
    return groups


def _safe_sheet_name(name: str, taken: set[str]) -> str:
    """
    Excel/Sheets sheet names can't contain : \\ / ? * [ ] and are capped
    at 31 characters (Excel's limit - the tighter of the two formats').
    Branch names in this app are free-text admin input, so this can't
    just assume they're already safe.
    """
    cleaned = re.sub(r"[:\\/?*\[\]]", "-", name).strip() or "Branch"
    cleaned = cleaned[:31]
    candidate = cleaned
    suffix = 2
    while candidate in taken:
        trimmed = cleaned[: 31 - len(f" ({suffix})")]
        candidate = f"{trimmed} ({suffix})"
        suffix += 1
    taken.add(candidate)
    return candidate


def build_loan_applications_workbook(
    applications: list[LoanApplicationRead],
    column_keys: list[str] | None = None,
) -> bytes:
    """Returns the .xlsx file's bytes - one sheet per branch, plus an
    "All Applications" sheet first. Import of openpyxl is local to this
    function so the rest of the app still runs even on an install that
    hasn't picked up the new dependency yet (see requirements.txt) - only
    this specific export would fail, with a clear error, not the backend
    as a whole.

    column_keys optionally narrows which columns are included (see the
    frontend's column picker / GET /columns) - omit it, or pass None/[],
    to get every column, same as before this option existed.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    columns = _resolve_columns(column_keys)
    headers = [label for _key, label, _getter in columns]
    groups = _group_by_branch(applications)

    workbook = Workbook()
    overview = workbook.active
    overview.title = "All Applications"

    def write_sheet(sheet, rows: list[LoanApplicationRead]) -> None:
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        sheet.freeze_panes = "A2"
        for application in rows:
            sheet.append(_row_for(application, columns))
        for i, header in enumerate(headers, start=1):
            width = max(len(header), 12)
            sheet.column_dimensions[get_column_letter(i)].width = min(width + 4, 40)

    write_sheet(overview, applications)

    taken_names = {"All Applications"}
    for branch_name, rows in groups.items():
        sheet = workbook.create_sheet(_safe_sheet_name(branch_name, taken_names))
        write_sheet(sheet, rows)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class GoogleSheetsNotConfigured(Exception):
    """No service account configured - see Settings.google_service_account_json."""


def _load_service_account_info() -> dict:
    raw = get_settings().google_service_account_json
    if not raw:
        raise GoogleSheetsNotConfigured(
            "Google Sheets export isn't configured on this server yet. Set "
            "GOOGLE_SERVICE_ACCOUNT_JSON (a service account key, either as a file "
            "path or pasted JSON) in the backend's environment."
        )
    candidate_path = Path(raw)
    if candidate_path.exists():
        return json.loads(candidate_path.read_text())
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GoogleSheetsNotConfigured(
            "GOOGLE_SERVICE_ACCOUNT_JSON is set but isn't a valid file path or valid "
            "JSON - double check how it was pasted into the environment."
        ) from exc


def _google_clients():
    """Local imports for the same reason as build_loan_applications_workbook
    above - this dependency is only needed if this export is actually used."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = _load_service_account_info()
    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
        ],
    )
    sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    return sheets, drive


def export_to_google_sheets(
    applications: list[LoanApplicationRead],
    title: str,
    share_with_email: str,
) -> str:
    """
    Creates a new Google Sheets spreadsheet - one tab per branch, plus an
    "All Applications" tab first - and shares it with share_with_email as
    an editor, then returns the spreadsheet's URL.

    A spreadsheet a service account creates lives in that service
    account's own (invisible-to-humans) Drive until explicitly shared,
    which is why sharing isn't optional here the way it might be for a
    personal Google account's own Sheets API calls.
    """
    sheets, drive = _google_clients()
    headers = [label for _key, label, _getter in COLUMNS]
    groups = _group_by_branch(applications)

    taken_names = {"All Applications"}
    branch_sheet_names = [_safe_sheet_name(name, taken_names) for name in groups.keys()]
    all_sheet_titles = ["All Applications", *branch_sheet_names]

    spreadsheet = (
        sheets.spreadsheets()
        .create(
            body={
                "properties": {"title": title},
                "sheets": [{"properties": {"title": sheet_title}} for sheet_title in all_sheet_titles],
            },
            fields="spreadsheetId,spreadsheetUrl",
        )
        .execute()
    )
    spreadsheet_id = spreadsheet["spreadsheetId"]

    value_ranges = [
        {"range": f"'All Applications'!A1", "values": [headers] + [_row_for(a) for a in applications]}
    ]
    for sheet_title, (_branch_name, rows) in zip(branch_sheet_names, groups.items()):
        value_ranges.append({"range": f"'{sheet_title}'!A1", "values": [headers] + [_row_for(a) for a in rows]})

    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": value_ranges},
    ).execute()

    # Bold header rows, one request per sheet, in the same batchUpdate call
    # that would also be the natural place to add freeze-panes/column
    # widths if this export grows more polish later.
    sheet_ids = {
        s["properties"]["title"]: s["properties"]["sheetId"]
        for s in sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()["sheets"]
    }
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold",
                    }
                }
                for sheet_id in sheet_ids.values()
            ]
            + [
                {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}}
                for sheet_id in sheet_ids.values()
            ]
        },
    ).execute()

    drive.permissions().create(
        fileId=spreadsheet_id,
        body={"type": "user", "role": "writer", "emailAddress": share_with_email},
        fields="id",
        sendNotificationEmail=True,
    ).execute()

    return spreadsheet["spreadsheetUrl"]