"""
Staff Performance - goal cascading (Phase 6d).  Prefix /api/admin/performance/cascade

  GET  /kpis                    additive KPIs that can be cascaded
  GET  /tree?kpi=&month=        company -> region -> branch -> people with the gap at every level (scoped: managers see their own part)
  PUT  /targets                 HR/admin: set a company / region / unit allocation (effective-dated, with a reason)
  GET  /proposal?level=&entity_id=&kpi=&month=   HR/admin: how the level's target would be split among those below
  POST /apply                   HR/admin: write that split (ordinary effective-dated targets, each with a reason); needs confirm=true
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.routers.admin_performance import admin_or_hr, my_access
from app.services import performance_cascade as pc
from app.services.auth import get_current_admin
from app.services.performance_config import log_audit
from app.services.performance_scope import get_performance_scope
from app.services.performance_time import today_nairobi
from app.services.performance_permissions import needs

router = APIRouter(prefix="/api/admin/performance/cascade", tags=["admin-performance-cascade"])


class SetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: Literal["company", "region", "unit"]
    entity_id: str = Field(default="company", max_length=36)
    kpi_key: str = Field(max_length=60)
    month: date
    value: float
    reason: str = Field(max_length=300)


class ApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: Literal["company", "region", "unit"]
    entity_id: str = Field(default="company", max_length=36)
    kpi_key: str = Field(max_length=60)
    month: date
    confirm: bool


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/kpis", dependencies=[my_access])
def kpis(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"items": pc.kpi_options(db)}


@router.get("/tree", dependencies=[my_access])
def get_tree(kpi: str = Query(max_length=60), month: date | None = None, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    if scope.visible_ids is not None and not (scope.managed_unit_ids or scope.managed_region_ids):
        raise HTTPException(status_code=404, detail="That wasn't found.")
    try:
        return pc.tree(db, scope, kpi, month or today_nairobi())
    except pc.CascadeError as exc:
        raise _bad(exc) from exc


@router.put("/targets", dependencies=[needs("cascade.manage")])
def set_target(body: SetBody, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    if scope.read_only:
        raise HTTPException(status_code=403, detail="Read-only access.")
    try:
        row = pc.set_cascade(db, body.level, body.entity_id, body.kpi_key, body.month, body.value, body.reason, admin.username)
    except pc.CascadeError as exc:
        raise _bad(exc) from exc
    if row is not None:
        log_audit(db, admin.username, "cascade.set", "cascade_target", row.id, {"level": body.level, "kpi": body.kpi_key})
    return {"changed": row is not None}


@router.get("/proposal", dependencies=[needs("cascade.manage")])
def get_proposal(level: Literal["company", "region", "unit"], kpi: str = Query(max_length=60), month: date | None = None, entity_id: str = Query(default="company", max_length=36), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return pc.proposal(db, level, entity_id, kpi, month or today_nairobi())
    except pc.CascadeError as exc:
        raise _bad(exc) from exc


@router.post("/apply", dependencies=[needs("cascade.manage")])
def apply(body: ApplyBody, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    if scope.read_only:
        raise HTTPException(status_code=403, detail="Read-only access.")
    if not body.confirm:
        raise HTTPException(status_code=422, detail="Please confirm that you want to write these targets.")
    try:
        res = pc.apply(db, body.level, body.entity_id, body.kpi_key, body.month, admin.username)
    except pc.CascadeError as exc:
        db.rollback()
        raise _bad(exc) from exc
    log_audit(db, admin.username, "cascade.apply", "cascade", body.entity_id, {"level": body.level, "kpi": body.kpi_key, **res})
    return res
