"""Version control for workbook imports. Admin or HR only. Declared BEFORE the import router so /versions isn't read as an import id."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import PerformanceImport
from app.routers.admin_performance import admin_or_hr
from app.services import performance_import as imp_svc
from app.services import performance_import_versions as ver
from app.services.auth import get_current_admin
from app.services.performance_scope import get_performance_scope

router = APIRouter(prefix="/api/admin/performance/import", tags=["admin-performance-import-versions"])


class Unpost(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=5, max_length=300)
    cascade: bool = False
    confirm: bool


class Restore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=5, max_length=300)
    confirm: bool


class Label(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=120)


def _load(db: Session, import_id: str) -> PerformanceImport:
    imp = db.get(PerformanceImport, import_id)
    if imp is None or imp.status in ("previewed", "discarded"):
        raise HTTPException(status_code=404, detail="That version wasn't found.")
    return imp


def _need_confirm(ok: bool) -> None:
    if not ok:
        raise HTTPException(status_code=422, detail="Confirm to continue.")


def _problem(fn):
    try:
        return fn()
    except imp_svc.ImportProblem as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/versions", dependencies=[admin_or_hr])
def versions(series: str | None = Query(default=None, max_length=20), db: Session = Depends(get_db)) -> dict[str, Any]:
    return ver.listing(db, series)


@router.get("/versions/compare", dependencies=[admin_or_hr])
def compare(a: str = Query(max_length=36), b: str = Query(max_length=36), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _problem(lambda: ver.compare(db, _load(db, a), _load(db, b)))


@router.put("/{import_id}/label", dependencies=[admin_or_hr])
def label(import_id: str, body: Label, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, str]:
    _problem(lambda: ver.set_label(db, _load(db, import_id), body.label, admin.username))
    return {"message": "Saved."}


@router.get("/{import_id}/restore-plan", dependencies=[admin_or_hr])
def restore_plan(import_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _problem(lambda: ver.restore_plan(db, _load(db, import_id)))


@router.post("/{import_id}/unpost", dependencies=[admin_or_hr])
def unpost(import_id: str, body: Unpost, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    _need_confirm(body.confirm)
    return _problem(lambda: ver.unpost(db, _load(db, import_id), admin.username, body.reason, cascade=body.cascade))


@router.post("/{import_id}/restore", dependencies=[admin_or_hr])
def restore(import_id: str, body: Restore, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    _need_confirm(body.confirm)
    is_hr = get_performance_scope(admin, db).is_hr
    return _problem(lambda: ver.restore(db, _load(db, import_id), admin.username, is_hr, body.reason))
