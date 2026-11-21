"""
Database side of Phase 4a: per-person projections, rule facts, and generating / listing recommendations.
The maths lives in performance_projection / performance_rules (pure); this module only gathers inputs in a fixed number of queries.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.performance import Appraisal, PerformanceAction, Recommendation, StaffProfile
from app.services import performance_projection as pj
from app.services import performance_reports as pr
from app.services import performance_rules as rules
from app.services.performance_periods import Period, month_end, months_in, parse_period
from app.services.performance_scoring import ramp_target
from app.services.performance_targets import first_of_month, pick_in_force

OPEN = ("new", "acknowledged")


def _year_end_period(today: date, fiscal_start: int) -> Period:
    """The fiscal year containing today, as a custom period (start of year to its last day)."""
    start = pr.resolve_period("ytd", today, fiscal_start).start
    idx = start.year * 12 + start.month - 1 + 11
    end = month_end(date(idx // 12, idx % 12 + 1, 1))
    return Period(f"{start:%Y-%m}..{end:%Y-%m}", "custom", start, end, f"{start:%b %Y} to {end:%b %Y}")


def _quarter_period(today: date) -> Period:
    return parse_period(f"{today.year}-Q{(today.month - 1) // 3 + 1}")


def future_target(ctx: pr.Ctx, person_id: str, kpi_key: str, month: date) -> float | None:
    prof = ctx.profiles[person_id]
    row = pick_in_force(ctx.targets.get((person_id, kpi_key), []), month)
    if row is None:
        return None
    cfg = ctx.cfg.get(prof.performance_role_id) or {}
    t = ramp_target(Decimal(row.value), prof.hire_date, month, int(cfg.get("ramp_up_months", 0)))
    return float(t) if t is not None and t > 0 else None


def project_person(ctx: pr.Ctx, person: dict[str, Any], fiscal_start: int = 1) -> dict[str, Any]:
    """Quarter-end and year-end projections, the run-rate needed for Meeting Expectation, and category chances (ranges)."""
    head = person["headline"]
    if head is None:
        return {"available": False, "reason": "No sales-type KPI to project for this role."}
    history_rows = [(t["actual"], t["target"], t["month"]) for t in person["trend"] if t["state"] == "scored" and t["actual"] is not None and t["target"]]
    history = [(a, t) for a, t, _m in history_rows]
    last = max((m for _a, _t, m in history_rows), default=None)
    cats = ctx.scale.categories
    meeting = next((float(c["min_score"]) for c in cats if c["key"] == "meeting_expectation"), 90.0)
    out: dict[str, Any] = {"available": bool(history), "headline": head["label"], "data_window_months": len(history), "horizons": {}}
    if not history:
        return {**out, "reason": "Insufficient history for a projection (no scored months yet)."}
    for name, period in (("quarter_end", _quarter_period(ctx.today)), ("year_end", _year_end_period(ctx.today, fiscal_start))):
        remaining: list[float] = []
        for m in months_in(period):
            if last is not None and m.strftime("%Y-%m") <= last:
                continue
            tgt = future_target(ctx, person["profile_id"], head["key"], m)
            if tgt is not None:
                remaining.append(tgt)
        in_period = [(a, t) for a, t, m in history_rows if period.start <= date.fromisoformat(m + "-01") <= period.end]
        base = (sum(a for a, _ in in_period), sum(t for _, t in in_period))
        out["horizons"][name] = {
            "period": {"label": period.label, "start": period.start.isoformat(), "end": period.end.isoformat()},
            "projection": pj.project_period(history, remaining, base=base),
            "required_run_rate": pj.required_run_rate(history, remaining, meeting, base=base),
            "category_chances": pj.category_probabilities(history, remaining, cats, base=base),
        }
    return out


def project_group(projections: list[dict[str, Any]], horizon: str = "year_end") -> dict[str, Any]:
    return pj.project_group([(p.get("horizons", {}).get(horizon) or {}).get("projection", {"available": False}) for p in projections])


# ---- rule facts --------------------------------------------------------------------------------------------------------


def _actions_by_profile(db: Session, ids: list[str]) -> dict[str, list[PerformanceAction]]:
    out: dict[str, list[PerformanceAction]] = defaultdict(list)
    for a in db.query(PerformanceAction).filter(PerformanceAction.profile_id.in_(ids)).order_by(PerformanceAction.started_on):
        out[a.profile_id].append(a)
    return out


def _overdue_days(db: Session, ids: list[str], today: date, limit_days: int) -> dict[str, int]:
    """Days past due for the last COMPLETED quarter's appraisal (due the limit after quarter end), for people who have none finalised."""
    q = (today.month - 1) // 3
    y = today.year if q else today.year - 1
    q = q or 4
    qkey = f"{y}-Q{q}"
    qend = parse_period(qkey).end
    done = {pid for (pid,) in db.query(Appraisal.profile_id).filter(Appraisal.profile_id.in_(ids), Appraisal.period_key.in_([qkey, str(y)]), Appraisal.status == "finalised")}
    due = qend + timedelta(days=limit_days)
    late = (today - due).days
    return {pid: late for pid in ids if pid not in done and late > 0}


