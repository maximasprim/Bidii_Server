"""
Staff Performance - monthly KPI entries and their verification.

Prefix: /api/admin/performance
  month view / save / submit   menu "/admin/performance", then: yourself, or on someone's behalf if you are HR/admin,
                               their manager or their branch's manager. Out-of-scope ids are the same 404 as missing ones.
  queue / decision             anyone who verifies for others; never your own figures or ones you entered (four eyes)
  reopen                       admin or hr, with a reason
Only VERIFIED figures count towards achievement. Nothing in the audit log carries a figure.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import KpiEntry, PerformanceRole, PerformanceUnit, StaffProfile
from app.routers.admin_performance import _profile_in_scope, admin_or_hr, manage_access
from app.schemas.performance import (
    EntryDecision,
    EntryDecisionResponse,
    EntryMonthResponse,
    EntryReopen,
    EntrySave,
    EntrySubmit,
    EntryWriteResponse,
    QueueItem,
    QueueResponse,
)
from app.services import performance_entries as pe
from app.services.auth import get_current_admin
from app.services.performance_config import effective_config, log_audit
from app.services.performance_scope import PerformanceScope, get_performance_scope
from app.services.performance_targets import first_of_month, in_force_map
from app.services.performance_time import today_nairobi
from app.services.performance_permissions import is_limited, needs

router = APIRouter(prefix="/api/admin/performance", tags=["admin-performance-entries"])


def _view(db: Session, scope: PerformanceScope, profile: StaffProfile, month: date) -> EntryMonthResponse:
    role = db.get(PerformanceRole, profile.performance_role_id)
    config = effective_config(db, role)
    units = pe.verifier_unit_ids(db, scope)
    data = pe.month_view(db, profile, month, config, today_nairobi())
    return EntryMonthResponse(
        profile_id=profile.id, month=data["month"], window=data["window"], rows=data["rows"],
        can_enter=pe.can_enter_for(scope, profile, units), can_verify=pe.can_verify(scope, profile, units),
    )


def _need_entry_rights(db: Session, scope: PerformanceScope, profile: StaffProfile) -> None:
    if not pe.can_enter_for(scope, profile, pe.verifier_unit_ids(db, scope)):
        raise HTTPException(status_code=403, detail="You can't enter figures for this person.")


@router.get("/staff/{profile_id}/entries", response_model=EntryMonthResponse, dependencies=[manage_access])
def month_entries(profile_id: str, month: date | None = None, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    profile = _profile_in_scope(db, scope, profile_id)
    return _view(db, scope, profile, month or today_nairobi())


@router.put("/staff/{profile_id}/entries", response_model=EntryWriteResponse, dependencies=[manage_access])
def save_entry(profile_id: str, payload: EntrySave, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    profile = _profile_in_scope(db, scope, profile_id)
    _need_entry_rights(db, scope, profile)
    config = effective_config(db, db.get(PerformanceRole, profile.performance_role_id))
    try:
        entry = pe.save_entry(
            db, profile, payload.kpi_key, payload.month, payload.value, payload.note, current_admin.username,
            window_days=int(config["sales_entry_window_days"]), today=today_nairobi(), override_window=scope.is_admin or scope.is_hr,
        )
    except pe.EntryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(db, current_admin.username, "entry_saved", "staff_profile", profile.id, {"kpi_key": entry.kpi_key, "month": entry.month.isoformat()})
    return EntryWriteResponse(message="Saved as a draft. Submit the month when you're done.", data=_view(db, scope, profile, entry.month))


@router.post("/staff/{profile_id}/entries/submit", response_model=EntryWriteResponse, dependencies=[manage_access])
def submit_entries(profile_id: str, payload: EntrySubmit, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    profile = _profile_in_scope(db, scope, profile_id)
    _need_entry_rights(db, scope, profile)
    try:
        n = pe.submit_month(db, profile, payload.month, current_admin.username)
    except pe.EntryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(db, current_admin.username, "entries_submitted", "staff_profile", profile.id, {"month": first_of_month(payload.month).isoformat(), "count": n})
    return EntryWriteResponse(message="Submitted for verification.", data=_view(db, scope, profile, payload.month))


@router.get("/entries/queue", response_model=QueueResponse, dependencies=[manage_access])
def verification_queue(month: date | None = None, unit_id: str | None = None, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """Submitted figures waiting for the caller to verify. Never includes your own or ones you entered."""
    scope = get_performance_scope(current_admin, db)
    q = db.query(KpiEntry, StaffProfile).join(StaffProfile, StaffProfile.id == KpiEntry.profile_id).filter(KpiEntry.status == "submitted")
    if not (scope.is_admin or scope.is_hr):
        units = pe.verifier_unit_ids(db, scope)
        own = scope.own_profile_id
        if own is None or scope.read_only:
            return QueueResponse(items=[])
        cond = StaffProfile.manager_id == own
        if units:
            cond = cond | StaffProfile.unit_id.in_(units)
        q = q.filter(cond, StaffProfile.id != own)
    if month:
        q = q.filter(KpiEntry.month == first_of_month(month))
    if unit_id:
        q = q.filter(StaffProfile.unit_id == unit_id)
    q = q.filter((KpiEntry.entered_by_username != current_admin.username) | KpiEntry.entered_by_username.is_(None))
    if scope.own_profile_id:
        q = q.filter(StaffProfile.id != scope.own_profile_id)
    rows = q.order_by(KpiEntry.month.desc(), StaffProfile.full_name.asc()).limit(500).all()
    units = {u.id: u.name for u in db.query(PerformanceUnit.id, PerformanceUnit.name)}
    kpi_cache: dict[str, dict] = {}
    targets: dict[tuple[date, str, str], object] = {}  # (month, profile_id, kpi_key) -> the target row in force that month
    for month_key in {e.month for e, _ in rows}:
        ids = [p.id for e, p in rows if e.month == month_key]
        for (pid, kpi), row in in_force_map(db, ids, month_key).items():
            targets[(month_key, pid, kpi)] = row
    items = []
    for e, p in rows:
        if p.performance_role_id not in kpi_cache:
            kpi_cache[p.performance_role_id] = pe.entry_kpis(db, p.performance_role_id)
        kpis = kpi_cache[p.performance_role_id]
        k = kpis.get(e.kpi_key)
        t = targets.get((e.month, p.id, e.kpi_key))
        items.append(QueueItem(
            entry_id=e.id, profile_id=p.id, profile_name=p.full_name, unit_name=units.get(p.unit_id), kpi_key=e.kpi_key,
            label=k.label if k else e.kpi_key, unit=k.unit if k else "", month=e.month, value=float(e.value),
            target=float(t.value) if t is not None else None, entered_by_username=e.entered_by_username, submitted_at=e.submitted_at,
        ))
    return QueueResponse(items=items)


@router.post("/entries/decision", response_model=EntryDecisionResponse, dependencies=[manage_access])
def decide_entries(payload: EntryDecision, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """Verify or send back up to 200 figures. Ones that can't be decided are reported with a reason; the rest are applied."""
    scope = get_performance_scope(current_admin, db)
    units = pe.verifier_unit_ids(db, scope)
    updated, failed = 0, []
    for eid in dict.fromkeys(payload.entry_ids):
        entry = db.get(KpiEntry, eid)
        profile = db.get(StaffProfile, entry.profile_id) if entry else None
        # Missing, out of scope and "not yours to verify" all look the same, so ids can't be probed.
        if entry is None or profile is None or not scope.can_see(profile.id) or not pe.can_verify(scope, profile, units):
            failed.append({"entry_id": eid, "reason": "Not found."})
            continue
        try:
            pe.decide(db, entry, payload.approve, current_admin.username, payload.reason)
            updated += 1
        except pe.EntryError as exc:
            failed.append({"entry_id": eid, "reason": str(exc)})
    if updated:
        log_audit(db, current_admin.username, "entries_verified" if payload.approve else "entries_rejected", "kpi_entries", None, {"count": updated})
    return EntryDecisionResponse(updated=updated, failed=failed)


@router.post("/entries/{entry_id}/reopen", response_model=EntryWriteResponse, dependencies=[needs("entries.reopen")])
def reopen_entry(entry_id: str, payload: EntryReopen, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    entry = db.get(KpiEntry, entry_id)
    if entry is None or not scope.can_see(entry.profile_id):
        raise HTTPException(status_code=404, detail="Entry not found.")
    if is_limited(db, current_admin, "entries.reopen") and entry.profile_id == scope.own_profile_id:  # a line manager: their team only
        raise HTTPException(status_code=403, detail="You can't reopen your own figures.")
    profile = db.get(StaffProfile, entry.profile_id)
    try:
        pe.reopen(db, entry, payload.reason, current_admin.username)
    except pe.EntryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(db, current_admin.username, "entry_reopened", "staff_profile", profile.id, {"kpi_key": entry.kpi_key, "month": entry.month.isoformat()})
    return EntryWriteResponse(message="Reopened for editing.", data=_view(db, scope, profile, entry.month))
