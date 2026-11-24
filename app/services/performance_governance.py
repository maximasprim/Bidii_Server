"""
Phase 4b-1 - data governance: period locks, the staff data-use notice, retention and anonymisation, and the access log.
Nothing here deletes scores: anonymising a leaver removes who they are and their free text, and keeps the numbers as statistics.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.performance import (
    Appraisal, AppraisalDispute, AppraisalItem, AppraisalObjective, AppraisalSignoff, DevelopmentNeed, KpiEntry, NoticeAck, PeriodLock,
    PerformanceAction, PerformanceAuditLog, PerformancePlan, Recommendation, StaffAssignmentHistory, StaffProfile,
)
from app.services import performance_storage as storage
from app.services.performance_config import get_org_config
from app.services.performance_periods import months_in, parse_period

NOTICE_VERSION = "2026-1"
NOTICE_TEXT = (
    "Bidii Credit records your work results, appraisals and related actions in this system so that your performance can be measured fairly, "
    "reviewed with you, and supported with coaching and training. Your manager, the managers above them and HR can see your results; "
    "warnings and improvement plans are visible only to HR, your direct manager and you. Figures sent to an AI service for suggestions are "
    "anonymised first and the AI never decides a score or a rating. Records are kept for the retention period after you leave, then can be "
    "anonymised. You can see who has viewed your report and you can ask HR to correct a mistake or to raise a dispute on an appraisal."
)
ANONYMISE_PHRASE = "ANONYMISE"
FORMER = "Former staff #"


class GovernanceError(ValueError):
    pass


# ---- period locks ------------------------------------------------------------------------------------------------------


def is_locked(db: Session, month: date) -> bool:
    row = db.query(PeriodLock).filter(PeriodLock.month == month.replace(day=1)).first()
    return row is not None and row.status == "locked"


def assert_month_open(db: Session, month: date) -> None:
    from app.services.performance_entries import EntryError  # local import: entries imports this module

    if is_locked(db, month):
        raise EntryError(f"{month:%B %Y} is locked. HR can reopen it with a reason.")


def _months(period_key: str, today: date) -> list[date]:
    try:
        p = parse_period(period_key)
    except Exception as exc:  # PeriodError and friends
        raise GovernanceError(str(exc)) from exc
    ms = [m for m in months_in(p)]
    if any(m.replace(day=1) >= today.replace(day=1) for m in ms):
        raise GovernanceError("Only finished months can be locked.")
    return [m.replace(day=1) for m in ms]


def lock(db: Session, period_key: str, username: str, today: date) -> list[str]:
    done = []
    now = datetime.now(timezone.utc)
    for m in _months(period_key, today):
        row = db.query(PeriodLock).filter(PeriodLock.month == m).first()
        if row is None:
            db.add(PeriodLock(month=m, status="locked", locked_by_username=username, locked_at=now))
        elif row.status != "locked":
            row.status, row.locked_by_username, row.locked_at = "locked", username, now
        else:
            continue
        done.append(m.strftime("%Y-%m"))
    db.commit()
    return done


def reopen(db: Session, period_key: str, username: str, reason: str, today: date) -> list[str]:
    why = (reason or "").strip()
    if len(why) < 5:
        raise GovernanceError("Please give a reason (at least a few words) for reopening.")
    try:
        p = parse_period(period_key)
    except Exception as exc:
        raise GovernanceError(str(exc)) from exc
    done = []
    now = datetime.now(timezone.utc)
    for m in (x.replace(day=1) for x in months_in(p)):
        row = db.query(PeriodLock).filter(PeriodLock.month == m, PeriodLock.status == "locked").first()
        if row is not None:
            row.status, row.reopened_by_username, row.reopened_at, row.reopen_reason = "open", username, now, why[:300]
            done.append(m.strftime("%Y-%m"))
    db.commit()
    return done


def lock_list(db: Session) -> list[dict[str, Any]]:
    return [
        {"month": r.month.strftime("%Y-%m"), "status": r.status, "locked_by": r.locked_by_username, "locked_at": r.locked_at.isoformat() if r.locked_at else None,
         "reopened_by": r.reopened_by_username, "reopen_reason": r.reopen_reason}
        for r in db.query(PeriodLock).order_by(PeriodLock.month.desc())
    ]


# ---- notice ------------------------------------------------------------------------------------------------------------


def notice_state(db: Session, admin_user_id: str) -> dict[str, Any]:
    ack = db.query(NoticeAck).filter_by(admin_user_id=admin_user_id, version=NOTICE_VERSION).first()
    return {"version": NOTICE_VERSION, "text": NOTICE_TEXT, "acknowledged": ack is not None, "acknowledged_at": ack.acknowledged_at.isoformat() if ack else None}


def acknowledge_notice(db: Session, admin_user_id: str) -> None:
    if db.query(NoticeAck).filter_by(admin_user_id=admin_user_id, version=NOTICE_VERSION).first() is None:
        db.add(NoticeAck(admin_user_id=admin_user_id, version=NOTICE_VERSION))
        db.commit()


def notice_outstanding(db: Session) -> list[dict[str, Any]]:
    """Active staff with a login who haven't acknowledged the current notice."""
    done = {a for (a,) in db.query(NoticeAck.admin_user_id).filter_by(version=NOTICE_VERSION)}
    rows = db.query(StaffProfile).filter(StaffProfile.admin_user_id.is_not(None), StaffProfile.is_active.is_(True), StaffProfile.status != "exited")
    return [{"profile_id": p.id, "name": p.full_name} for p in rows if p.admin_user_id not in done]


