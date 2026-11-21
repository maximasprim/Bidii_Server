"""
Staff Performance - projections, suggestions and AI advice (Phase 4a).  Prefix /api/admin/performance/insights

  GET   /projection/me | /projection/person/{id} | /projection/unit/{id} | /projection/region/{id} | /projection/organisation
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

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import PerformanceUnit, Recommendation, StaffProfile
from app.routers.admin_performance import my_access
from app.services import performance_advice as advice
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
