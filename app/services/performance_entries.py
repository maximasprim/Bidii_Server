"""
Monthly KPI entries (sales disbursed, new customers, PAR, retention, ...).

* Who enters: the person (if they have a login), or HR / admin / their manager / their branch's manager on
  their behalf (staff without accounts still get tracked).
* Window: a month can be entered until `sales_entry_window_days` after it ends; later only HR/admin may enter
  or amend (a "late entry"). Future months are never accepted.
* Flow: draft -> submitted -> verified | rejected. A submitted or verified entry can't be edited; a rejected
  one can (it goes back to draft). Verification needs someone who is NOT the person and NOT the one who
  entered it (four eyes). Only VERIFIED entries count towards achievement; the rest are "pending".
* The optional "suggest from approved applications" helper only PRE-FILLS a suggestion: the system tracks
  approval, not disbursement, so it is never the source of truth.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.performance import (
    KpiEntry,
    PerformanceKpi,
    PerformanceUnit,
    Region,
    StaffProfile,
    UnitRegionLink,
)
from app.services import performance_governance as gov
from app.services.performance_roles import kpis_for, latest_version, lock_version
from app.services.performance_scope import PerformanceScope
from app.services.performance_scoring import achievement_pct, ramp_target
from app.services.performance_targets import first_of_month, in_force_map
from app.services.performance_time import NAIROBI
from app.services.performance_periods import month_end

SUGGESTABLE_KEYS = {"sales", "disbursement"}
MAX_VALUE = Decimal("1000000000000")
EDITABLE = {"draft", "rejected"}
COUNTED = {"verified", "locked"}


class EntryError(ValueError):
    """A problem the person can fix; the message is safe to show."""


def entry_kpis(db: Session, role_id: str) -> dict[str, PerformanceKpi]:
    """The KPIs a person enters monthly: the current JD version's KPIs whose source is kpi_entry."""
    version = latest_version(db, role_id)
    if version is None:
        return {}
    return {k.key: k for k in kpis_for(db, version.id) if k.source == "kpi_entry"}


def window_state(month: date, window_days: int, today: date) -> dict[str, Any]:
    m = first_of_month(month)
    if m > first_of_month(today):
        return {"state": "future", "closes_on": None}
    closes = month_end(m) + timedelta(days=window_days)
    return {"state": "open" if today <= closes else "closed", "closes_on": closes}


def clean_value(kpi: PerformanceKpi, raw: Any) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str, Decimal)):
        raise EntryError("Enter a number.")
    try:
        d = Decimal(str(raw))
    except Exception:
        raise EntryError("Enter a number.") from None
    if not d.is_finite() or d < 0 or d > MAX_VALUE:
        raise EntryError("Enter a number from 0 upwards.")
    if kpi.unit == "count" and d != d.to_integral_value():
        raise EntryError(f'"{kpi.label}" is a count, so it must be a whole number.')
    if kpi.unit == "percent" and d > 100:
        raise EntryError(f'"{kpi.label}" is a percentage and can\'t be more than 100.')
    return d.quantize(Decimal("0.0001"))


def verifier_unit_ids(db: Session, scope: PerformanceScope) -> set[str]:
    """Units whose figures this person verifies as a MANAGER: the units they manage, and units in regions they manage.
    (A branch_office_admin's account-level branch link deliberately does not count: clerks don't verify.)"""
    own = scope.own_profile_id
    if own is None:
        return set()
    ids = {u for (u,) in db.query(PerformanceUnit.id).filter(PerformanceUnit.manager_profile_id == own)}
    region_ids = {r for (r,) in db.query(Region.id).filter(Region.manager_profile_id == own)}
    if region_ids:
        today = datetime.now(NAIROBI).date()
        ids |= {
            u for (u,) in db.query(UnitRegionLink.unit_id).filter(
                UnitRegionLink.region_id.in_(region_ids), UnitRegionLink.effective_from <= today,
                (UnitRegionLink.effective_to.is_(None)) | (UnitRegionLink.effective_to >= today),
            )
        }
    return ids


def can_enter_for(scope: PerformanceScope, profile: StaffProfile, verifier_units: set[str]) -> bool:
    if scope.read_only:
        return False
    own = scope.own_profile_id
    if own is not None and own == profile.id:
        return True
    if scope.is_admin or scope.is_hr:
        return True
    return own is not None and (profile.manager_id == own or (profile.unit_id is not None and profile.unit_id in verifier_units))


def can_verify(scope: PerformanceScope, profile: StaffProfile, verifier_units: set[str]) -> bool:
    own = scope.own_profile_id
    if own is not None and own == profile.id:
        return False  # nobody verifies their own figures
    if scope.read_only:
        return False
    if scope.is_admin or scope.is_hr:
        return True
    return own is not None and (profile.manager_id == own or (profile.unit_id is not None and profile.unit_id in verifier_units))


def _lock_current_version(db: Session, role_id: str) -> None:
    version = latest_version(db, role_id)
    if version is not None:
        lock_version(db, version.id)