# ---- retention -----------------------------------------------------------------------------------------------------------


def is_anonymised(p: StaffProfile) -> bool:
    return (p.full_name or "").startswith(FORMER)


def _cutoff(db: Session, today: date) -> date:
    years = int(get_org_config(db)[0].get("retention_years", 7))
    try:
        return today.replace(year=today.year - years)
    except ValueError:  # 29 Feb
        return today.replace(year=today.year - years, day=28)


def retention_candidates(db: Session, today: date) -> dict[str, Any]:
    cutoff = _cutoff(db, today)
    rows = db.query(StaffProfile).filter(StaffProfile.exit_date.is_not(None), StaffProfile.exit_date <= cutoff).order_by(StaffProfile.exit_date).all()
    return {"retention_years": int(get_org_config(db)[0].get("retention_years", 7)), "cutoff": cutoff.isoformat(),
            "items": [{"profile_id": p.id, "name": p.full_name, "exit_date": p.exit_date.isoformat()} for p in rows if not is_anonymised(p)]}


def anonymise(db: Session, profile: StaffProfile, confirm: str, today: date) -> dict[str, int]:
    if confirm != ANONYMISE_PHRASE:
        raise GovernanceError(f"Type {ANONYMISE_PHRASE} to confirm.")
    if is_anonymised(profile):
        raise GovernanceError("That record is already anonymised.")
    if profile.exit_date is None or profile.exit_date > _cutoff(db, today):
        raise GovernanceError("The retention period for this person hasn't ended.")
    counts = {"files": 0, "text_fields": 0}
    appraisals = db.query(Appraisal).filter(Appraisal.profile_id == profile.id).all()
    aids = [a.id for a in appraisals]
    for a in appraisals:
        if a.uploaded_file_path:
            storage.remove(a.uploaded_file_path)
            counts["files"] += 1
        a.uploaded_file_path = a.uploaded_original_name = a.extraction = None
        if a.extraction_status != "none":
            a.extraction_status = "none"
    if aids:
        for i in db.query(AppraisalItem).filter(AppraisalItem.appraisal_id.in_(aids)):
            i.self_text = None
            counts["text_fields"] += 1
        for d in db.query(DevelopmentNeed).filter(DevelopmentNeed.appraisal_id.in_(aids)):
            d.text = None
            counts["text_fields"] += 1
        for o in db.query(AppraisalObjective).filter(AppraisalObjective.appraisal_id.in_(aids)):
            o.values = []
        for s in db.query(AppraisalSignoff).filter(AppraisalSignoff.appraisal_id.in_(aids)):
            s.comment = None
            counts["text_fields"] += 1
        for d in db.query(AppraisalDispute).filter(AppraisalDisputeAlias.appraisal_id.in_(aids)):
            d.reason, d.resolution = "[removed]", None
            counts["text_fields"] += 1
    for e in db.query(KpiEntry).filter(KpiEntry.profile_id == profile.id):
        if e.evidence_path:
            storage.remove(e.evidence_path)
            counts["files"] += 1
        e.evidence_path = e.evidence_name = e.note = e.rejection_reason = None
    for act in db.query(PerformanceAction).filter(PerformanceAction.profile_id == profile.id):
        act.note, act.outcome = None, None
        counts["text_fields"] += 1
    for r in db.query(Recommendation).filter(Recommendation.entity_type == "staff", Recommendation.entity_id == profile.id):
        r.content = {"detail": "[removed]"}
        r.status_note = None
    for pl in db.query(PerformancePlan).filter(PerformancePlan.profile_id == profile.id):  # the goal and path are numbers; the manager's notes are text
        pl.note = pl.close_note = None
        counts["text_fields"] += 1
    db.query(StaffAssignmentHistory).filter(StaffAssignmentHistory.profile_id == profile.id).update({"reason": None})
    profile.full_name = f"{FORMER}{profile.id[:6]}"
    profile.employee_no = None
    profile.admin_user_id = None
    profile.is_active = False
    db.commit()
    return counts


# ---- who viewed ------------------------------------------------------------------------------------------------------------

VIEW_ACTIONS = ("report.view", "appraisal_viewed", "appraisal_downloaded", "appraisal_file_downloaded")


def access_log(db: Session, profile_id: str, limit: int = 200) -> list[dict[str, Any]]:
    aids = [a for (a,) in db.query(Appraisal.id).filter(Appraisal.profile_id == profile_id)]
    ids = [profile_id, *aids]
    rows = (db.query(PerformanceAuditLog).filter(PerformanceAuditLog.action.in_(VIEW_ACTIONS), PerformanceAuditLog.entity_id.in_(ids))
            .order_by(PerformanceAuditLog.created_at.desc()).limit(limit).all())
    return [{"viewer": r.actor_username, "action": r.action, "at": r.created_at.isoformat() if r.created_at else None} for r in rows]
