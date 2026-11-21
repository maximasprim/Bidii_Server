"""
Staff Performance - governance (Phase 4b-1).  Prefix /api/admin/performance/governance

  GET  /notice                      the staff data-use notice and whether I've acknowledged it
  POST /notice/acknowledge          acknowledge the current version
  GET  /notice/outstanding          HR/admin: staff with a login who haven't
  GET  /locks                       HR/admin: month locks and reopen history
  POST /locks                       HR/admin: lock finished month(s)  {period}
  POST /locks/reopen                HR/admin: reopen with a reason     {period, reason}
  GET  /retention/candidates        admin/HR: leavers past the retention period (nothing is removed automatically)
  POST /retention/{profile_id}/anonymise   admin only; body {confirm: "ANONYMISE"}
  GET  /access-log/{profile_id}     who viewed this person's report / appraisals (HR/admin, or the person themselves)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import StaffProfile
from app.routers.admin_performance import admin_only, admin_or_hr, my_access
from app.services import performance_governance as gov
from app.services.auth import get_current_admin
from app.services.performance_config import log_audit
from app.services.performance_scope import get_performance_scope
from app.services.performance_time import today_nairobi

router = APIRouter(prefix="/api/admin/performance/governance", tags=["admin-performance-governance"])


class PeriodBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period: str = Field(min_length=4, max_length=30)


class ReopenBody(PeriodBody):
    reason: str = Field(min_length=1, max_length=300)


class AnonymiseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str = Field(max_length=20)


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/notice", dependencies=[my_access])
def notice(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    return gov.notice_state(db, admin.id)


@router.post("/notice/acknowledge", dependencies=[my_access])
def acknowledge(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    gov.acknowledge_notice(db, admin.id)
    log_audit(db, admin.username, "notice.acknowledged", "notice", gov.NOTICE_VERSION)
    return gov.notice_state(db, admin.id)


@router.get("/notice/outstanding", dependencies=[admin_or_hr])
def outstanding(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"version": gov.NOTICE_VERSION, "items": gov.notice_outstanding(db)}


@router.get("/locks", dependencies=[admin_or_hr])
def locks(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"items": gov.lock_list(db)}


@router.post("/locks", dependencies=[admin_or_hr])
def lock(body: PeriodBody, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        done = gov.lock(db, body.period, admin.username, today_nairobi())
    except gov.GovernanceError as exc:
        raise _bad(exc) from exc
    log_audit(db, admin.username, "period.lock", "period", body.period, {"months": done})
    return {"locked": done}


@router.post("/locks/reopen", dependencies=[admin_or_hr])
def reopen(body: ReopenBody, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        done = gov.reopen(db, body.period, admin.username, body.reason, today_nairobi())
    except gov.GovernanceError as exc:
        raise _bad(exc) from exc
    log_audit(db, admin.username, "period.reopen", "period", body.period, {"months": done})
    return {"reopened": done}


@router.get("/retention/candidates", dependencies=[admin_or_hr])
def candidates(db: Session = Depends(get_db)) -> dict[str, Any]:
    return gov.retention_candidates(db, today_nairobi())


@router.post("/retention/{profile_id}/anonymise", dependencies=[admin_only])
def anonymise(profile_id: str, body: AnonymiseBody, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    profile = db.get(StaffProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="That wasn't found.")
    try:
        counts = gov.anonymise(db, profile, body.confirm, today_nairobi())
    except gov.GovernanceError as exc:
        raise _bad(exc) from exc
    log_audit(db, admin.username, "retention.anonymise", "staff_profile", profile_id, counts)
    return {"anonymised": True, **counts}


@router.get("/access-log/{profile_id}", dependencies=[my_access])
def access_log(profile_id: str, limit: int = Query(default=200, ge=1, le=500), admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    mine = profile_id == scope.own_profile_id
    if not (mine or scope.is_admin or scope.is_hr) or db.get(StaffProfile, profile_id) is None:
        raise HTTPException(status_code=404, detail="That wasn't found.")
    return {"items": gov.access_log(db, profile_id, limit)}
