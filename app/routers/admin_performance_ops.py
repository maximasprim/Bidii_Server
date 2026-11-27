"""
Staff Performance - operations (Phase 4b-2).  Prefix /api/admin/performance/ops   (+ /api/internal/performance/daily-digest)

  GET   /reminders/me               what is due for me (counts only)
  GET   /actions/open               the tracker: open coaching / PIP / warning items I may see, with review dates
  PATCH /actions/{id}               update dates / note, or close with an outcome
  GET   /anomalies  /data-quality   HR/admin
  GET   /calibration                HR/admin/executive: average score by line manager
  GET   /leaderboard/units          branches ranked by achievement (small units flagged)
  GET   /timeline/{profile_id}      hires, moves, actions, appraisals (restricted actions only for those entitled)
  GET   /training-themes            common development needs by unit (small units hidden)
  POST  /api/internal/performance/daily-digest   cron only: X-Cron-Secret header; 404 if no secret is configured

Every route has a guard and goes through get_performance_scope; out-of-scope ids answer 404.
"""

from __future__ import annotations

import hmac
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import PerformanceAction, StaffProfile
from app.routers.admin_performance import admin_or_hr, my_access
from app.services import performance_ops as ops
from app.services import performance_weekly as weekly
from app.services.auth import get_current_admin
from app.services.performance_config import log_audit
from app.services.performance_scope import get_performance_scope
from app.services.performance_time import today_nairobi
from app.services.performance_permissions import needs

router = APIRouter(prefix="/api/admin/performance/ops", tags=["admin-performance-ops"])
internal = APIRouter(prefix="/api/internal/performance", tags=["internal-performance"])
_NOT_FOUND = HTTPException(status_code=404, detail="That wasn't found.")
TRACKED = {"coaching", "pip", "pip_extended", "probation_extend", "verbal_warning", "first_written_warning", "final_warning", "talk"}


def _scope(admin: AdminUser, db: Session):
    return get_performance_scope(admin, db)


def _visible_live(db: Session, scope) -> list[str]:
    q = db.query(StaffProfile.id).filter(StaffProfile.status.in_(ops.LIVE), StaffProfile.is_active.is_(True))
    ids = [i for (i,) in q]
    return ids if scope.visible_ids is None else [i for i in ids if i in scope.visible_ids]


