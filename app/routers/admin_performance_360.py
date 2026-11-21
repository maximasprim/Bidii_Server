"""
Staff Performance - 360 feedback (Phase 6c).  Prefix /api/admin/performance/feedback360

HR / admin:  POST /cycles   GET /cycles   GET /cycles/{id}   POST /cycles/{id}/status   POST /cycles/{id}/subjects
             DELETE /cycles/{id}/subjects/{subject_id}   GET /suggest/{profile_id}
Raters:      GET /my-requests   GET /requests/{rater_id}   PUT /requests/{rater_id}   POST /requests/{rater_id}/decline
Results:     GET /my-results   GET /results/{subject_id}  (subject, their direct manager, HR/admin; only after HR releases the cycle)

Developmental only. Small groups are hidden, comments are shuffled and unattributed, raters never see each other.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import Feedback360Cycle, Feedback360Rater, Feedback360Subject, StaffProfile
from app.routers.admin_performance import admin_or_hr, my_access
from app.services import performance_360 as f
from app.services.auth import get_current_admin
from app.services.internal_notifications import notify
from app.services.performance_config import log_audit
from app.services.performance_scope import get_performance_scope

router = APIRouter(prefix="/api/admin/performance/feedback360", tags=["admin-performance-360"])
_NOT_FOUND = HTTPException(status_code=404, detail="That wasn't found.")


class CycleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=120)
    min_group: int = Field(default=3, ge=2, le=10)
    closes_on: date | None = None


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: Literal["open", "closed", "released"]


class RaterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(max_length=36)
    relationship: Literal["self", "manager", "peer", "direct_report", "other"]


class SubjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(max_length=36)
    raters: list[RaterIn] | None = Field(default=None, max_length=20)


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: dict[str, int]
    strengths: str | None = Field(default=None, max_length=1000)
    development: str | None = Field(default=None, max_length=1000)


def _cycle(db: Session, cid: str) -> Feedback360Cycle:
    c = db.get(Feedback360Cycle, cid)
    if c is None:
        raise _NOT_FOUND
    return c


def _cycle_json(c: Feedback360Cycle) -> dict[str, Any]:
    return {"id": c.id, "name": c.name, "status": c.status, "min_group": c.min_group, "closes_on": c.closes_on.isoformat() if c.closes_on else None}


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@router.post("/cycles", dependencies=[admin_or_hr], status_code=201)
def create_cycle(body: CycleBody, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    c = Feedback360Cycle(name=body.name.strip(), min_group=body.min_group, closes_on=body.closes_on, created_by_username=admin.username)
    db.add(c)
    db.commit()
    log_audit(db, admin.username, "f360.cycle_created", "feedback360_cycle", c.id)
    return _cycle_json(c)


@router.get("/cycles", dependencies=[admin_or_hr])
def list_cycles(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"items": [_cycle_json(c) for c in db.query(Feedback360Cycle).order_by(Feedback360Cycle.created_at.desc())]}


@router.get("/cycles/{cycle_id}", dependencies=[admin_or_hr])
def get_cycle(cycle_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    c = _cycle(db, cycle_id)
    return {**_cycle_json(c), "progress": f.progress(db, c)}


@router.get("/suggest/{profile_id}", dependencies=[admin_or_hr])
def suggest(profile_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    p = db.get(StaffProfile, profile_id)
    if p is None:
        raise _NOT_FOUND
    names = {i: n for i, n in db.query(StaffProfile.id, StaffProfile.full_name)}
    return {"raters": [{"profile_id": i, "name": names.get(i), "relationship": rel} for i, rel in f.suggest_raters(db, p)]}


@router.post("/cycles/{cycle_id}/subjects", dependencies=[admin_or_hr], status_code=201)
def add_subject(cycle_id: str, body: SubjectBody, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    c = _cycle(db, cycle_id)
    p = db.get(StaffProfile, body.profile_id)
    if p is None:
        raise _NOT_FOUND
    try:
        s = f.add_subject(db, c, p, None if body.raters is None else [(r.profile_id, r.relationship) for r in body.raters])
    except f.F360Error as exc:
        raise _bad(exc) from exc
    log_audit(db, admin.username, "f360.subject_added", "feedback360_subject", s.id, {"cycle": c.id})
    return {"subject_id": s.id}


@router.delete("/cycles/{cycle_id}/subjects/{subject_id}", dependencies=[admin_or_hr])
def remove_subject(cycle_id: str, subject_id: str, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    c = _cycle(db, cycle_id)
    s = db.get(Feedback360Subject, subject_id)
    if s is None or s.cycle_id != c.id:
        raise _NOT_FOUND
    if c.status != "draft":
        raise HTTPException(status_code=409, detail="Subjects can only be removed while the cycle is a draft.")
    db.query(Feedback360Rater).filter_by(subject_id=s.id).delete()
    db.delete(s)
    db.commit()
    log_audit(db, admin.username, "f360.subject_removed", "feedback360_subject", subject_id)
    return {"removed": True}


@router.post("/cycles/{cycle_id}/status", dependencies=[admin_or_hr])
def set_status(cycle_id: str, body: StatusBody, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    c = _cycle(db, cycle_id)
    try:
        f.transition(db, c, body.to)
    except f.F360Error as exc:
        raise _bad(exc) from exc
    if body.to == "open":
        user_ids = {u for (u,) in db.query(StaffProfile.admin_user_id).join(Feedback360Rater, Feedback360Rater.rater_profile_id == StaffProfile.id)
                    .join(Feedback360Subject, Feedback360Subject.id == Feedback360Rater.subject_id).filter(Feedback360Subject.cycle_id == c.id, StaffProfile.admin_user_id.is_not(None))}
        for uid in user_ids:
            notify(db, recipient_admin_id=uid, message="You have been asked to give feedback. It takes a few minutes and your answers stay anonymous.", link_path="/admin/performance/feedback")
        db.commit()
    log_audit(db, admin.username, f"f360.{body.to}", "feedback360_cycle", c.id)
    return _cycle_json(c)


# ---- raters ------------------------------------------------------------------------------------------------------------------


def _mine(db: Session, admin: AdminUser, rater_id: str) -> tuple[Feedback360Rater, Feedback360Subject, Feedback360Cycle]:
    scope = get_performance_scope(admin, db)
    r = db.get(Feedback360Rater, rater_id)
    if r is None or scope.own_profile_id is None or r.rater_profile_id != scope.own_profile_id:
        raise _NOT_FOUND
    s = db.get(Feedback360Subject, r.subject_id)
    return r, s, db.get(Feedback360Cycle, s.cycle_id)


def _subject_label(db: Session, r: Feedback360Rater, s: Feedback360Subject) -> str:
    return "Yourself" if r.relationship == "self" else (db.get(StaffProfile, s.profile_id).full_name)


@router.get("/my-requests", dependencies=[my_access])
def my_requests(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    if scope.own_profile_id is None:
        return {"items": []}
    out = []
    for r in db.query(Feedback360Rater).filter_by(rater_profile_id=scope.own_profile_id):
        s = db.get(Feedback360Subject, r.subject_id)
        c = db.get(Feedback360Cycle, s.cycle_id)
        if c.status == "open":
            out.append({"rater_id": r.id, "cycle": c.name, "subject": _subject_label(db, r, s), "relationship": r.relationship, "status": r.status, "closes_on": c.closes_on.isoformat() if c.closes_on else None})
    return {"items": out}


@router.get("/requests/{rater_id}", dependencies=[my_access])
def get_request(rater_id: str, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    r, s, c = _mine(db, admin, rater_id)
    if c.status != "open":
        raise _NOT_FOUND
    return {"rater_id": r.id, "cycle": c.name, "subject": _subject_label(db, r, s), "relationship": r.relationship, "status": r.status, "questions": f.QUESTIONS,
            "answers": r.answers, "strengths": r.strengths, "development": r.development, "anonymity": "Peers, direct reports and others are shown only as a group of at least %d." % c.min_group}


@router.put("/requests/{rater_id}", dependencies=[my_access])
def answer(rater_id: str, body: AnswerBody, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    r, _s, c = _mine(db, admin, rater_id)
    if r.status == "declined":
        raise HTTPException(status_code=409, detail="You declined this request.")
    try:
        f.submit(db, r, c, body.answers, body.strengths, body.development)
    except f.F360Error as exc:
        raise _bad(exc) from exc
    log_audit(db, admin.username, "f360.submitted", "feedback360_rater", r.id)  # who answered, never what
    return {"status": r.status}


@router.post("/requests/{rater_id}/decline", dependencies=[my_access])
def decline(rater_id: str, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    r, _s, c = _mine(db, admin, rater_id)
    if c.status != "open" or r.status == "submitted":
        raise HTTPException(status_code=409, detail="That can't be declined now.")
    r.status = "declined"
    db.commit()
    return {"status": r.status}


# ---- results -----------------------------------------------------------------------------------------------------------------


def _released(db: Session, s: Feedback360Subject) -> tuple[Feedback360Cycle, dict[str, Any]]:
    c = db.get(Feedback360Cycle, s.cycle_id)
    if c.status != "released":
        raise _NOT_FOUND
    p = db.get(StaffProfile, s.profile_id)
    return c, {"subject": p.full_name if p else None, "cycle": c.name, **f.results(db, c, s)}


@router.get("/my-results", dependencies=[my_access])
def my_results(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    if scope.own_profile_id is None:
        return {"items": []}
    out = []
    for s in db.query(Feedback360Subject).filter_by(profile_id=scope.own_profile_id):
        try:
            out.append({"subject_id": s.id, **_released(db, s)[1]})
        except HTTPException:
            continue
    return {"items": out}


@router.get("/team-results", dependencies=[my_access])
def team_results(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Released results for people I manage directly, so I can hold the development conversation."""
    scope = get_performance_scope(admin, db)
    if scope.own_profile_id is None:
        return {"items": []}
    out = []
    for s in db.query(Feedback360Subject).join(StaffProfile, StaffProfile.id == Feedback360Subject.profile_id).filter(StaffProfile.manager_id == scope.own_profile_id):
        try:
            out.append({"subject_id": s.id, **_released(db, s)[1]})
        except HTTPException:
            continue
    if out:
        log_audit(db, admin.username, "f360.team_results_viewed", "feedback360_subject", None, {"count": len(out)})
    return {"items": out}


@router.get("/results/{subject_id}", dependencies=[my_access])
def get_results(subject_id: str, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    s = db.get(Feedback360Subject, subject_id)
    if s is None:
        raise _NOT_FOUND
    p = db.get(StaffProfile, s.profile_id)
    allowed = scope.is_admin or scope.is_hr or (scope.own_profile_id is not None and (s.profile_id == scope.own_profile_id or (p is not None and p.manager_id == scope.own_profile_id)))
    if not allowed:
        raise _NOT_FOUND
    _c, data = _released(db, s)
    if s.profile_id != scope.own_profile_id:
        log_audit(db, admin.username, "f360.results_viewed", "feedback360_subject", s.id)
    return data
