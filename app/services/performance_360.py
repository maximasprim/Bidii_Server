"""
Phase 6c - 360 feedback for branch managers and head office. Developmental only: nothing here feeds a score, rating or PIP.
Anonymity rules: peers, direct reports and others are shown only as a group and only when the group has at least `min_group`
responses; their comments are shuffled and unattributed. The self and manager views are shown as such (a single manager can't be anonymous).
Raters see only their own assignments, never who else was asked or what anyone said.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from sqlalchemy.orm import Session

from app.models.performance import Feedback360Cycle, Feedback360Rater, Feedback360Subject, StaffProfile
from app.services.performance_role_templates import DEFAULT_COMPETENCIES

QUESTIONS = [{"key": c["key"], "title": c["title"], "description": c["description"]} for c in DEFAULT_COMPETENCIES]
KEYS = {q["key"] for q in QUESTIONS}
RELATIONSHIPS = ("self", "manager", "peer", "direct_report", "other")
ANON_GROUPS = ("peer", "direct_report", "other")
MAX_RATERS = 12
LIVE = ("active", "probation", "pip")


class F360Error(ValueError):
    pass


def suggest_raters(db: Session, subject: StaffProfile, limit: int = 8) -> list[tuple[str, str]]:
    """self, the manager, direct reports, then peers (same manager). (profile_id, relationship)"""
    out: list[tuple[str, str]] = [(subject.id, "self")]
    if subject.manager_id:
        out.append((subject.manager_id, "manager"))
    reports = [i for (i,) in db.query(StaffProfile.id).filter(StaffProfile.manager_id == subject.id, StaffProfile.status.in_(LIVE), StaffProfile.is_active.is_(True)).order_by(StaffProfile.full_name)]
    out += [(i, "direct_report") for i in reports[:limit]]
    if subject.manager_id:
        peers = [i for (i,) in db.query(StaffProfile.id).filter(StaffProfile.manager_id == subject.manager_id, StaffProfile.id != subject.id, StaffProfile.status.in_(LIVE), StaffProfile.is_active.is_(True)).order_by(StaffProfile.full_name)]
        out += [(i, "peer") for i in peers[:limit]]
    return out


def add_subject(db: Session, cycle: Feedback360Cycle, subject: StaffProfile, raters: list[tuple[str, str]] | None) -> Feedback360Subject:
    if cycle.status not in ("draft", "open"):
        raise F360Error("Subjects can only be added while a cycle is in draft or open.")
    if db.query(Feedback360Subject).filter_by(cycle_id=cycle.id, profile_id=subject.id).first():
        raise F360Error("That person is already in this cycle.")
    chosen = raters if raters is not None else suggest_raters(db, subject)
    seen: set[str] = set()
    clean = []
    for rid, rel in chosen:
        if rel not in RELATIONSHIPS:
            raise F360Error("Unknown relationship.")
        if rid in seen or (rel == "self") != (rid == subject.id):
            raise F360Error("Each rater once; only the person themselves is 'self'.")
        seen.add(rid)
        clean.append((rid, rel))
    if len(clean) > MAX_RATERS + 1:
        raise F360Error(f"At most {MAX_RATERS} raters.")
    if sum(1 for _r, rel in clean if rel in ANON_GROUPS) < cycle.min_group:
        raise F360Error(f"Ask at least {cycle.min_group} peers, direct reports or others so that answers can stay anonymous.")
    for rid, _rel in clean:
        p = db.get(StaffProfile, rid)
        if p is None or p.admin_user_id is None or not p.is_active:
            raise F360Error("Every rater needs an active login so they can answer.")
    sub = Feedback360Subject(cycle_id=cycle.id, profile_id=subject.id)
    db.add(sub)
    db.flush()
    for rid, rel in clean:
        db.add(Feedback360Rater(subject_id=sub.id, rater_profile_id=rid, relationship=rel))
    db.commit()
    return sub


def transition(db: Session, cycle: Feedback360Cycle, to: str) -> None:
    order = {"draft": "open", "open": "closed", "closed": "released"}
    if order.get(cycle.status) != to:
        raise F360Error(f"A {cycle.status} cycle can't move to {to}.")
    if to == "open" and db.query(Feedback360Subject).filter_by(cycle_id=cycle.id).count() == 0:
        raise F360Error("Add at least one person before opening.")
    cycle.status = to
    if to == "released":
        cycle.released_at = datetime.now(timezone.utc)
    db.commit()


def validate_answers(answers: dict[str, Any]) -> dict[str, int]:
    if not isinstance(answers, dict) or set(answers) - KEYS:
        raise F360Error("Unknown question.")
    out = {}
    for k, v in answers.items():
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 5:
            raise F360Error("Ratings are whole numbers from 1 to 5.")
        out[k] = v
    return out


def submit(db: Session, rater: Feedback360Rater, cycle: Feedback360Cycle, answers: dict[str, Any], strengths: str | None, development: str | None) -> None:
    if cycle.status != "open":
        raise F360Error("This feedback round isn't open.")
    clean = validate_answers(answers)
    if set(clean) != KEYS:
        raise F360Error("Please rate every competency.")
    rater.answers, rater.strengths, rater.development = clean, (strengths or "").strip()[:1000] or None, (development or "").strip()[:1000] or None
    rater.status, rater.submitted_at = "submitted", datetime.now(timezone.utc)
    db.commit()


def progress(db: Session, cycle: Feedback360Cycle) -> list[dict[str, Any]]:
    """Counts only: HR sees who has answered, never what they said."""
    out = []
    for s in db.query(Feedback360Subject).filter_by(cycle_id=cycle.id):
        rs = db.query(Feedback360Rater).filter_by(subject_id=s.id).all()
        p = db.get(StaffProfile, s.profile_id)
        out.append({"subject_id": s.id, "profile_id": s.profile_id, "name": p.full_name if p else None, "asked": len(rs), "submitted": sum(r.status == "submitted" for r in rs),
                    "declined": sum(r.status == "declined" for r in rs), "pending_raters": [db.get(StaffProfile, r.rater_profile_id).full_name for r in rs if r.status == "pending"]})
    return out


def _avg(vals: list[dict[str, int]]) -> dict[str, float]:
    return {k: round(mean(v[k] for v in vals), 2) for k in KEYS if vals}


def _shuffled(texts: list[str], salt: str) -> list[str]:
    return sorted(texts, key=lambda t: hashlib.sha256((salt + t).encode()).hexdigest())


def results(db: Session, cycle: Feedback360Cycle, subject: Feedback360Subject) -> dict[str, Any]:
    rs = [r for r in db.query(Feedback360Rater).filter_by(subject_id=subject.id, status="submitted")]
    by: dict[str, list[Feedback360Rater]] = defaultdict(list)
    for r in rs:
        by[r.relationship].append(r)
    groups: dict[str, Any] = {}
    for rel in RELATIONSHIPS:
        g = by.get(rel, [])
        if not g:
            continue
        if rel in ANON_GROUPS and len(g) < cycle.min_group:
            groups[rel] = {"shown": False, "responses": len(g), "message": f"Fewer than {cycle.min_group} responses, so they are not shown separately."}
            continue
        entry: dict[str, Any] = {"shown": True, "responses": len(g), "averages": _avg([r.answers or {} for r in g])}
        if rel in ANON_GROUPS:
            entry["strengths"] = _shuffled([r.strengths for r in g if r.strengths], subject.id + rel)
            entry["development"] = _shuffled([r.development for r in g if r.development], subject.id + rel + "d")
        else:
            entry["strengths"] = [r.strengths for r in g if r.strengths]
            entry["development"] = [r.development for r in g if r.development]
        groups[rel] = entry
    others = [r for rel in ANON_GROUPS for r in by.get(rel, [])]
    overall = _avg([r.answers or {} for r in others]) if len(others) >= cycle.min_group else None
    me, mgr = groups.get("self"), groups.get("manager")
    gaps = None
    if me and overall:
        gaps = {k: round(me["averages"][k] - overall[k], 2) for k in KEYS}
    return {"questions": QUESTIONS, "groups": groups, "overall_others": overall, "self_vs_others": gaps, "manager_shown_individually": bool(mgr),
            "note": "Development feedback only. It is not used in scores, ratings or improvement plans."}
