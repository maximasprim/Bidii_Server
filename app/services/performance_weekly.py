"""
Phase 6b - the manager's weekly "who needs me" view. Deterministic: it gathers what the earlier phases already know
(scores, early-warning reasons, open suggestions, check-ins, overdue appraisals, figures to verify) into one ranked list
per manager, with a short "recognise" list for people on a strong run. No AI, nothing new is stored.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.admin_user import AdminUser
from app.models.performance import KpiEntry, PerformanceAction, PerformanceUnit, Recommendation, StaffProfile
from app.services import performance_entries as pe
from app.services import performance_insights as ins
from app.services import performance_reports as pr
from app.services.performance_config import get_org_config
from app.services.performance_scope import PerformanceScope

LIVE = ("active", "probation", "pip")
SEV = {"urgent": 3, "action": 2, "info": 1}
MAX_ROWS = 60
TRACKED = {"coaching", "pip", "pip_extended", "probation_extend", "verbal_warning", "first_written_warning", "final_warning", "talk"}


def team_ids(db: Session, scope: PerformanceScope) -> list[str]:
    """The manager's own reporting line plus the people in units they manage; admin/HR without a profile get everyone they can see."""
    own = scope.own_profile_id
    pool: set[str] = set()
    if own is not None:
        pool |= set(pr.reporting_line(db, own))
        units = set(scope.managed_unit_ids)
        if units:
            pool |= {i for (i,) in db.query(StaffProfile.id).filter(StaffProfile.unit_id.in_(units))}
        pool.discard(own)
    if own is None or scope.is_admin or scope.is_hr:
        pool |= {i for (i,) in db.query(StaffProfile.id)} if own is None else set()
    live = {i for (i,) in db.query(StaffProfile.id).filter(StaffProfile.id.in_(pool or {""}), StaffProfile.status.in_(LIVE), StaffProfile.is_active.is_(True), StaffProfile.is_executive.is_(False))}
    return [i for i in live if scope.visible_ids is None or i in scope.visible_ids]


def build(db: Session, scope: PerformanceScope, today: date) -> dict[str, Any]:
    ids = team_ids(db, scope)
    fy = int(get_org_config(db)[0].get("fiscal_year_start_month", 1))
    period = pr.resolve_period("ytd", today, fy)
    ctx = pr.load_ctx(db, ids, period.start, period.end, today=today)
    people = {p["profile_id"]: p for p in pr.score_many(ctx, ids, period)}
    prev = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    reasons: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def add(pid: str, kind: str, severity: str, text: str) -> None:
        reasons[pid].append({"kind": kind, "severity": severity, "text": text})

    # performance reasons
    for pid, p in people.items():
        prof = ctx.profiles[pid]
        cfg = ctx.cfg.get(prof.performance_role_id) or {}
        need = int((cfg.get("early_warning") or {}).get("consecutive_months_below", 2))
        for text in pr.attention_reasons(p, cfg, scope.can_see_restricted(prof)):
            if text.startswith("Figures waiting"):
                continue
            m = text.split(" for ")[-1] if " months running" in text else ""
            add(pid, "performance", "urgent" if m and int(m.split()[0]) > need else "action", text)
    # suggestions I may see
    recs = db.query(Recommendation).filter(Recommendation.entity_type == "staff", Recommendation.entity_id.in_(ids or [""]), Recommendation.status.in_(ins.OPEN)).all()
    positives: set[str] = set()
    for r in recs:
        prof = ctx.profiles.get(r.entity_id)
        if prof is None or not ins.can_view(scope, r, prof):
            continue
        if r.rule_key == "sustained_above":
            positives.add(r.entity_id)
            continue
        add(r.entity_id, "suggestion", r.severity if r.severity in SEV else "action", r.title)
    # check-ins due within a week, or overdue
    horizon = today + timedelta(days=7)
    for a in db.query(PerformanceAction).filter(PerformanceAction.profile_id.in_(ids or [""]), PerformanceAction.outcome.is_(None), PerformanceAction.action_type.in_(TRACKED),
                                                PerformanceAction.review_on.is_not(None), PerformanceAction.review_on <= horizon):
        prof = ctx.profiles[a.profile_id]
        if a.restricted and not scope.can_see_restricted(prof):
            continue
        late = a.review_on < today
        add(a.profile_id, "check_in", "urgent" if late else "action", f"Check-in {'overdue since' if late else 'due'} {a.review_on:%d %b}")
    # appraisals overdue
    limit = int(get_org_config(db)[0].get("appraisal_window_days", 30))
    for pid, days in ins._overdue_days(db, ids, today, limit).items():  # noqa: SLF001
        add(pid, "appraisal", "action", f"Appraisal overdue by {days} days")
    # no results at all last month, and figures waiting for me
    reported = {pid for (pid,) in db.query(KpiEntry.profile_id).filter(KpiEntry.month == prev, KpiEntry.profile_id.in_(ids or [""]))}
    for pid in ids:
        prof = ctx.profiles[pid]
        if pid not in reported and prof.hire_date and prof.hire_date < prev:
            add(pid, "data", "action", f"No results entered for {prev:%B}")
    vunits = pe.verifier_unit_ids(db, scope)
    waiting: dict[str, int] = defaultdict(int)
    for pid, in db.query(KpiEntry.profile_id).filter(KpiEntry.status == "submitted", KpiEntry.profile_id.in_(ids or [""])):
        if pe.can_verify(scope, ctx.profiles[pid], vunits):
            waiting[pid] += 1

    rows = []
    for pid, rs in reasons.items():
        p = people.get(pid)
        if p is None:
            continue
        rows.append({"profile_id": pid, "name": p["name"], "role": p["role_name"], "unit": p["unit_name"], "score": p["score"], "category": (p["category"] or {}).get("label") if isinstance(p["category"], dict) else p["category"],
                     "trend": [t["pct"] for t in p["trend"]][-4:], "reasons": sorted(rs, key=lambda r: -SEV[r["severity"]]), "priority": sum(SEV[r["severity"]] for r in rs)})
    rows.sort(key=lambda r: (-r["priority"], r["name"] or ""))
    recognise = [{"profile_id": pid, "name": people[pid]["name"], "score": people[pid]["score"]} for pid in positives if pid in people]
    return {
        "week_of": (today - timedelta(days=today.weekday())).isoformat(), "team_size": len(ids), "needing_attention": len(rows), "shown": min(len(rows), MAX_ROWS),
        "people": rows[:MAX_ROWS], "recognise": sorted(recognise, key=lambda r: -(r["score"] or 0)),
        "to_verify": [{"profile_id": pid, "name": people[pid]["name"] if pid in people else ctx.profiles[pid].full_name, "figures": n} for pid, n in sorted(waiting.items(), key=lambda kv: -kv[1])],
        "note": "Suggestions for conversations, built from verified figures and open items. They don't change any rating.",
    }


def weekly_counts(db: Session, admin: AdminUser, scope: PerformanceScope, today: date) -> dict[str, int]:
    """Counts only, for the weekly message (no names)."""
    v = build(db, scope, today)
    return {"attention": v["needing_attention"], "verify": sum(x["figures"] for x in v["to_verify"]), "urgent": sum(1 for r in v["people"] if any(x["severity"] == "urgent" for x in r["reasons"]))}
