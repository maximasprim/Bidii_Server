"""
Staff Performance - projections, suggestions and AI advice (Phase 4a).  Prefix /api/admin/performance/insights

  GET   /projection/me | /projection/person/{id} | /projection/unit/{id} | /projection/region/{id} | /projection/organisation
  GET   /outlook/me | /outlook/person/{id}   month-by-month chart series: actual, projection (with range) and the path needed to reach a goal
  POST  /plans   save the goal and path as a plan | GET /plans/person/{id} history | POST /plans/{id}/close
  GET   /recommendations            what I may see (?status=open|all, ?entity_type=, ?entity_id=)
  POST  /recommendations/generate   manager / HR: run the rules for my reporting line (HR/admin: everyone)
  PATCH /recommendations/{id}       acknowledge | action | dismiss (a note is needed to dismiss); assignee and due date for managers
  POST  /recommendations/{id}/advice   AI advice from pseudonymised figures (rate limited, falls back to the rules)

Projections are ranges from past verified results, labelled with method and data window. Suggestions never change a rating,
status or PIP. Restricted (PIP) suggestions are visible only to HR, the direct manager and the person. Out-of-scope ids answer 404.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import PerformancePlan, PerformanceUnit, Recommendation, StaffProfile
from app.routers.admin_performance import my_access
from app.services import performance_advice as advice
from app.services import performance_outlook as outlook
from app.services import performance_plans as plans
from app.services.auth import get_current_admin
from app.services import performance_insights as ins
from app.services import performance_reports as pr
from app.services.performance_config import get_org_config, log_audit
from app.services.performance_ratelimit import rate_limited
from app.services.performance_scope import PerformanceScope, get_performance_scope
from app.services.performance_time import today_nairobi

router = APIRouter(prefix="/api/admin/performance/insights", tags=["admin-performance-insights"])
ai_limit = Depends(rate_limited("insights-ai", limit=20, window_seconds=3600))
generate_limit = Depends(rate_limited("insights-generate", limit=30, window_seconds=3600))
_NOT_FOUND = HTTPException(status_code=404, detail="That wasn't found.")
LIVE = ("active", "probation", "pip")


def _fy(db: Session) -> int:
    return int(get_org_config(db)[0].get("fiscal_year_start_month", 1))


def _ctx_for(db: Session, ids: list[str]) -> tuple[pr.Ctx, Any, list[dict[str, Any]]]:
    period = pr.resolve_period("ytd", today_nairobi(), _fy(db))
    ctx = pr.load_ctx(db, ids, period.start, period.end)
    return ctx, period, pr.score_many(ctx, ids, period)


def _live(db: Session, ids: list[str] | None) -> list[str]:
    q = db.query(StaffProfile.id).filter(StaffProfile.status.in_(LIVE), StaffProfile.is_active.is_(True))
    if ids is not None:
        q = q.filter(StaffProfile.id.in_(ids))
    return [i for (i,) in q]


def _group_payload(db: Session, scope: PerformanceScope, ids: list[str]) -> dict[str, Any]:
    ids = ids if scope.visible_ids is None else [i for i in ids if i in scope.visible_ids]
    ids = _live(db, ids)
    ctx, period, people = _ctx_for(db, ids)
    fy = _fy(db)
    projs = [ins.project_person(ctx, p, fy) for p in people]
    return {"people_counted": len(ids), "year_end": ins.project_group(projs, "year_end"), "quarter_end": ins.project_group(projs, "quarter_end"),
            "method": "Sum of individual projections over sum of targets; ranges, not promises."}


@router.get("/projection/me", dependencies=[my_access])
def projection_me(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    if scope.own_profile_id is None:
        raise _NOT_FOUND
    ctx, _p, people = _ctx_for(db, [scope.own_profile_id])
    return ins.project_person(ctx, people[0], _fy(db)) if people else {"available": False}


@router.get("/projection/person/{profile_id}", dependencies=[my_access])
def projection_person(profile_id: str, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    if not scope.can_see(profile_id):
        raise _NOT_FOUND
    ctx, _p, people = _ctx_for(db, [profile_id])
    if not people:
        raise _NOT_FOUND
    return {"profile_id": profile_id, "name": people[0]["name"], **ins.project_person(ctx, people[0], _fy(db))}


@router.get("/projection/unit/{unit_id}", dependencies=[my_access])
def projection_unit(unit_id: str, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    if db.get(PerformanceUnit, unit_id) is None or not (scope.visible_ids is None or unit_id in scope.managed_unit_ids):
        raise _NOT_FOUND
    ids = [i for (i,) in db.query(StaffProfile.id).filter(StaffProfile.unit_id == unit_id)]
    return {"unit_id": unit_id, **_group_payload(db, scope, ids)}


@router.get("/projection/region/{region_id}", dependencies=[my_access])
def projection_region(region_id: str, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    if not (scope.visible_ids is None or region_id in scope.managed_region_ids):
        raise _NOT_FOUND
    unit_ids = pr.region_unit_ids(db, region_id, today_nairobi())
    if not unit_ids:
        raise _NOT_FOUND
    ids = [i for (i,) in db.query(StaffProfile.id).filter(StaffProfile.unit_id.in_(unit_ids))]
    return {"region_id": region_id, **_group_payload(db, scope, ids)}


@router.get("/projection/organisation", dependencies=[my_access])
def projection_org(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    if scope.visible_ids is not None:
        raise _NOT_FOUND
    return _group_payload(db, scope, [i for (i,) in db.query(StaffProfile.id)])


# ---- outlook chart (actual / projection / path needed) and saved plans -------------------------------------------------------


class PlanSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=36)
    goal_pct: float | None = Field(default=None, gt=0, le=300)
    months: int = Field(default=3, ge=1, le=outlook.MAX_PLAN_MONTHS)
    recommendation_id: str | None = Field(default=None, max_length=36)
    note: str | None = Field(default=None, max_length=500)


class PlanClose(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=500)


def _can_manage_plan(scope: PerformanceScope, profile_id: str) -> bool:
    """Managers and HR/admin for people they can see; never the executive view, and never someone setting their own plan (HR/admin excepted)."""
    if scope.read_only or not scope.can_see(profile_id):
        return False
    return not (profile_id == scope.own_profile_id and not (scope.is_admin or scope.is_hr))


def _rolling_ctx(db: Session, profile_id: str) -> tuple[pr.Ctx, list[dict[str, Any]]]:
    """The last 12 months to today (not 'year to date'), so the chart and a saved plan carry on across the year end."""
    today = today_nairobi()
    y, m0 = divmod(today.year * 12 + today.month - 12, 12)
    period = pr.resolve_period(f"{y:04d}-{m0 + 1:02d}..{today.year:04d}-{today.month:02d}", today, _fy(db))
    ctx = pr.load_ctx(db, [profile_id], period.start, period.end)
    return ctx, pr.score_many(ctx, [profile_id], period)


def _outlook_core(db: Session, profile_id: str, goal_pct: float | None, months: int) -> dict[str, Any]:
    ctx, people = _rolling_ctx(db, profile_id)
    if not people:
        raise _NOT_FOUND
    person = people[0]
    if person["headline"] is None:
        return {"profile_id": profile_id, "name": person["name"], "available": False, "reason": "No sales-type KPI to chart for this role.", "points": [], "projection": {"available": False}, "plan": {"available": False}, "goal_options": []}
    return {"profile_id": profile_id, "name": person["name"], "headline": person["headline"]["label"], **outlook.build_outlook(person["trend"], ctx.scale.categories, goal_pct, months)}


def _outlook_payload(db: Session, scope: PerformanceScope, profile_id: str, goal_pct: float | None, months: int) -> dict[str, Any]:
    result = _outlook_core(db, profile_id, goal_pct, months)
    can_manage = _can_manage_plan(scope, profile_id)
    result["can_manage_plan"] = can_manage
    result["tracked_plan"] = None
    try:  # a problem reading saved plans must never take the chart itself down
        active = plans.active_plan(db, profile_id)
        if active is not None:
            result = outlook.attach_saved_plan(result, active.path or [])
            result["tracked_plan"] = plans.to_json(active, outlook.actuals_by_month(result), can_manage=can_manage)
    except SQLAlchemyError:
        db.rollback()
        result["plans_unavailable"] = True
    return result


@router.get("/outlook/me", dependencies=[my_access])
def outlook_me(goal_pct: float | None = Query(default=None, gt=0, le=300), months: int = Query(default=3, ge=1, le=outlook.MAX_PLAN_MONTHS), admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    if scope.own_profile_id is None:
        raise _NOT_FOUND
    return _outlook_payload(db, scope, scope.own_profile_id, goal_pct, months)


@router.get("/outlook/person/{profile_id}", dependencies=[my_access])
def outlook_person(profile_id: str, goal_pct: float | None = Query(default=None, gt=0, le=300), months: int = Query(default=3, ge=1, le=outlook.MAX_PLAN_MONTHS), admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    if not scope.can_see(profile_id):
        raise _NOT_FOUND
    return _outlook_payload(db, scope, profile_id, goal_pct, months)


@router.post("/plans", dependencies=[my_access])
def save_plan(body: PlanSave, response: Response, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    if not scope.can_see(body.profile_id):
        raise _NOT_FOUND
    if not _can_manage_plan(scope, body.profile_id):
        raise HTTPException(status_code=403, detail="You can't save a plan for this person.")
    if body.recommendation_id:
        rec = db.get(Recommendation, body.recommendation_id)
        prof = db.get(StaffProfile, rec.entity_id) if rec is not None and rec.entity_type == "staff" else None
        if rec is None or prof is None or prof.id != body.profile_id or not ins.can_view(scope, rec, prof):
            raise HTTPException(status_code=422, detail="That suggestion isn't for this person.")
    result = _outlook_core(db, body.profile_id, body.goal_pct, body.months)  # the path is worked out here, never taken from the browser
    try:
        plan, created = plans.save_plan(db, profile_id=body.profile_id, result=result, recommendation_id=body.recommendation_id, note=body.note, username=admin.username)
    except plans.PlanError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    if created:
        log_audit(db, admin.username, "plan.save", "plan", plan.id, {"goal_pct": plan.goal_pct, "months": plan.plan_months, "from_suggestion": bool(plan.recommendation_id)})
    response.status_code = 201 if created else 200
    return {"created": created, "plan": plans.to_json(plan, outlook.actuals_by_month(result), can_manage=True)}


@router.get("/plans/person/{profile_id}", dependencies=[my_access])
def plan_history(profile_id: str, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    if not scope.can_see(profile_id):
        raise _NOT_FOUND
    can_manage = _can_manage_plan(scope, profile_id)
    rows = plans.history(db, profile_id)
    actuals = outlook.actuals_by_month(_outlook_core(db, profile_id, None, 3)) if any(p.status == "active" for p in rows) else {}
    return {"can_manage": can_manage, "items": [plans.to_json(p, actuals, can_manage=can_manage) for p in rows]}


@router.post("/plans/{plan_id}/close", dependencies=[my_access])
def close_plan(plan_id: str, body: PlanClose, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    plan = db.get(PerformancePlan, plan_id)
    if plan is None or not scope.can_see(plan.profile_id):
        raise _NOT_FOUND
    if not _can_manage_plan(scope, plan.profile_id):
        raise HTTPException(status_code=403, detail="You can't close this plan.")
    actuals = outlook.actuals_by_month(_outlook_core(db, plan.profile_id, None, 3))
    try:
        plans.close_plan(db, plan, username=admin.username, note=body.note, actuals=actuals)
    except plans.PlanError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    log_audit(db, admin.username, "plan.close", "plan", plan.id, {"state": (plan.result or {}).get("state")})
    return {"plan": plans.to_json(plan, None, can_manage=True)}


# ---- suggestions ----------------------------------------------------------------------------------------------------------


class RecStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["acknowledged", "actioned", "dismissed"]
    note: str | None = Field(default=None, max_length=500)
    assignee_profile_id: str | None = None
    due_on: date | None = None


def _rec_or_404(db: Session, scope: PerformanceScope, rec_id: str) -> tuple[Recommendation, StaffProfile | None]:
    rec = db.get(Recommendation, rec_id)
    if rec is None:
        raise _NOT_FOUND
    prof = db.get(StaffProfile, rec.entity_id) if rec.entity_type == "staff" else None
    if not ins.can_view(scope, rec, prof):
        raise _NOT_FOUND
    return rec, prof


def _names(db: Session, recs: list[Recommendation]) -> dict[tuple[str, str], str]:
    staff = [r.entity_id for r in recs if r.entity_type == "staff"]
    units = [r.entity_id for r in recs if r.entity_type == "unit"]
    out: dict[tuple[str, str], str] = {}
    if staff:
        out.update({("staff", i): n for i, n in db.query(StaffProfile.id, StaffProfile.full_name).filter(StaffProfile.id.in_(staff))})
    if units:
        out.update({("unit", i): n for i, n in db.query(PerformanceUnit.id, PerformanceUnit.name).filter(PerformanceUnit.id.in_(units))})
    return out


@router.get("/recommendations", dependencies=[my_access])
def list_recommendations(
    status: Literal["open", "all"] = "open", entity_type: Literal["staff", "unit"] | None = None, entity_id: str | None = Query(default=None, max_length=36),
    admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db),
) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    q = db.query(Recommendation)
    if status == "open":
        q = q.filter(Recommendation.status.in_(ins.OPEN))
    if entity_type:
        q = q.filter(Recommendation.entity_type == entity_type)
    if entity_id:
        q = q.filter(Recommendation.entity_id == entity_id)
    rows = q.order_by(Recommendation.created_at.desc()).limit(500).all()
    profiles = {p.id: p for p in db.query(StaffProfile).filter(StaffProfile.id.in_([r.entity_id for r in rows if r.entity_type == "staff"] or [""]))}
    seen = [r for r in rows if ins.can_view(scope, r, profiles.get(r.entity_id))]
    names = _names(db, seen)
    sev = {"urgent": 0, "action": 1, "info": 2}
    seen.sort(key=lambda r: sev.get(r.severity, 3))
    return {"items": [ins.to_json(r, names.get((r.entity_type, r.entity_id))) for r in seen], "can_generate": not scope.read_only and (scope.is_admin or scope.is_hr or bool(scope.managed_unit_ids) or scope.visible_ids is not None)}


@router.post("/recommendations/generate", dependencies=[my_access, generate_limit])
def generate(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    if scope.read_only or (scope.own_profile_id is not None and not (scope.is_admin or scope.is_hr) and scope.visible_ids is not None and len(scope.visible_ids) <= 1):
        raise HTTPException(status_code=403, detail="You can't run suggestions.")
    ids = _live(db, None if scope.visible_ids is None else [i for i in scope.visible_ids if i != scope.own_profile_id])
    units = [u for (u,) in db.query(PerformanceUnit.id)] if scope.visible_ids is None else list(scope.managed_unit_ids)
    result = ins.generate(db, ids, unit_ids=units)
    log_audit(db, admin.username, "recommendation.generate", "recommendation", "batch", {"created": result["created"], "updated": result["updated"]})
    return result


@router.patch("/recommendations/{rec_id}", dependencies=[my_access])
def update_status(rec_id: str, body: RecStatus, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    rec, prof = _rec_or_404(db, scope, rec_id)
    is_self = prof is not None and prof.id == scope.own_profile_id and not (scope.is_admin or scope.is_hr)
    if scope.read_only or (is_self and body.status != "acknowledged"):
        raise HTTPException(status_code=403, detail="You can only acknowledge this one.")
    if body.status == "dismissed" and not (body.note or "").strip():
        raise HTTPException(status_code=422, detail="Please say why you're dismissing it.")
    if body.assignee_profile_id and (is_self or not scope.can_see(body.assignee_profile_id)):
        raise HTTPException(status_code=422, detail="That person can't be assigned.")
    rec.status, rec.status_note = body.status, (body.note or None)
    if body.assignee_profile_id is not None:
        rec.assignee_profile_id = body.assignee_profile_id
    if body.due_on is not None:
        rec.due_on = body.due_on
    rec.acted_by_username = admin.username
    from datetime import datetime, timezone
    rec.acted_at = datetime.now(timezone.utc)
    db.commit()
    log_audit(db, admin.username, f"recommendation.{body.status}", "recommendation", rec.id, {"rule": rec.rule_key})
    return ins.to_json(rec, _names(db, [rec]).get((rec.entity_type, rec.entity_id)))


def _advice_payload(db: Session, rec: Recommendation) -> dict[str, Any]:
    """Pseudonymised figures only: no names, ids, units or free text leave the server."""
    base: dict[str, Any] = {"rule": rec.rule_key, "severity": rec.severity, "figures": rec.content.get("figures") or {}, "baseline": rec.baseline or {}}
    if rec.entity_type == "staff":
        ctx, period, people = _ctx_for(db, [rec.entity_id])
        if people:
            p = people[0]
            base.update({"subject": "Staff A", "role": p["role_name"], "period": period.label, "score": p["score"], "category": p["category"],
                         "monthly_achievement_pct": [t["pct"] for t in p["trend"]],
                         "kpi_achievement_pct": {k["label"]: k.get("achievement_pct") for k in p["kpis"] if k.get("achievement_pct") is not None},
                         "projection": ins.project_person(ctx, p, _fy(db)).get("horizons", {}).get("year_end", {}).get("projection")})
    else:
        base["subject"] = "A branch"
    return base


@router.post("/recommendations/{rec_id}/advice", dependencies=[my_access, ai_limit])
def get_advice(rec_id: str, regenerate: bool = False, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    rec, prof = _rec_or_404(db, scope, rec_id)
    if prof is not None and prof.id == scope.own_profile_id and not (scope.is_admin or scope.is_hr):
        raise HTTPException(status_code=403, detail="AI advice is for managers and HR.")
    if scope.read_only:
        raise HTTPException(status_code=403, detail="Read-only access.")
    result = advice.advise(db, _advice_payload(db, rec), title=rec.title, detail=rec.content.get("detail") or "", username=admin.username, regenerate=regenerate)
    rec.content = {**rec.content, "advice": result}
    db.commit()
    log_audit(db, admin.username, "recommendation.advice", "recommendation", rec.id, {"source": result.get("source")})
    return result
