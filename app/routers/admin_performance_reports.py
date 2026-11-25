"""
Staff Performance - scores and reports (Phase 3).  Prefix /api/admin/performance/reports

  GET  /me                      my own score, KPIs, trend and how it was calculated
  GET  /person/{profile_id}     one person (in scope)
  GET  /team                    a leader's reporting line (default: me; ?leader_id=)
  GET  /unit/{unit_id}          a branch / department: heatmap, vs organisation, small-unit caution
  GET  /region/{region_id}      branch comparison, category mix, target vs actual
  GET  /organisation            admin / HR / executive only: distribution by month, heatmap, movers, KES vs target
  GET  /compare                 two periods side by side (month/quarter/year-on-year) for me, a unit, or everyone I can see
  GET  /export                  .xlsx or .pdf of any of the above
  POST /snapshots/recompute     admin/HR: store the results for a period     GET /snapshots  stored history of one person

Every route has a menu guard and goes through get_performance_scope; an id outside the caller's scope answers
exactly like one that doesn't exist (404). Periods: 2026-03, 2026-Q1, 2026-H1, 2026, ytd, 2026-01..2026-06.
All numbers come from verified figures; nothing here calls AI.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import PerformanceUnit, Region, ScoreSnapshot, StaffProfile
from app.routers.admin_performance import admin_or_hr, my_access
from app.services import performance_export as export
from app.services import performance_reports as pr
from app.services.auth import get_current_admin
from app.services.performance_config import effective_config, get_org_config, log_audit
from app.services.performance_periods import PeriodError, months_in, parse_period
from app.services.performance_ratelimit import rate_limited
from app.services.performance_scope import PerformanceScope, get_performance_scope
from app.services.performance_time import today_nairobi

router = APIRouter(prefix="/api/admin/performance/reports", tags=["admin-performance-reports"])
export_limit = Depends(rate_limited("report-export", limit=30, window_seconds=3600))
_NOT_FOUND = HTTPException(status_code=404, detail="That wasn't found.")
LIVE = ("active", "probation", "pip")


def _scope(admin: AdminUser, db: Session) -> PerformanceScope:
    return get_performance_scope(admin, db)


def _period(key: str, db: Session):
    cfg, _v = get_org_config(db)
    try:
        return pr.resolve_period(key, today_nairobi(), int(cfg.get("fiscal_year_start_month", 1)))
    except PeriodError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _visible(scope: PerformanceScope, ids: list[str]) -> list[str]:
    return ids if scope.visible_ids is None else [i for i in ids if i in scope.visible_ids]


def _live_ids(db: Session, ids: list[str] | None = None) -> list[str]:
    q = db.query(StaffProfile.id).filter(StaffProfile.status.in_(LIVE), StaffProfile.is_active.is_(True))
    if ids is not None:
        q = q.filter(StaffProfile.id.in_(ids))
    return [i for (i,) in q]


def _leaver_ids(db: Session, ids: list[str] | None = None) -> list[str]:
    """People who have left or been deactivated. Their months still count towards group totals (never towards people lists or counts)."""
    q = db.query(StaffProfile.id).filter(or_(StaffProfile.status.notin_(LIVE), StaffProfile.is_active.is_(False)))
    if ids is not None:
        q = q.filter(StaffProfile.id.in_(ids))
    return [i for (i,) in q]


def _units_worked_in(ctx: pr.Ctx, ids: list[str], period) -> dict[str, set]:
    """For each person, every unit they belonged to at some point of the period (same test the scoring uses to keep a unit's months)."""
    months = pr.months_in(period)
    return {pid: {pr.unit_at(ctx, ctx.profiles[pid], m) for m in months} for pid in ids if pid in ctx.profiles}


def _show_pip(scope: PerformanceScope, person: dict[str, Any], ctx: pr.Ctx) -> bool:
    return scope.can_see_restricted(ctx.profiles[person["profile_id"]])


def _person_row(scope: PerformanceScope, ctx: pr.Ctx, person: dict[str, Any]) -> dict[str, Any]:
    """The table row for one person. A PIP status is only shown to those entitled to it."""
    prof = ctx.profiles[person["profile_id"]]
    cfg = ctx.cfg.get(prof.performance_role_id) or {}
    row = pr.public(person)
    row.pop("kpis", None)
    row["status"] = scope.display_status(prof)
    row["spark"] = pr.sparkline(person)
    row["attention"] = pr.attention_reasons(person, cfg, _show_pip(scope, person, ctx))
    return row


def _month_keys(period) -> list[str]:
    return [m.strftime("%Y-%m") for m in months_in(period)]


def _heatmap(ctx: pr.Ctx, people: list[dict[str, Any]], period) -> dict[str, Any]:
    months = [m for m in _month_keys(period) if m <= today_nairobi().strftime("%Y-%m")]
    rows = []
    for p in people:
        by = {t["month"]: t["pct"] for t in p["trend"]}
        rows.append({"profile_id": p["profile_id"], "name": p["name"], "cells": [by.get(m) for m in months]})
    return {"months": months, "rows": rows}


def _by_month(ctx: pr.Ctx, ids: list[str], period, *, unit_filter: str | None = None, leaver_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Distribution of people across the four categories for each month (replaces their JAN-OCT / H1 sheets)."""
    out = []
    for m in pr.scored_months(period, ctx.today):
        mp = parse_period(m.strftime("%Y-%m"))
        people = pr.score_many(ctx, ids, mp, unit_filter=unit_filter)
        g = pr.group_summary(ctx, people, pr.score_many(ctx, leaver_ids or [], mp, unit_filter=unit_filter, cap_at_exit=True))
        out.append({"month": mp.key, "achievement_pct": g["achievement_pct"], "average_score": g["average_score"],
                    "category_counts": g["category_counts"], "legacy_counts": g["legacy_counts"], "scored_people": g["scored_people"],
                    "actual_total": g["actual_total"], "target_total": g["target_total"]})
    return out


def _period_json(p) -> dict[str, Any]:
    return {"key": p.key, "kind": p.kind, "label": p.label, "start": p.start.isoformat(), "end": p.end.isoformat()}


def _scale_json(ctx: pr.Ctx) -> dict[str, Any]:
    return {"effective_from": ctx.scale.effective_from.isoformat(), "categories": ctx.scale.categories, "rating_bands": ctx.scale.rating_bands}


def _peer_rank(scope: PerformanceScope, db: Session, ctx: pr.Ctx, person: dict[str, Any], period) -> dict[str, Any] | None:
    """Rank among same-role colleagues in the same unit. The person themselves only gets it when an admin enabled peer rankings."""
    prof = ctx.profiles[person["profile_id"]]
    if prof.id == scope.own_profile_id and not scope.is_hr and not scope.is_admin and not get_org_config(db)[0].get("show_peer_rankings", False):
        return None
    if prof.unit_id is None or person["score"] is None:
        return None
    peers = [i for (i,) in db.query(StaffProfile.id).filter(StaffProfile.unit_id == prof.unit_id, StaffProfile.performance_role_id == prof.performance_role_id, StaffProfile.status.in_(LIVE))]
    if len(peers) < pr.SMALL_UNIT:
        return None
    ctx2 = pr.load_ctx(db, peers, period.start, period.end)
    scores = sorted((p["score"] for p in pr.score_many(ctx2, peers, period) if p["score"] is not None), reverse=True)
    return {"rank": scores.index(person["score"]) + 1, "of": len(scores)} if person["score"] in scores else None


# ---- person ---------------------------------------------------------------------------------------------------------


def _person_payload(db: Session, scope: PerformanceScope, profile_id: str, period_key: str, viewer: AdminUser) -> dict[str, Any]:
    if not scope.can_see(profile_id):
        raise _NOT_FOUND
    period = _period(period_key, db)
    ctx = pr.load_ctx(db, [profile_id], period.start, period.end)
    if profile_id not in ctx.profiles:
        raise _NOT_FOUND
    person = pr.score_person(ctx, profile_id, period)
    prof = ctx.profiles[profile_id]
    row = _person_row(scope, ctx, person)
    row["kpis"] = pr.public(person)["kpis"]
    row["peer_rank"] = _peer_rank(scope, db, ctx, person, period)
    qtd = pr.score_person(ctx, profile_id, parse_period(f"{period.end.year}-Q{(period.end.month - 1) // 3 + 1}"))
    ytd = pr.score_person(ctx, profile_id, pr.resolve_period("ytd", ctx.today))
    if profile_id != scope.own_profile_id:
        log_audit(db, viewer.username, "report.view", "staff_profile", profile_id, {"period": period.key})
    return {
        "period": _period_json(period), "scale": _scale_json(ctx), "person": row,
        "quarter_to_date": {"period": f"{period.end.year}-Q{(period.end.month - 1) // 3 + 1}", "score": qtd["score"], "category": qtd["category"], "headline": qtd["headline"]},
        "year_to_date": {"score": ytd["score"], "category": ytd["category"], "headline": ytd["headline"]},
        "explanation": _explain(row, ctx),
        "show_pip": scope.can_see_restricted(prof),
    }


def _explain(row: dict[str, Any], ctx: pr.Ctx) -> list[str]:
    lines = ["Only figures your manager has verified count. Months with nothing verified are shown as pending, not as zero.",
             "Each KPI is achievement against the target in force that month, capped, then combined by its weight over the KPIs that could be scored."]
    if row.get("competency_score") is not None:
        lines.append("Behavioural competencies from your finalised appraisal are blended in at the configured share.")
    lines.append("Your category uses the rating scale that applied on the last day of the period (" + ", ".join(f"{c['label']} from {c['min_score']:g}" for c in ctx.scale.categories[:-1]) + ").")
    return lines


@router.get("/me", dependencies=[my_access])
def my_report(period: str = Query(default="ytd", max_length=30), db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    scope = _scope(admin, db)
    if not scope.own_profile_id:
        raise HTTPException(status_code=404, detail="You don't have a performance profile yet. Please ask HR to set one up.")
    return _person_payload(db, scope, scope.own_profile_id, period, admin)


@router.get("/person/{profile_id}", dependencies=[my_access])
def person_report(profile_id: str, period: str = Query(default="ytd", max_length=30), db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    return _person_payload(db, _scope(admin, db), profile_id, period, admin)


# ---- groups ---------------------------------------------------------------------------------------------------------


def _group_payload(db: Session, scope: PerformanceScope, ids: list[str], period, *, unit_filter: str | None = None, extra_months: bool = True, leaver_ids: list[str] | None = None) -> tuple[pr.Ctx, list[dict[str, Any]], dict[str, Any]]:
    ctx = pr.load_ctx(db, [*ids, *(leaver_ids or [])], period.start, period.end)
    people = pr.score_many(ctx, ids, period, unit_filter=unit_filter)
    leavers = pr.score_many(ctx, leaver_ids or [], period, unit_filter=unit_filter, cap_at_exit=True)
    return ctx, people, pr.group_summary(ctx, people, leavers)


def _team_for(db: Session, scope: PerformanceScope, leader_id: str, period_key: str) -> dict[str, Any]:
    if not scope.can_see(leader_id):
        raise _NOT_FOUND
    period = _period(period_key, db)
    line = _visible(scope, pr.reporting_line(db, leader_id))
    members = _live_ids(db, line)
    leaver_ids = _leaver_ids(db, line)
    ids = [leader_id, *members]
    ctx, people, _ = _group_payload(db, scope, ids, period, leaver_ids=leaver_ids)
    by_id = {p["profile_id"]: p for p in people}
    if leader_id not in by_id:
        raise _NOT_FOUND
    team_people = [by_id[i] for i in members if i in by_id]
    summary = pr.group_summary(ctx, team_people, pr.score_many(ctx, leaver_ids, period, cap_at_exit=True))
    rows = sorted((_person_row(scope, ctx, p) for p in team_people), key=lambda r: (r["score"] is None, -(r["score"] or 0)))
    return {
        "period": _period_json(period), "scale": _scale_json(ctx), "leader": _person_row(scope, ctx, by_id[leader_id]),
        "summary": summary, "people": rows, "needs_attention": [r for r in rows if r["attention"]],
        "leader_vs_team": {"leader_score": by_id[leader_id]["score"], "team_average": summary["average_score"]},
        "heatmap": _heatmap(ctx, team_people, period),
    }


@router.get("/team", dependencies=[my_access])
def team_report(period: str = Query(default="ytd", max_length=30), leader_id: str | None = Query(default=None, max_length=36), db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    scope = _scope(admin, db)
    leader = leader_id or scope.own_profile_id
    if not leader:
        raise HTTPException(status_code=404, detail="You don't have a performance profile yet.")
    return _team_for(db, scope, leader, period)


def _unit_for(db: Session, scope: PerformanceScope, unit_id: str, period_key: str) -> dict[str, Any]:
    unit = db.get(PerformanceUnit, unit_id)
    if unit is None:
        raise _NOT_FOUND
    period = _period(period_key, db)
    ever = pr.unit_member_ids(db, unit_id, period.start, period.end, None if scope.visible_ids is None else set(scope.visible_ids))
    members = _live_ids(db, ever)
    if not members and scope.visible_ids is not None and unit_id not in scope.managed_unit_ids:
        raise _NOT_FOUND
    ctx, people, summary = _group_payload(db, scope, members, period, unit_filter=unit_id, leaver_ids=_leaver_ids(db, ever))
    all_ids, gone_ids = _live_ids(db), _leaver_ids(db)
    org_ctx = pr.load_ctx(db, [*all_ids, *gone_ids], period.start, period.end)
    org = pr.group_summary(org_ctx, pr.score_many(org_ctx, all_ids, period), pr.score_many(org_ctx, gone_ids, period, cap_at_exit=True))
    rows = sorted((_person_row(scope, ctx, p) for p in people), key=lambda r: (r["score"] is None, -(r["score"] or 0)))
    small = len(members) < pr.SMALL_UNIT
    return {
        "period": _period_json(period), "scale": _scale_json(ctx), "unit": {"id": unit.id, "name": unit.name, "kind": unit.kind},
        "summary": summary, "organisation": {"achievement_pct": org["achievement_pct"], "average_score": org["average_score"]},
        "small_unit": small, "small_unit_note": "With so few people one result moves the whole figure, so read the ranking with care." if small else None,
        "people": rows, "heatmap": _heatmap(ctx, people, period), "needs_attention": [r for r in rows if r["attention"]],
    }


@router.get("/unit/{unit_id}", dependencies=[my_access])
def unit_report(unit_id: str, period: str = Query(default="ytd", max_length=30), db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    return _unit_for(db, _scope(admin, db), unit_id, period)


def _region_for(db: Session, scope: PerformanceScope, region_id: str, period_key: str) -> dict[str, Any]:
    region = db.get(Region, region_id)
    if region is None or (scope.visible_ids is not None and region_id not in scope.managed_region_ids):
        raise _NOT_FOUND
    period = _period(period_key, db)
    unit_ids = pr.region_unit_ids(db, region_id, period.end)
    units = {u.id: u for u in db.query(PerformanceUnit).filter(PerformanceUnit.id.in_(unit_ids))} if unit_ids else {}
    ever_by_unit = {uid: pr.unit_member_ids(db, uid, period.start, period.end, None if scope.visible_ids is None else set(scope.visible_ids)) for uid in units}
    members_by_unit = {uid: _live_ids(db, ever) for uid, ever in ever_by_unit.items()}
    leavers_by_unit = {uid: _leaver_ids(db, ever) for uid, ever in ever_by_unit.items()}
    all_ids = sorted({i for ids in members_by_unit.values() for i in ids})
    all_leavers = sorted({i for ids in leavers_by_unit.values() for i in ids})
    ctx = pr.load_ctx(db, [*all_ids, *all_leavers], period.start, period.end)
    branches, everyone, everyone_left = [], [], []
    for uid, ids in members_by_unit.items():
        people = pr.score_many(ctx, ids, period, unit_filter=uid)
        left = pr.score_many(ctx, leavers_by_unit[uid], period, unit_filter=uid, cap_at_exit=True)
        everyone += people
        everyone_left += left
        g = pr.group_summary(ctx, people, left)
        branches.append({"unit_id": uid, "name": units[uid].name, "people": g["people"], "achievement_pct": g["achievement_pct"], "average_score": g["average_score"],
                         "category": g["average_category"], "category_counts": g["category_counts"], "actual_total": g["actual_total"], "target_total": g["target_total"],
                         "small_unit": len(ids) < pr.SMALL_UNIT})
    branches.sort(key=lambda b: (b["average_score"] is None, -(b["average_score"] or 0)))
    return {"period": _period_json(period), "scale": _scale_json(ctx), "region": {"id": region.id, "name": region.name}, "summary": pr.group_summary(ctx, everyone, everyone_left), "branches": branches}


@router.get("/region/{region_id}", dependencies=[my_access])
def region_report(region_id: str, period: str = Query(default="ytd", max_length=30), db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    return _region_for(db, _scope(admin, db), region_id, period)


def _org_for(db: Session, scope: PerformanceScope, period_key: str) -> dict[str, Any]:
    if scope.visible_ids is not None:
        raise _NOT_FOUND
    period = _period(period_key, db)
    prev = pr.previous_period(period)
    ids = _live_ids(db)
    gone = _leaver_ids(db)
    ctx = pr.load_ctx(db, [*ids, *gone], prev.start, period.end)
    ctx.scale = pr.scale_for_period(db, period.end)  # classify with the scale in force at the END of the period shown
    people = pr.score_many(ctx, ids, period)
    before = {p["profile_id"]: p for p in pr.score_many(ctx, ids, prev)}
    summary = pr.group_summary(ctx, people, pr.score_many(ctx, gone, period, cap_at_exit=True))
    by_month = _by_month(ctx, ids, period, leaver_ids=gone)
    units = {u.id: u for u in db.query(PerformanceUnit).filter(PerformanceUnit.kind == "branch")}
    members = {uid: [i for i in ids if ctx.profiles[i].unit_id == uid] for uid in units}
    gone_units = _units_worked_in(ctx, gone, period)  # score each branch's leavers against that branch only, not against every branch
    gone_in = {uid: [i for i in gone if uid in gone_units.get(i, ())] for uid in units}
    branches, heat = [], []
    for uid, mem in members.items():
        if not mem:
            continue
        g = pr.group_summary(ctx, pr.score_many(ctx, mem, period, unit_filter=uid), pr.score_many(ctx, gone_in[uid], period, unit_filter=uid, cap_at_exit=True))
        branches.append({"unit_id": uid, "name": units[uid].name, "people": g["people"], "actual_total": g["actual_total"], "target_total": g["target_total"],
                         "achievement_pct": g["achievement_pct"], "average_score": g["average_score"], "category": g["average_category"]})
        cells = []
        for m in pr.scored_months(period, ctx.today):
            mp = parse_period(m.strftime("%Y-%m"))
            gm = pr.group_summary(ctx, pr.score_many(ctx, mem, mp, unit_filter=uid), pr.score_many(ctx, gone_in[uid], mp, unit_filter=uid, cap_at_exit=True))
            cells.append(gm["achievement_pct"])
        heat.append({"unit_id": uid, "name": units[uid].name, "cells": cells})
    branches.sort(key=lambda b: (b["achievement_pct"] is None, -(b["achievement_pct"] or 0)))
    movers = []
    for p in people:
        b = before.get(p["profile_id"])
        if p["score"] is not None and b is not None and b["score"] is not None:
            movers.append({"profile_id": p["profile_id"], "name": p["name"], "unit_name": p["unit_name"], "score": p["score"], "previous": b["score"], "change": round(p["score"] - b["score"], 2)})
    movers.sort(key=lambda r: r["change"])
    kes = {"actual": summary["actual_total"], "target": summary["target_total"], "achievement_pct": summary["achievement_pct"]}
    return {
        "period": _period_json(period), "previous": _period_json(prev), "scale": _scale_json(ctx), "summary": summary,
        "by_month": by_month, "branches": branches, "heatmap": {"months": [m["month"] for m in by_month], "rows": heat},
        "top_movers": movers[::-1][:5], "bottom_movers": movers[:5], "kes": kes,
    }


@router.get("/organisation", dependencies=[my_access])
def org_report(period: str = Query(default="ytd", max_length=30), db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    return _org_for(db, _scope(admin, db), period)


# ---- compare --------------------------------------------------------------------------------------------------------


@router.get("/compare", dependencies=[my_access])
def compare(a: str = Query(max_length=30), b: str = Query(max_length=30), unit_id: str | None = Query(default=None, max_length=36), profile_id: str | None = Query(default=None, max_length=36), db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    scope = _scope(admin, db)
    pa, pb = _period(a, db), _period(b, db)
    if profile_id:
        if not scope.can_see(profile_id):
            raise _NOT_FOUND
        ids, unit_filter, label = [profile_id], None, "person"
    elif unit_id:
        if db.get(PerformanceUnit, unit_id) is None:
            raise _NOT_FOUND
        ever = pr.unit_member_ids(db, unit_id, min(pa.start, pb.start), max(pa.end, pb.end), None if scope.visible_ids is None else set(scope.visible_ids))
        ids, gone = _live_ids(db, ever), _leaver_ids(db, ever)
        if not ids and scope.visible_ids is not None and unit_id not in scope.managed_unit_ids:
            raise _NOT_FOUND
        unit_filter, label = unit_id, "unit"
    else:
        ids, unit_filter, label = _live_ids(db, _visible(scope, _live_ids(db)) if scope.visible_ids is not None else None), None, "everyone you can see"
        gone = _leaver_ids(db, _visible(scope, _leaver_ids(db)) if scope.visible_ids is not None else None)
    if profile_id:
        gone = []
    ctx = pr.load_ctx(db, [*ids, *gone], min(pa.start, pb.start), max(pa.end, pb.end))
    out = {}
    for tag, p in (("a", pa), ("b", pb)):
        g = pr.group_summary(ctx, pr.score_many(ctx, ids, p, unit_filter=unit_filter), pr.score_many(ctx, gone, p, unit_filter=unit_filter, cap_at_exit=True))
        out[tag] = {"period": _period_json(p), **{k: g[k] for k in ("people", "achievement_pct", "average_score", "average_category", "category_counts", "legacy_counts", "actual_total", "target_total")}}
    def delta(k):
        x, y = out["a"][k], out["b"][k]
        return round(y - x, 2) if x is not None and y is not None else None
    return {"basis": label, "a": out["a"], "b": out["b"], "change": {"achievement_pct": delta("achievement_pct"), "average_score": delta("average_score"), "actual_total": delta("actual_total")}}


# ---- exports --------------------------------------------------------------------------------------------------------


def _export_rows(db: Session, scope: PerformanceScope, kind: str, ident: str | None, period_key: str, viewer: AdminUser) -> tuple[str, list[dict[str, Any]], str]:
    if kind == "person":
        data = _person_payload(db, scope, ident or "", period_key, viewer)
        p = data["person"]
        return p["name"], [p], data["period"]["label"]
    if kind == "team":
        d = _team_for(db, scope, ident or scope.own_profile_id or "", period_key)
        return f"Team of {d['leader']['name']}", d["people"], d["period"]["label"]
    if kind == "unit":
        d = _unit_for(db, scope, ident or "", period_key)
        return d["unit"]["name"], d["people"], d["period"]["label"]
    if kind == "region":
        d = _region_for(db, scope, ident or "", period_key)
        return f"Region {d['region']['name']}", d["branches"], d["period"]["label"]
    d = _org_for(db, scope, period_key)
    return "Organisation", d["branches"], d["period"]["label"]


@router.get("/export", dependencies=[my_access, export_limit])
def export_report(kind: Literal["person", "team", "unit", "region", "org"], period: str = Query(default="ytd", max_length=30), id: str | None = Query(default=None, max_length=36), format: Literal["xlsx", "pdf"] = "xlsx", db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> Response:
    scope = _scope(admin, db)
    title, rows, label = _export_rows(db, scope, kind, id, period, admin)
    if kind in ("region", "org"):
        headers = ["Branch", "People", "Actual (KES)", "Target (KES)", "Achievement %", "Average score", "Category"]
        table = [[r["name"], r["people"], r["actual_total"], r["target_total"], r["achievement_pct"], r["average_score"], (r["category"] or {}).get("label") if r["category"] else None] for r in rows]
    else:
        headers = ["Name", "Role", "Unit", "Score", "Category", "Achievement %", "1-5 rating", "Legacy band", "Status", "Needs attention"]
        table = [[r["name"], r["role_name"], r["unit_name"], r["score"], (r["category"] or {}).get("label") if r["category"] else None,
                  (r["headline"] or {}).get("pct"), r["rating"], r["legacy_band"], r["status"], "; ".join(r.get("attention", []))] for r in rows]
    subtitle = f"Period: {label}. Only verified figures are counted."
    data = export.build_xlsx(title, subtitle, headers, table) if format == "xlsx" else export.build_pdf(title, subtitle, headers, table)
    log_audit(db, admin.username, "report.export", kind, id, {"period": period, "format": format})
    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if format == "xlsx" else "application/pdf"
    return Response(content=data, media_type=media, headers={"Content-Disposition": f'attachment; filename="performance-report.{format}"', "X-Content-Type-Options": "nosniff"})


# ---- stored snapshots -------------------------------------------------------------------------------------------------


class RecomputeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period: str = Field(max_length=30)


def snapshot_one(db: Session, ctx: pr.Ctx, person: dict[str, Any], period, username: str) -> None:
    row = db.query(ScoreSnapshot).filter_by(profile_id=person["profile_id"], period_key=period.key).first() or ScoreSnapshot(profile_id=person["profile_id"], period_key=period.key, period_start=period.start, period_end=period.end)
    head = person["headline"] or {}
    row.unit_id, row.score = person["unit_id"], person["score"]
    row.category_key = person["category"]["key"] if person["category"] else None
    row.rating, row.legacy_band, row.achievement_pct = person["rating"], person["legacy_band"], head.get("pct")
    row.actual_total, row.target_total, row.coverage_pct = head.get("actual"), head.get("target"), person["coverage_pct"]
    row.scale_effective_from = ctx.scale.effective_from
    row.detail = {"kpis": [{k: v for k, v in x.items() if k != "per_month"} for x in pr.public(person)["kpis"]]}
    row.computed_by_username = username
    db.add(row)


@router.post("/snapshots/recompute", dependencies=[admin_or_hr, Depends(rate_limited("snapshots", limit=10, window_seconds=3600))])
def recompute(body: RecomputeRequest, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    period = _period(body.period, db)
    ids = _live_ids(db)
    ctx = pr.load_ctx(db, ids, period.start, period.end)
    people = pr.score_many(ctx, ids, period)
    for p in people:
        snapshot_one(db, ctx, p, period, admin.username)
    db.commit()
    log_audit(db, admin.username, "snapshots.recompute", "period", period.key, {"people": len(people)})
    return {"period": period.key, "stored": len(people)}


@router.get("/snapshots", dependencies=[my_access])
def snapshots(profile_id: str | None = Query(default=None, max_length=36), db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    scope = _scope(admin, db)
    pid = profile_id or scope.own_profile_id
    if not pid or not scope.can_see(pid):
        raise _NOT_FOUND
    rows = db.query(ScoreSnapshot).filter_by(profile_id=pid).order_by(ScoreSnapshot.period_end.desc()).limit(60).all()
    return {"snapshots": [{"period_key": r.period_key, "score": float(r.score) if r.score is not None else None, "category_key": r.category_key, "rating": r.rating,
                           "achievement_pct": float(r.achievement_pct) if r.achievement_pct is not None else None, "scale_effective_from": r.scale_effective_from.isoformat() if r.scale_effective_from else None,
                           "computed_at": r.computed_at.isoformat()} for r in rows]}