def save_entry(
    db: Session, profile: StaffProfile, kpi_key: str, month: date, value: Any, note: str | None, username: str,
    *, window_days: int, today: date, override_window: bool,
) -> KpiEntry:
    kpis = entry_kpis(db, profile.performance_role_id)
    if kpi_key not in kpis:
        raise EntryError("That figure isn't entered monthly for this person's job role.")
    m = first_of_month(month)
    gov.assert_month_open(db, m)
    state = window_state(m, window_days, today)
    if state["state"] == "future":
        raise EntryError("That month hasn't started yet.")
    if state["state"] == "closed" and not override_window:
        raise EntryError(f"The entry window for that month closed on {state['closes_on']:%d %b %Y}. Ask HR to enter or amend it.")
    v = clean_value(kpis[kpi_key], value)
    clean_note = (note or "").strip()[:300] or None
    entry = db.query(KpiEntry).filter_by(profile_id=profile.id, kpi_key=kpi_key, month=m).first()
    if entry is not None and entry.status not in EDITABLE:
        raise EntryError(
            "That figure is already submitted for verification. Ask your verifier to send it back, or HR to reopen it."
            if entry.status == "submitted" else "That figure has been verified and can't be changed. Ask HR to reopen it."
        )
    if entry is None:
        entry = KpiEntry(profile_id=profile.id, kpi_key=kpi_key, month=m, value=v, status="draft", note=clean_note, entered_by_username=username)
        db.add(entry)
        _lock_current_version(db, profile.performance_role_id)
    else:
        entry.value, entry.note, entry.status = v, clean_note, "draft"
        entry.entered_by_username, entry.rejection_reason = username, None
    db.commit()
    db.refresh(entry)
    return entry


def submit_month(db: Session, profile: StaffProfile, month: date, username: str) -> int:
    gov.assert_month_open(db, first_of_month(month))
    drafts = db.query(KpiEntry).filter_by(profile_id=profile.id, month=first_of_month(month), status="draft").all()
    if not drafts:
        raise EntryError("There's nothing to submit for that month: enter at least one figure first.")
    now = datetime.now(timezone.utc)
    for e in drafts:
        e.status, e.submitted_at = "submitted", now
        e.entered_by_username = e.entered_by_username or username
    db.commit()
    return len(drafts)


def decide(db: Session, entry: KpiEntry, approve: bool, username: str, reason: str | None) -> None:
    gov.assert_month_open(db, entry.month)
    if entry.status != "submitted":
        raise EntryError(f"That figure is {entry.status}, not waiting for verification.")
    if entry.entered_by_username == username:
        raise EntryError("Someone else must verify a figure you entered.")
    why = (reason or "").strip()
    if not approve and len(why) < 3:
        raise EntryError("Say why you're sending it back.")
    entry.status = "verified" if approve else "rejected"
    entry.verified_by_username, entry.verified_at = username, datetime.now(timezone.utc)
    entry.rejection_reason = None if approve else why[:300]
    db.commit()


def reopen(db: Session, entry: KpiEntry, reason: str, username: str) -> None:
    gov.assert_month_open(db, entry.month)
    if entry.status not in ("verified", "submitted", "locked"):
        raise EntryError("That figure is already open for editing.")
    if len((reason or "").strip()) < 3:
        raise EntryError("Please give a reason for reopening it.")
    entry.status, entry.verified_by_username, entry.verified_at, entry.rejection_reason = "draft", None, None, None
    db.commit()


# ---- the month view -------------------------------------------------------------------------------------------------


def suggestion(db: Session, profile: StaffProfile, month: date) -> dict[str, Any] | None:
    """Approved website applications assigned to this officer that month - a rough pre-fill, never the truth."""
    if profile.admin_user_id is None:
        return None
    from app.models.loan_application import LoanApplication, LoanApplicationStatus

    m = first_of_month(month)
    start = datetime(m.year, m.month, 1, tzinfo=NAIROBI)
    end = datetime(m.year + (m.month // 12), m.month % 12 + 1, 1, tzinfo=NAIROBI)
    rows = db.query(LoanApplication.amount).filter(
        LoanApplication.assigned_loan_officer_id == profile.admin_user_id,
        LoanApplication.status == LoanApplicationStatus.approved,
        LoanApplication.created_at >= start, LoanApplication.created_at < end,
    ).all()
    if not rows:
        return None
    return {
        "count": len(rows), "total_amount": round(sum(float(a) for (a,) in rows), 2),
        "note": "Approved applications assigned to you this month. Approval is not disbursement - enter the amount actually disbursed.",
    }


def month_view(db: Session, profile: StaffProfile, month: date, config: dict[str, Any], today: date) -> dict[str, Any]:
    m = first_of_month(month)
    kpis = entry_kpis(db, profile.performance_role_id)
    entries = {e.kpi_key: e for e in db.query(KpiEntry).filter_by(profile_id=profile.id, month=m)}
    targets = in_force_map(db, [profile.id], m, list(kpis))
    ramp = int(config.get("ramp_up_months", 0))
    sug = suggestion(db, profile, m)
    rows = []
    for key, k in kpis.items():
        e = entries.get(key)
        t_row = targets.get((profile.id, key))
        target = ramp_target(Decimal(t_row.value), profile.hire_date, m, ramp) if t_row else None
        verified = Decimal(e.value) if e is not None and e.status in COUNTED else None
        pct = achievement_pct(verified, target, k.direction)
        rows.append({
            "kpi_key": key, "label": k.label, "unit": k.unit, "direction": k.direction,
            "entry_id": e.id if e else None, "value": float(e.value) if e else None, "status": e.status if e else None,
            "note": e.note if e else None, "evidence_name": e.evidence_name if e and e.evidence_path else None, "rejection_reason": e.rejection_reason if e else None,
            "target": float(target) if target is not None else None,
            "verified_value": float(verified) if verified is not None else None,
            "achievement_pct": float(pct) if pct is not None and pct.is_finite() else None,
            "pending_verification": e is not None and e.status not in COUNTED,
            "suggestion": sug if key in SUGGESTABLE_KEYS else None,
        })
    return {"month": m, "window": window_state(m, int(config.get("sales_entry_window_days", 10)), today), "rows": rows}