def build_facts(ctx: pr.Ctx, person: dict[str, Any], actions: list[PerformanceAction], overdue: int | None) -> rules.Facts:
    prof = ctx.profiles[person["profile_id"]]
    cfg = ctx.cfg.get(prof.performance_role_id) or {}
    quality = next((k for k in person["kpis"] if k["source"] == "kpi_entry" and k["direction"] == "lower" and k["unit"] == "percent"), None)
    q_series: list[float | None] = []
    if quality and quality.get("per_month"):
        q_series = [v["actual"] if v["state"] == "scored" else None for _m, v in sorted(quality["per_month"].items())]
    pip = None
    for a in actions:
        if a.action_type in ("pip", "pip_extended"):
            pip = a
        elif a.action_type == "pip_lifted":
            pip = None
    decided = any(a.action_type in ("probation_confirm", "probation_extend") for a in actions)
    return {
        "status": prof.status, "pcts": [t["pct"] for t in person["trend"]], "sales": [t["actual"] if t["state"] == "scored" else None for t in person["trend"]],
        "quality": q_series, "probation_end": prof.probation_end_date, "probation_decided": decided, "appraisal_overdue_days": overdue,
        "pip_ends_on": pip.ends_on if pip else None, "pip_review_on": pip.review_on if pip else None, "today": ctx.today, "early_warning": cfg.get("early_warning", {}),
    }


# ---- generating and storing -----------------------------------------------------------------------------------------------


def generate(db: Session, profile_ids: list[str], *, unit_ids: list[str] | None = None, today: date | None = None) -> dict[str, int]:
    """Runs the rules for `profile_ids` (and branch rules for `unit_ids`) and stores new suggestions. Idempotent within a month."""
    ctx_period = pr.resolve_period("ytd", today)
    ctx = pr.load_ctx(db, profile_ids, ctx_period.start, ctx_period.end, today=today)
    people = pr.score_many(ctx, profile_ids, ctx_period)
    actions = _actions_by_profile(db, profile_ids) if profile_ids else {}
    limit = 30
    overdue = _overdue_days(db, profile_ids, ctx.today, limit) if profile_ids else {}
    key = f"{ctx.today:%Y-%m}"
    created = updated = 0
    existing = {(r.entity_type, r.entity_id, r.rule_key): r for r in db.query(Recommendation).filter(Recommendation.period_key == key)}
    open_rows = defaultdict(set)
    for r in db.query(Recommendation).filter(Recommendation.status.in_(OPEN), Recommendation.period_key != key):
        open_rows[(r.entity_type, r.entity_id)].add(r.rule_key)

    def upsert(entity_type: str, entity_id: str, rec: dict[str, Any], baseline: dict[str, Any]) -> None:
        nonlocal created, updated
        row = existing.get((entity_type, entity_id, rec["rule_key"]))
        content = {"detail": rec["detail"], "figures": rec["figures"]}
        if row is None:
            db.add(Recommendation(entity_type=entity_type, entity_id=entity_id, period_key=key, rule_key=rec["rule_key"], severity=rec["severity"], restricted=rec["restricted"],
                                  audience=rec["audience"], title=rec["title"], content=content, baseline=baseline))
            created += 1
        elif row.status in OPEN:
            row.title, row.severity, row.content = rec["title"], rec["severity"], {**row.content, **content}
            updated += 1

    for person in people:
        facts = build_facts(ctx, person, actions.get(person["profile_id"], []), overdue.get(person["profile_id"]))
        for rec in rules.person_recommendations(facts):
            upsert("staff", person["profile_id"], rec, {"score": person["score"], "headline_pct": (person["headline"] or {}).get("pct")})
    if unit_ids:
        by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for p in people:
            if p["unit_id"]:
                by_unit[p["unit_id"]].append(p)
        for uid in unit_ids:
            members = by_unit.get(uid, [])
            g = pr.group_summary(ctx, members)
            for rec in rules.unit_recommendations(g["category_counts"], g["scored_people"], ctx.units[uid].name if uid in ctx.units else "This branch"):
                upsert("unit", uid, rec, {"average_score": g["average_score"], "achievement_pct": g["achievement_pct"]})
    # follow-up: an actioned suggestion more than 30 days old gets its outcome recorded once
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    now_score = {p["profile_id"]: p for p in people}
    for r in db.query(Recommendation).filter(Recommendation.status == "actioned", Recommendation.outcome.is_(None), Recommendation.entity_type == "staff", Recommendation.acted_at <= cutoff, Recommendation.entity_id.in_(profile_ids or [""])):
        p = now_score.get(r.entity_id)
        before = (r.baseline or {}).get("score")
        if p is not None and p["score"] is not None and before is not None:
            r.outcome = {"score_then": before, "score_now": p["score"], "improved": p["score"] > before, "checked_on": ctx.today.isoformat()}
    db.commit()
    return {"people": len(people), "created": created, "updated": updated}


def can_view(scope, rec: Recommendation, profile: StaffProfile | None) -> bool:
    if rec.entity_type == "staff":
        if profile is None or not scope.can_see(rec.entity_id):
            return False
        if rec.restricted and not scope.can_see_restricted(profile):
            return False
        if profile.id == scope.own_profile_id and "self" not in (rec.audience or []):
            return False
        return True
    return scope.visible_ids is None or rec.entity_id in scope.managed_unit_ids


def to_json(rec: Recommendation, name: str | None) -> dict[str, Any]:
    return {
        "id": rec.id, "entity_type": rec.entity_type, "entity_id": rec.entity_id, "entity_name": name, "period_key": rec.period_key, "rule_key": rec.rule_key,
        "severity": rec.severity, "restricted": rec.restricted, "title": rec.title, "detail": rec.content.get("detail"), "figures": rec.content.get("figures"),
        "advice": rec.content.get("advice"), "status": rec.status, "status_note": rec.status_note, "due_on": rec.due_on.isoformat() if rec.due_on else None,
        "assignee_profile_id": rec.assignee_profile_id, "baseline": rec.baseline, "outcome": rec.outcome, "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }
