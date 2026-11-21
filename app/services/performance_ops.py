"""
Phase 4b-2 - day-to-day operations: reminders and the daily digest, anomalies and data quality, calibration across managers,
the lifecycle timeline, training themes and the units leaderboard. Read-only except the digest, which only creates in-app notifications.
Notifications carry counts and generic wording only - never a PIP, warning or name.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from app.models.admin_user import AdminUser
from app.models.internal_notification import InternalNotification
from app.models.performance import (
    Appraisal, DevelopmentNeed, KpiEntry, PerformanceAction, PerformanceAuditLog, PerformanceKpi, PerformanceTarget, PerformanceUnit, StaffAssignmentHistory, StaffProfile,
)
from app.services import performance_insights as ins
from app.services import performance_reports as pr
from app.services.performance_config import RESTRICTED_ACTION_TYPES, get_org_config
from app.services.performance_periods import month_end

LIVE = ("active", "probation", "pip")
CLOSED_ACTIONS = ("pip_lifted",)


def _prev_month(today: date) -> date:
    first = today.replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


# ---- reminders ---------------------------------------------------------------------------------------------------------


def due_items(db: Session, today: date) -> dict[str, list[dict[str, str]]]:
    """admin_user_id -> [{message, link}]. Counts only."""
    cfg = get_org_config(db)[0]
    window = int(cfg.get("sales_entry_window_days", 10))
    prev = _prev_month(today)
    out: dict[str, list[dict[str, str]]] = defaultdict(list)
    people = {p.id: p for p in db.query(StaffProfile).filter(StaffProfile.status.in_(LIVE), StaffProfile.is_active.is_(True))}

    # 1. my results for last month (not yet submitted), while the window is open
    deadline = month_end(prev) + timedelta(days=window)
    if today <= deadline:
        submitted = {pid for (pid,) in db.query(KpiEntry.profile_id).filter(KpiEntry.month == prev, KpiEntry.status.in_(("submitted", "verified", "locked")))}
        for p in people.values():
            if p.admin_user_id and p.id not in submitted:
                out[p.admin_user_id].append({"message": f"Your results for {prev:%B %Y} are due by {deadline:%d %b}.", "link": "/admin/performance/entries"})

    # 2. figures waiting for a verifier (direct manager)
    waiting: Counter[str] = Counter()
    for pid, in db.query(KpiEntry.profile_id).filter(KpiEntry.status == "submitted"):
        mgr = people.get(pid).manager_id if pid in people else None
        if mgr:
            waiting[mgr] += 1
    for mgr, n in waiting.items():
        u = people.get(mgr)
        if u and u.admin_user_id:
            out[u.admin_user_id].append({"message": f"{n} monthly figure(s) are waiting for you to verify.", "link": "/admin/performance/entries"})

    # 3. overdue appraisals: tell the person and their manager
    limit = int(cfg.get("appraisal_window_days", 30))
    late = ins._overdue_days(db, list(people), today, limit)  # noqa: SLF001
    by_mgr: Counter[str] = Counter()
    for pid in late:
        p = people[pid]
        if p.admin_user_id:
            out[p.admin_user_id].append({"message": "Your appraisal for last quarter is overdue.", "link": "/admin/performance/appraisals"})
        if p.manager_id:
            by_mgr[p.manager_id] += 1
    for mgr, n in by_mgr.items():
        u = people.get(mgr)
        if u and u.admin_user_id:
            out[u.admin_user_id].append({"message": f"{n} appraisal(s) in your team are overdue.", "link": "/admin/performance/appraisals"})

    # 4. check-ins due (generic: no type, no name)
    due_by: Counter[str] = Counter()
    hr_total = 0
    for a in db.query(PerformanceAction).filter(PerformanceAction.review_on.is_not(None), PerformanceAction.review_on <= today, PerformanceAction.outcome.is_(None)):
        p = people.get(a.profile_id)
        if p is None:
            continue
        hr_total += 1
        if p.manager_id:
            due_by[p.manager_id] += 1
    for mgr, n in due_by.items():
        u = people.get(mgr)
        if u and u.admin_user_id:
            out[u.admin_user_id].append({"message": f"{n} check-in(s) are due for review.", "link": "/admin/performance"})
    if hr_total:
        for (uid,) in db.query(AdminUser.id).filter(AdminUser.role == "hr", AdminUser.is_active.is_(True)):
            out[uid].append({"message": f"{hr_total} check-in(s) are due for review across the company.", "link": "/admin/performance"})
    return out


def send_digest(db: Session, today: date) -> dict[str, Any]:
    """In-app notification plus the person's chosen email/SMS, at most one digest per person per day (tracked in the audit log and delivery log)."""
    from app.services import performance_channels as ch

    key = today.isoformat()
    already = {e for (e,) in db.query(PerformanceAuditLog.entity_id).filter(PerformanceAuditLog.action == "digest.sent", PerformanceAuditLog.details["day"].as_string() == key)}
    due = due_items(db, today)
    users = {u.id: u for u in db.query(AdminUser).filter(AdminUser.id.in_(list(due) or [""]), AdminUser.is_active.is_(True))}
    sent = skipped = 0
    channels: dict[str, int] = defaultdict(int)
    for uid, items in due.items():
        if uid in already or uid not in users:
            skipped += 1
            continue
        db.add(InternalNotification(recipient_admin_id=uid, message=" ".join(i["message"] for i in items)[:900], link_path=items[0]["link"]))
        db.add(PerformanceAuditLog(actor_username="system", action="digest.sent", entity_type="admin_user", entity_id=uid, details={"day": key, "items": len(items)}))
        db.commit()
        body = "Hello,\n\nThese need your attention in the Bidii performance system:\n\n" + "\n".join(f"- {i['message']}" for i in items) + "\n\nPlease sign in to take action. This message contains no personal performance details."
        sms = f"Bidii Performance: {len(items)} item(s) need your attention. Please sign in to the system."
        for chn, status in ch.deliver(db, users[uid], kind="daily", day=today, subject="Bidii performance: items need your attention", email_body=body, sms_text=sms).items():
            channels[f"{chn}_{status}"] += 1
        sent += 1
    db.commit()
    return {"sent": sent, "skipped": skipped, "channels": dict(channels)}


def send_weekly(db: Session, today: date) -> dict[str, Any]:
    """Counts-only weekly message to managers (email/SMS per their preferences). One per person per day and kind."""
    from app.services import performance_channels as ch
    from app.services import performance_weekly as weekly
    from app.services.performance_scope import get_performance_scope

    managers = db.query(AdminUser).join(StaffProfile, StaffProfile.admin_user_id == AdminUser.id).filter(AdminUser.is_active.is_(True), StaffProfile.is_active.is_(True)).all()
    manager_profile_ids = {m for (m,) in db.query(StaffProfile.manager_id).filter(StaffProfile.manager_id.is_not(None))} | {u for (u,) in db.query(PerformanceUnit.manager_profile_id).filter(PerformanceUnit.manager_profile_id.is_not(None))}
    sent = skipped = 0
    channels: dict[str, int] = defaultdict(int)
    for user in managers:
        prof = db.query(StaffProfile).filter_by(admin_user_id=user.id).first()
        if prof is None or prof.id not in manager_profile_ids:
            continue
        c = weekly.weekly_counts(db, user, get_performance_scope(user, db), today)
        if c["attention"] == 0 and c["verify"] == 0:
            skipped += 1
            continue
        lines = [f"{c['attention']} team member(s) may need a conversation this week" + (f" ({c['urgent']} urgent)" if c["urgent"] else "") + "."]
        if c["verify"]:
            lines.append(f"{c['verify']} monthly figure(s) are waiting for you to verify.")
        body = "Hello,\n\nYour weekly performance summary:\n\n" + "\n".join(f"- {x}" for x in lines) + "\n\nOpen Staff Performance > Operations > This week for details. This message contains no names or personal performance details."
        sms = "Bidii Performance weekly: " + " ".join(lines)[:200] + " Please sign in."
        for chn, status in ch.deliver(db, user, kind="weekly", day=today, subject="Bidii performance: your week", email_body=body, sms_text=sms).items():
            channels[f"{chn}_{status}"] += 1
        sent += 1
    return {"managers": sent, "skipped": skipped, "channels": dict(channels)}


# ---- anomalies and data quality ---------------------------------------------------------------------------------------


def anomalies(db: Session, ids: list[str]) -> list[dict[str, Any]]:
    """Verified figures far from the person's own median (a typing slip is the usual cause)."""
    out: list[dict[str, Any]] = []
    rows = db.query(KpiEntry).filter(KpiEntry.profile_id.in_(ids or [""]), KpiEntry.status.in_(("verified", "submitted", "locked"))).all()
    series: dict[tuple[str, str], list[KpiEntry]] = defaultdict(list)
    for e in rows:
        series[(e.profile_id, e.kpi_key)].append(e)
    names = {i: n for i, n in db.query(StaffProfile.id, StaffProfile.full_name).filter(StaffProfile.id.in_(ids or [""]))}
    for (pid, key), es in series.items():
        vals = [float(e.value) for e in es]
        if len(vals) < 4:
            continue
        for e in es:
            others = [float(x.value) for x in es if x.id != e.id]
            m = median(others)
            v = float(e.value)
            if m > 0 and (v >= 5 * m or v <= m / 5):
                out.append({"profile_id": pid, "name": names.get(pid), "kpi_key": key, "month": e.month.strftime("%Y-%m"), "value": v, "typical": round(m, 2),
                            "why": f"{v:g} is far from this person's usual {m:g}. Check for a typing slip."})
    return sorted(out, key=lambda r: (r["name"] or "", r["month"]))


def data_quality(db: Session, today: date) -> list[dict[str, Any]]:
    """Setup problems that quietly distort scores. HR only."""
    issues: list[dict[str, Any]] = []

    def add(kind: str, rows: list[tuple[str, str]], text: str) -> None:
        if rows:
            issues.append({"kind": kind, "text": text, "count": len(rows), "items": [{"profile_id": a, "name": b} for a, b in rows[:50]]})

    live = db.query(StaffProfile).filter(StaffProfile.status.in_(LIVE), StaffProfile.is_active.is_(True), StaffProfile.is_executive.is_(False)).all()
    add("no_unit", [(p.id, p.full_name) for p in live if p.unit_id is None], "Active staff with no branch or department.")
    add("no_manager", [(p.id, p.full_name) for p in live if p.manager_id is None], "Active staff with no line manager (sign-offs and verification have nobody to go to).")
    dup: dict[str, list[StaffProfile]] = defaultdict(list)
    for p in live:
        if p.employee_no:
            dup[p.employee_no.strip().lower()].append(p)
    add("duplicate_employee_no", [(p.id, p.full_name) for v in dup.values() if len(v) > 1 for p in v], "Staff sharing an employee number.")
    cfgs = {p.id: p for p in live}
    have_target = {pid for (pid,) in db.query(PerformanceTarget.profile_id).filter(PerformanceTarget.profile_id.in_(list(cfgs) or [""]))}
    add("no_targets", [(p.id, p.full_name) for p in live if p.id not in have_target], "Active staff with no targets set, so nothing they enter can be scored.")
    prev = _prev_month(today)
    reported = {pid for (pid,) in db.query(KpiEntry.profile_id).filter(KpiEntry.month == prev)}
    add("no_results_last_month", [(p.id, p.full_name) for p in live if p.id not in reported and p.hire_date and p.hire_date < prev], f"No results at all for {prev:%B %Y}.")
    add("probation_ended", [(p.id, p.full_name) for p in live if p.status == "probation" and p.probation_end_date and p.probation_end_date < today], "Probation end date passed with no decision recorded on the profile.")
    add("exited_but_active", [(p.id, p.full_name) for p in db.query(StaffProfile).filter(StaffProfile.exit_date.is_not(None), StaffProfile.exit_date < today, StaffProfile.status != "exited")], "Exit date has passed but the person is not marked as exited.")
    return issues


# ---- calibration and leaderboard -------------------------------------------------------------------------------------


def calibration(db: Session, ids: list[str], today: date) -> dict[str, Any]:
    """Average score by line manager next to the company average: surfaces harsh and lenient raters. Teams under 3 are not shown."""
    period = pr.resolve_period("ytd", today)
    ctx = pr.load_ctx(db, ids, period.start, period.end, today=today)
    people = pr.score_many(ctx, ids, period)
    scored = [p for p in people if p["score"] is not None]
    org_avg = round(sum(p["score"] for p in scored) / len(scored), 1) if scored else None
    mgr_of = {p.id: p.manager_id for p in ctx.profiles.values()}
    teams: dict[str, list[float]] = defaultdict(list)
    for p in scored:
        if mgr_of.get(p["profile_id"]):
            teams[mgr_of[p["profile_id"]]].append(p["score"])
    names = {i: n for i, n in db.query(StaffProfile.id, StaffProfile.full_name).filter(StaffProfile.id.in_(list(teams) or [""]))}
    rows = []
    for m, vals in teams.items():
        if len(vals) < 3:
            continue
        avg = round(sum(vals) / len(vals), 1)
        gap = round(avg - org_avg, 1) if org_avg is not None else None
        rows.append({"manager_id": m, "manager": names.get(m), "team_size": len(vals), "average_score": avg, "gap_to_company": gap,
                     "flag": "review" if gap is not None and abs(gap) >= 15 else None})
    return {"period": period.label, "company_average": org_avg, "managers": sorted(rows, key=lambda r: r["average_score"]),
            "note": "A large gap can mean a strong or weak team, or a harsh or lenient rater. It is a prompt for a conversation, not a verdict."}