@router.get("/weekly", dependencies=[my_access])
def weekly_view(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    """The manager's weekly list: who needs a conversation this week, why, and what is waiting for me to verify."""
    scope = _scope(admin, db)
    if scope.own_profile_id is None and not (scope.is_admin or scope.is_hr):
        raise _NOT_FOUND
    return weekly.build(db, scope, today_nairobi())


@router.get("/reminders/me", dependencies=[my_access])
def my_reminders(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"items": ops.due_items(db, today_nairobi()).get(admin.id, [])}


def _action_json(a: PerformanceAction, name: str | None) -> dict[str, Any]:
    return {"id": a.id, "profile_id": a.profile_id, "name": name, "action_type": a.action_type, "restricted": a.restricted, "started_on": a.started_on.isoformat(),
            "review_on": a.review_on.isoformat() if a.review_on else None, "ends_on": a.ends_on.isoformat() if a.ends_on else None, "outcome": a.outcome, "note": a.note}


@router.get("/actions/open", dependencies=[my_access])
def open_actions(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = _scope(admin, db)
    rows = db.query(PerformanceAction).filter(PerformanceAction.outcome.is_(None), PerformanceAction.action_type.in_(TRACKED)).order_by(PerformanceAction.review_on.asc().nulls_last()).all()
    profiles = {p.id: p for p in db.query(StaffProfile).filter(StaffProfile.id.in_([a.profile_id for a in rows] or [""]))}
    out = []
    for a in rows:
        p = profiles.get(a.profile_id)
        if p is None or not scope.can_see(p.id) or (a.restricted and not scope.can_see_restricted(p)):
            continue
        out.append(_action_json(a, p.full_name))
    today = today_nairobi()
    for r in out:
        r["overdue"] = bool(r["review_on"] and date.fromisoformat(r["review_on"]) < today)
    return {"items": out}


class ActionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_on: date | None = None
    ends_on: date | None = None
    note: str | None = Field(default=None, max_length=2000)
    outcome: str | None = Field(default=None, min_length=3, max_length=200)  # setting it closes the item


@router.patch("/actions/{action_id}", dependencies=[my_access])
def update_action(action_id: str, body: ActionUpdate, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = _scope(admin, db)
    a = db.get(PerformanceAction, action_id)
    p = db.get(StaffProfile, a.profile_id) if a else None
    if a is None or p is None or not scope.can_see(p.id) or (a.restricted and not scope.can_see_restricted(p)):
        raise _NOT_FOUND
    direct = scope.own_profile_id is not None and p.manager_id == scope.own_profile_id
    allowed = (scope.is_hr or direct) if a.restricted else (scope.is_hr or scope.is_admin or direct)
    if scope.read_only or not allowed:
        raise HTTPException(status_code=403, detail="You don't have permission to change that.")
    if a.outcome is not None:
        raise HTTPException(status_code=409, detail="That item is closed.")
    for field in ("review_on", "ends_on", "note", "outcome"):
        if field in body.model_fields_set:
            setattr(a, field, getattr(body, field))
    db.commit()
    log_audit(db, admin.username, "action_closed" if body.outcome else "action_updated", "performance_action", a.id, {"profile_id": p.id, "type": a.action_type})
    return _action_json(a, p.full_name)


@router.get("/anomalies", dependencies=[needs("ops.data_quality")])
def get_anomalies(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"items": ops.anomalies(db, _visible_live(db, _scope(admin, db)))}


@router.get("/data-quality", dependencies=[needs("ops.data_quality")])
def get_data_quality(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"issues": ops.data_quality(db, today_nairobi())}


@router.get("/calibration", dependencies=[my_access])
def get_calibration(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = _scope(admin, db)
    if scope.visible_ids is not None:
        raise _NOT_FOUND
    return ops.calibration(db, _visible_live(db, scope), today_nairobi())


@router.get("/leaderboard/units", dependencies=[my_access])
def leaderboard(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = _scope(admin, db)
    units = None if scope.visible_ids is None else sorted(scope.managed_unit_ids)
    return {"items": ops.units_leaderboard(db, units, today_nairobi())}


@router.get("/timeline/{profile_id}", dependencies=[my_access])
def get_timeline(profile_id: str, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = _scope(admin, db)
    p = db.get(StaffProfile, profile_id)
    if p is None or not scope.can_see(profile_id):
        raise _NOT_FOUND
    return {"items": ops.timeline(db, p, scope.can_see_restricted(p))}


@router.get("/training-themes", dependencies=[my_access])
def get_training(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = _scope(admin, db)
    if not (scope.is_admin or scope.is_hr or scope.managed_unit_ids or scope.visible_ids is None):
        raise _NOT_FOUND
    ids = [i for (i,) in db.query(StaffProfile.id)]
    ids = ids if scope.visible_ids is None else [i for i in ids if i in scope.visible_ids]
    return ops.training_themes(db, ids)


def _cron_guard(x_cron_secret: str | None) -> None:
    secret = get_settings().performance_cron_token
    if not secret:
        raise HTTPException(status_code=404, detail="Not found.")
    if not x_cron_secret or not hmac.compare_digest(x_cron_secret.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="Invalid secret.")


@internal.post("/weekly-digest")
def weekly_digest(x_cron_secret: str | None = Header(default=None), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Cron, Monday morning: a counts-only email/SMS to each manager who has someone needing attention."""
    _cron_guard(x_cron_secret)
    return ops.send_weekly(db, today_nairobi())


@internal.post("/daily-digest")
def daily_digest(x_cron_secret: str | None = Header(default=None), db: Session = Depends(get_db)) -> dict[str, Any]:
    secret = get_settings().performance_cron_token
    if not secret:
        raise HTTPException(status_code=404, detail="Not found.")
    if not x_cron_secret or not hmac.compare_digest(x_cron_secret.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="Invalid secret.")
    return ops.send_digest(db, today_nairobi())
