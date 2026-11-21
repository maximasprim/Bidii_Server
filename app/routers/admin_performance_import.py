"""
Staff Performance - the Excel import wizard.

Prefix: /api/admin/performance/import     admin or HR only (restricted PIP/warning notes are read and imported
only when the caller is HR; an admin sees a count, never the notes).
  POST   /preview          upload the workbook (.xlsx, magic-byte checked, size capped while streaming) -> preview
  GET    /                 past runs
  GET    /template         a clean workbook in the layout this wizard reads
  GET    /{id}             the preview again (sheet->unit mapping, name matching, issues, reconciliation)
  PUT    /{id}/decisions   save options and decisions (only while previewing)
  POST   /{id}/dry-run     what a commit WOULD do (rolled back; same code as commit)
  POST   /{id}/commit      do it, all or nothing
  POST   /{id}/undo        reverse a committed run
  DELETE /{id}             discard a preview (and the parsed data held for it)
Nothing in the audit log carries a name, a figure or a note.
"""

from __future__ import annotations

import io
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import PerformanceImport, PerformanceUnit
from app.routers.admin_performance import admin_or_hr
from app.schemas.performance_import import CommitRequest, DecisionsUpdate
from app.services import performance_import as imp_svc
from app.services import performance_upload as upload
from app.services.auth import get_current_admin
from app.services.performance_config import log_audit
from app.services.performance_import_parse import MAX_XLSX_BYTES, WorkbookError
from app.services.performance_ratelimit import rate_limited

router = APIRouter(prefix="/api/admin/performance/import", tags=["admin-performance-import"])
upload_rate_limit = Depends(rate_limited("import", limit=10, window_seconds=3600))
_NOT_FOUND = HTTPException(status_code=404, detail="That import wasn't found.")
_OPTION_KEYS = set(imp_svc.DEFAULT_OPTIONS)


def _load(db: Session, import_id: str) -> PerformanceImport:
    imp = db.get(PerformanceImport, import_id)
    if imp is None or imp.status == "discarded":
        raise _NOT_FOUND
    return imp


def _is_hr(admin: AdminUser) -> bool:
    return admin.role in ("hr", "admin")


@router.post("/preview", dependencies=[admin_or_hr, upload_rate_limit])
async def preview(request: Request, filename: str = "workbook.xlsx", year: int | None = Query(default=None, ge=2000, le=2100), db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Please upload an .xlsx workbook (in Excel: Save As > Excel Workbook).")
    data = await upload.read_limited(request.stream(), max_bytes=MAX_XLSX_BYTES, declared_length=request.headers.get("content-length"))
    try:
        imp = imp_svc.create_preview(db, data, filename, current_admin.username, year)
    except WorkbookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(db, current_admin.username, "import_previewed", "performance_import", imp.id, None)
    return imp_svc.preview_payload(db, imp, _is_hr(current_admin))


@router.get("/template", dependencies=[admin_or_hr])
def template(db: Session = Depends(get_db)) -> StreamingResponse:
    units = db.query(PerformanceUnit).filter(PerformanceUnit.is_active.is_(True), PerformanceUnit.kind != "head_office").order_by(PerformanceUnit.name.asc()).all()
    data = imp_svc.build_template([u.name for u in units])
    return StreamingResponse(
        io.BytesIO(data), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="staff-performance-import-template.xlsx"'},
    )


@router.get("", dependencies=[admin_or_hr])
def list_imports(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.query(PerformanceImport).filter(PerformanceImport.status != "discarded").order_by(PerformanceImport.created_at.desc()).limit(50).all()
    return {
        "items": [
            {"id": r.id, "original_name": r.original_name, "status": r.status, "created_by": r.created_by_username, "created_at": r.created_at,
             "committed_at": r.committed_at, "undone_at": r.undone_at, "counts": (r.summary or {}).get("counts")}
            for r in rows
        ]
    }


@router.get("/{import_id}", dependencies=[admin_or_hr])
def get_import(import_id: str, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    imp = _load(db, import_id)
    if not imp.parsed:
        raise _NOT_FOUND
    return imp_svc.preview_payload(db, imp, _is_hr(current_admin))


@router.put("/{import_id}/decisions", dependencies=[admin_or_hr])
def save_decisions(import_id: str, payload: DecisionsUpdate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    imp = _load(db, import_id)
    if imp.status != "previewed":
        raise HTTPException(status_code=409, detail="This import is no longer open for changes.")
    options = dict(imp.options or imp_svc.DEFAULT_OPTIONS)
    if payload.options is not None:
        for k, v in payload.options.model_dump(exclude_none=True).items():
            if k in _OPTION_KEYS:
                options[k] = v
    decisions = {"sheets": dict((imp.decisions or {}).get("sheets") or {}), "staff": dict((imp.decisions or {}).get("staff") or {})}
    if payload.sheets:
        decisions["sheets"].update({k: v.model_dump(exclude_none=True) for k, v in payload.sheets.items()})
    if payload.staff:
        decisions["staff"].update({k: v.model_dump(exclude_none=True) for k, v in payload.staff.items()})
    imp.options, imp.decisions = options, decisions
    db.commit()
    db.refresh(imp)
    return imp_svc.preview_payload(db, imp, _is_hr(current_admin))


def _run(fn, imp: PerformanceImport, db: Session, admin: AdminUser) -> dict[str, Any]:
    try:
        return fn(db, imp, admin.username, _is_hr(admin))
    except imp_svc.ImportProblem as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{import_id}/dry-run", dependencies=[admin_or_hr])
def dry_run(import_id: str, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    imp = _load(db, import_id)
    if imp.status != "previewed":
        raise HTTPException(status_code=409, detail="This import is no longer open for changes.")
    return _run(imp_svc.dry_run, imp, db, current_admin)


@router.post("/{import_id}/commit", dependencies=[admin_or_hr])
def commit_import(import_id: str, payload: CommitRequest, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    imp = _load(db, import_id)
    return _run(imp_svc.commit, imp, db, current_admin)


@router.post("/{import_id}/undo", dependencies=[admin_or_hr])
def undo_import(import_id: str, payload: CommitRequest, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    imp = _load(db, import_id)
    try:
        return imp_svc.undo(db, imp, current_admin.username)
    except imp_svc.ImportProblem as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{import_id}", dependencies=[admin_or_hr])
def discard_import(import_id: str, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)) -> dict[str, str]:
    imp = _load(db, import_id)
    if imp.status != "previewed":
        raise HTTPException(status_code=409, detail="Only a preview that hasn't been committed can be discarded.")
    imp.status, imp.parsed, imp.decisions = "discarded", {}, {}
    db.commit()
    log_audit(db, current_admin.username, "import_discarded", "performance_import", imp.id, None)
    return {"message": "Discarded."}