def units_leaderboard(db: Session, unit_ids: list[str] | None, today: date) -> list[dict[str, Any]]:
    period = pr.resolve_period("ytd", today)
    q = db.query(PerformanceUnit)
    if unit_ids is not None:
        q = q.filter(PerformanceUnit.id.in_(unit_ids or [""]))
    units = q.all()
    members = {u.id: [i for (i,) in db.query(StaffProfile.id).filter(StaffProfile.unit_id == u.id, StaffProfile.status.in_(LIVE))] for u in units}
    all_ids = [i for v in members.values() for i in v]
    ctx = pr.load_ctx(db, all_ids, period.start, period.end, today=today)
    people = {p["profile_id"]: p for p in pr.score_many(ctx, all_ids, period)}
    rows = []
    for u in units:
        g = pr.group_summary(ctx, [people[i] for i in members[u.id] if i in people])
        rows.append({"unit_id": u.id, "name": u.name, "people": len(members[u.id]), "achievement_pct": g["achievement_pct"], "average_score": g["average_score"],
                     "small_unit": len(members[u.id]) < pr.SMALL_UNIT})
    return sorted(rows, key=lambda r: (r["achievement_pct"] is None, -(r["achievement_pct"] or 0)))


# ---- timeline and training themes ---------------------------------------------------------------------------------------


def timeline(db: Session, profile: StaffProfile, show_restricted: bool) -> list[dict[str, Any]]:
    ev: list[dict[str, Any]] = []
    if profile.hire_date:
        ev.append({"date": profile.hire_date.isoformat(), "kind": "hired", "text": "Joined"})
    if profile.probation_end_date:
        ev.append({"date": profile.probation_end_date.isoformat(), "kind": "probation", "text": "Probation end date"})
    for h in db.query(StaffAssignmentHistory).filter(StaffAssignmentHistory.profile_id == profile.id):
        ev.append({"date": h.effective_from.isoformat(), "kind": "assignment", "text": "Unit, manager or role changed"})
    for a in db.query(PerformanceAction).filter(PerformanceAction.profile_id == profile.id):
        if a.restricted and not show_restricted:
            continue
        ev.append({"date": a.started_on.isoformat(), "kind": "action", "text": a.action_type.replace("_", " ").capitalize(), "restricted": a.restricted})
    for a in db.query(Appraisal).filter(Appraisal.profile_id == profile.id, Appraisal.status == "finalised"):
        ev.append({"date": a.finalised_at.date().isoformat() if a.finalised_at else a.period_end.isoformat(), "kind": "appraisal", "text": f"Appraisal {a.period_key} finalised"})
    if profile.exit_date:
        ev.append({"date": profile.exit_date.isoformat(), "kind": "exit", "text": "Left"})
    return sorted(ev, key=lambda e: e["date"], reverse=True)


_WORDS = re.compile(r"[a-z]{3,}")
_STOP = {"and", "the", "for", "with", "skills", "training", "course", "better", "improve", "improved", "need", "needs", "more", "how", "that", "this", "customer"}


def training_themes(db: Session, ids: list[str], min_people: int = 4) -> dict[str, Any]:
    """Most common words in finalised appraisals' development needs, by unit. A unit with fewer than min_people appraised is not shown."""
    prof = {p.id: p for p in db.query(StaffProfile).filter(StaffProfile.id.in_(ids or [""]))}
    rows = (db.query(DevelopmentNeed.text, Appraisal.profile_id).join(Appraisal, Appraisal.id == DevelopmentNeed.appraisal_id)
            .filter(Appraisal.profile_id.in_(ids or [""]), Appraisal.status == "finalised", DevelopmentNeed.text.is_not(None)).all())
    by_unit: dict[str, Counter[str]] = defaultdict(Counter)
    people: dict[str, set[str]] = defaultdict(set)
    overall: Counter[str] = Counter()
    for text, pid in rows:
        unit = prof[pid].unit_id or "none"
        words = {w for w in _WORDS.findall((text or "").lower()) if w not in _STOP}
        by_unit[unit].update(words)
        people[unit].add(pid)
        overall.update(words)
    names = {i: n for i, n in db.query(PerformanceUnit.id, PerformanceUnit.name)}
    units = [{"unit_id": u, "unit": names.get(u, "No unit"), "people": len(people[u]), "themes": [{"word": w, "count": c} for w, c in by_unit[u].most_common(5)]}
             for u in by_unit if len(people[u]) >= min_people]
    return {"overall": [{"word": w, "count": c} for w, c in overall.most_common(10)], "units": units,
            "note": f"Units with fewer than {min_people} appraised people are hidden so individuals can't be identified."}
