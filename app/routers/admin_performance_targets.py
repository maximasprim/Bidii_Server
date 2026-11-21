"""
Staff Performance - levels and targets.

Prefix: /api/admin/performance
  reads             menu "/admin/performance", then scope-filtered (out-of-scope ids = the same 404 as missing)
  set / bulk        admin or hr (bulk is always a dry-run plan first)
  requests          anyone who can see the person and isn't them may REQUEST a change; admin/hr decide
Every write needs a reason. Targets are append-only (see performance_targets.py).
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import (
    PerformanceLevel,
    PerformanceRole,
    PerformanceUnit,
    StaffProfile,
    TargetChangeRequest,
)
from app.routers.admin_performance import _profile_in_scope, admin_only, admin_or_hr, manage_access
from app.schemas.performance import (
    BulkTargetRequest,
    BulkTargetResponse,
    LevelCreate,
    LevelListResponse,
    LevelRead,
    LevelUpdate,
    LevelWriteResponse,
    PlanItemRead,
    StaffTargetsResponse,
    TargetGridResponse,
    TargetGridRow,
    TargetHistoryRow,
    TargetRequestCreate,
    TargetRequestDecision,
    TargetRequestListResponse,
    TargetRequestRead,
    TargetRequestWriteResponse,
    TargetRow,
    TargetSet,
    TargetSetResponse,
)
from app.services import performance_targets as tg
from app.services.auth import get_current_admin
from app.services.performance_config import log_audit
from app.services.performance_levels import LevelError, make_key, validate_default_targets
from app.services.performance_roles import targetable_kpis
from app.services.performance_scope import PerformanceScope, get_performance_scope
from app.services.performance_time import today_nairobi

router = APIRouter(prefix="/api/admin/performance", tags=["admin-performance-targets"])


def _num(v):
    return float(v) if v is not None else None


def _level_read(db: Session, level: PerformanceLevel) -> LevelRead:
    data = LevelRead.model_validate(level)
    data.staff_count = db.query(StaffProfile).filter(StaffProfile.level_id == level.id).count()
    return data


# ---- levels ----------------------------------------------------------------------------------------------


@router.get("/levels", response_model=LevelListResponse, dependencies=[manage_access])
def list_levels(role_id: str | None = None, include_inactive: bool = False, db: Session = Depends(get_db)):
    q = db.query(PerformanceLevel)
    if role_id:
        q = q.filter(PerformanceLevel.role_id == role_id)
    if not include_inactive:
        q = q.filter(PerformanceLevel.is_active.is_(True))
    return LevelListResponse(items=[_level_read(db, l) for l in q.order_by(PerformanceLevel.role_id, PerformanceLevel.rank).all()])


@router.post("/levels", response_model=LevelWriteResponse, status_code=201, dependencies=[admin_only])
def create_level(payload: LevelCreate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    if db.get(PerformanceRole, payload.role_id) is None:
        raise HTTPException(status_code=400, detail="That job role doesn't exist.")
    key = make_key(payload.label)
    if db.query(PerformanceLevel).filter_by(role_id=payload.role_id, key=key).first() is not None:
        raise HTTPException(status_code=409, detail="This role already has a level with that name.")
    try:
        targets = validate_default_targets(db, payload.role_id, payload.default_targets)
    except LevelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    level = PerformanceLevel(role_id=payload.role_id, key=key, label=payload.label.strip(), rank=payload.rank, default_targets=targets, note=payload.note)
    db.add(level)
    db.commit()
    db.refresh(level)
    log_audit(db, current_admin.username, "level_created", "performance_level", level.id, {"role_id": level.role_id})
    return LevelWriteResponse(message="Level created.", data=_level_read(db, level))


@router.patch("/levels/{level_id}", response_model=LevelWriteResponse, dependencies=[admin_only])
def update_level(level_id: str, payload: LevelUpdate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    level = db.get(PerformanceLevel, level_id)
    if level is None:
        raise HTTPException(status_code=404, detail="Level not found.")
    changes = payload.model_dump(exclude_unset=True)
    if "default_targets" in changes and changes["default_targets"] is not None:
        try:
            changes["default_targets"] = validate_default_targets(db, level.role_id, changes["default_targets"])
        except LevelError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    for field in ("label", "rank", "default_targets", "is_active"):
        if field in changes and changes[field] is None:
            changes.pop(field)
    for field, value in changes.items():
        setattr(level, field, value)
    db.commit()
    db.refresh(level)
    # Editing a level's defaults never touches anyone's existing targets - they change only through "apply level defaults".
    log_audit(db, current_admin.username, "level_updated", "performance_level", level.id, {"fields": sorted(changes)})
    return LevelWriteResponse(message="Level updated.", data=_level_read(db, level))


# ---- one person's targets ---------------------------------------------------------------------------------------


def _staff_targets(db: Session, profile: StaffProfile, month: date) -> StaffTargetsResponse:
    kpis = targetable_kpis(db, profile.performance_role_id)
    current = tg.in_force_map(db, [profile.id], month)
    items = []
    for key, k in kpis.items():
        row = current.get((profile.id, key))
        items.append(TargetRow(kpi_key=key, label=k.label, unit=k.unit, direction=k.direction, value=_num(row.value) if row else None,
                               effective_from=row.effective_from if row else None, source=row.source if row else None, reason=row.reason if row else None))
    hist = [TargetHistoryRow(id=h.id, kpi_key=h.kpi_key, effective_from=h.effective_from, value=float(h.value), source=h.source, reason=h.reason,
                             set_by_username=h.set_by_username, created_at=h.created_at) for h in tg.history(db, profile.id)]
    return StaffTargetsResponse(profile_id=profile.id, month=month, items=items, history=hist)


@router.get("/staff/{profile_id}/targets", response_model=StaffTargetsResponse, dependencies=[manage_access])
def staff_targets(profile_id: str, month: date | None = None, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    profile = _profile_in_scope(db, scope, profile_id)
    return _staff_targets(db, profile, tg.first_of_month(month or today_nairobi()))


@router.put("/staff/{profile_id}/targets", response_model=TargetSetResponse, dependencies=[admin_or_hr])
def set_staff_target(profile_id: str, payload: TargetSet, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    profile = _profile_in_scope(db, scope, profile_id)
    try:
        row = tg.set_target(db, profile, payload.kpi_key, payload.effective_from, payload.value, payload.reason, current_admin.username)
    except tg.TargetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if row is not None:
        log_audit(db, current_admin.username, "target_set", "staff_profile", profile.id, {"kpi_key": payload.kpi_key, "effective_from": row.effective_from.isoformat()})
    data = _staff_targets(db, profile, tg.first_of_month(payload.effective_from))
    return TargetSetResponse(changed=row is not None, message="Target saved." if row else "That is already the target in force; nothing changed.", data=data)


# ---- grid -----------------------------------------------------------------------------------------------------------


@router.get("/targets/grid", response_model=TargetGridResponse, dependencies=[manage_access])
def targets_grid(
    kpi_key: str = Query(..., min_length=1, max_length=60),
    month: date | None = None,
    role_id: str | None = None,
    unit_id: str | None = None,
    level_id: str | None = None,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
):
    """The in-force target for one KPI across the people the caller may see (one query for targets, however many people)."""
    scope = get_performance_scope(current_admin, db)
    m = tg.first_of_month(month or today_nairobi())
    ids = None if scope.visible_ids is None else list(scope.visible_ids)
    try:
        profiles = tg.select_profiles(db, role_id=role_id, unit_id=unit_id, level_id=level_id, profile_ids=ids)
    except tg.TargetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    roles = {r.id: r.name for r in db.query(PerformanceRole.id, PerformanceRole.name)}
    units = {u.id: u.name for u in db.query(PerformanceUnit.id, PerformanceUnit.name)}
    levels = {l.id: l.label for l in db.query(PerformanceLevel.id, PerformanceLevel.label)}
    current = tg.in_force_map(db, [p.id for p in profiles], m, [kpi_key])
    kpi_cache: dict[str, bool] = {}
    rows = []
    for p in profiles:
        if p.performance_role_id not in kpi_cache:
            kpi_cache[p.performance_role_id] = kpi_key in targetable_kpis(db, p.performance_role_id)
        r = current.get((p.id, kpi_key))
        rows.append(TargetGridRow(profile_id=p.id, full_name=p.full_name, unit_name=units.get(p.unit_id), role_name=roles.get(p.performance_role_id),
                                  level_label=levels.get(p.level_id), value=_num(r.value) if r else None, effective_from=r.effective_from if r else None,
                                  source=r.source if r else None, has_kpi=kpi_cache[p.performance_role_id]))
    return TargetGridResponse(kpi_key=kpi_key, month=m, items=rows)


# ---- bulk -----------------------------------------------------------------------------------------------------------


@router.post("/targets/bulk", response_model=BulkTargetResponse, dependencies=[admin_or_hr])
def bulk_targets(payload: BulkTargetRequest, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """
    Set a value, apply level defaults, or copy forward (with an optional % uplift) for many people. With
    dry_run (the default) it only returns the plan. Applying writes every change in one transaction.
    """
    try:
        profiles = tg.select_profiles(db, role_id=payload.role_id, unit_id=payload.unit_id, region_id=payload.region_id,
                                      level_id=payload.level_id, profile_ids=payload.profile_ids)
        if payload.mode == "set_value":
            if not payload.kpi_key or payload.value is None:
                raise tg.TargetError("Choose a KPI and a value.")
            items = tg.plan_set_value(db, profiles, payload.kpi_key, payload.value, payload.effective_from)
            source = "bulk"
        elif payload.mode == "level_defaults":
            items = tg.plan_level_defaults(db, profiles, payload.effective_from, payload.overwrite)
            source = "level_default"
        else:
            if payload.source_month is None:
                raise tg.TargetError("Choose the month to copy from.")
            items = tg.plan_copy_forward(db, profiles, payload.source_month, payload.effective_from, payload.uplift_pct, payload.kpi_key)
            source = "copy_forward"
        tg.clean_reason(payload.reason)  # fail a dry run too, so the admin finds out before applying
        applied = 0 if payload.dry_run else tg.apply_plan(db, items, payload.effective_from, payload.reason, current_admin.username, source)
    except tg.TargetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    summary = {a: sum(1 for i in items if i.action == a) for a in ("create", "unchanged", "skip")}
    if not payload.dry_run:
        log_audit(db, current_admin.username, "targets_bulk_applied", "targets", None, {"mode": payload.mode, "created": applied, "people": len(profiles)})
    return BulkTargetResponse(
        dry_run=payload.dry_run, applied=applied, summary=summary,
        items=[PlanItemRead(profile_id=i.profile_id, full_name=i.full_name, kpi_key=i.kpi_key, current=_num(i.current), new=_num(i.new), action=i.action, note=i.note) for i in items],
    )


# ---- change requests ---------------------------------------------------------------------------------------------------


def _request_read(req: TargetChangeRequest, names: dict[str, str], current: dict, scope: PerformanceScope, username: str) -> TargetRequestRead:
    data = TargetRequestRead.model_validate(req)
    data.profile_name = names.get(req.profile_id)
    data.is_mine = req.requested_by_username == username
    data.can_decide = req.status == "pending" and (scope.is_admin or scope.is_hr) and not data.is_mine and req.profile_id != scope.own_profile_id
    cur = current.get((req.profile_id, req.kpi_key))
    data.current_value = _num(cur.value) if cur else None
    return data


def _reads(db: Session, reqs: list[TargetChangeRequest], scope: PerformanceScope, username: str) -> list[TargetRequestRead]:
    ids = {r.profile_id for r in reqs}
    names = {pid: n for pid, n in db.query(StaffProfile.id, StaffProfile.full_name).filter(StaffProfile.id.in_(ids or {""}))}
    current = tg.in_force_map(db, ids, today_nairobi())
    return [_request_read(r, names, current, scope, username) for r in reqs]


def _can_request(scope: PerformanceScope, profile: StaffProfile) -> bool:
    if scope.read_only:
        return False
    if scope.is_admin or scope.is_hr:
        return True
    # A leader may propose a change for someone they can see - never for themselves.
    return scope.own_profile_id is not None and profile.id != scope.own_profile_id


@router.post("/staff/{profile_id}/target-requests", response_model=TargetRequestWriteResponse, status_code=201, dependencies=[manage_access])
def create_target_request(profile_id: str, payload: TargetRequestCreate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    profile = _profile_in_scope(db, scope, profile_id)
    if not _can_request(scope, profile):
        raise HTTPException(status_code=403, detail="You can't request a target change for this person.")
    try:
        req = tg.create_request(db, profile, payload.kpi_key, payload.effective_from, payload.value, payload.reason, current_admin.username)
    except tg.TargetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(db, current_admin.username, "target_change_requested", "staff_profile", profile.id, {"kpi_key": req.kpi_key})
    return TargetRequestWriteResponse(message="Request sent for approval.", data=_reads(db, [req], scope, current_admin.username)[0])


@router.get("/target-requests", response_model=TargetRequestListResponse, dependencies=[manage_access])
def list_target_requests(status: str = Query(default="pending", pattern="^(pending|approved|rejected|withdrawn|all)$"), db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """Admin/HR see every request; everyone else only the requests they raised, for people they can still see."""
    scope = get_performance_scope(current_admin, db)
    q = db.query(TargetChangeRequest)
    if status != "all":
        q = q.filter(TargetChangeRequest.status == status)
    if not (scope.is_admin or scope.is_hr):
        q = q.filter(TargetChangeRequest.requested_by_username == current_admin.username)
        if scope.visible_ids is not None:
            q = q.filter(TargetChangeRequest.profile_id.in_(scope.visible_ids or {""}))
    return TargetRequestListResponse(items=_reads(db, q.order_by(TargetChangeRequest.created_at.desc()).limit(200).all(), scope, current_admin.username))


def _request_in_scope(db: Session, scope: PerformanceScope, request_id: str, username: str) -> TargetChangeRequest:
    req = db.get(TargetChangeRequest, request_id)
    visible = req is not None and scope.can_see(req.profile_id) and (scope.is_admin or scope.is_hr or req.requested_by_username == username)
    if not visible:
        raise HTTPException(status_code=404, detail="Request not found.")
    return req


@router.post("/target-requests/{request_id}/decision", response_model=TargetRequestWriteResponse, dependencies=[admin_or_hr])
def decide_target_request(request_id: str, payload: TargetRequestDecision, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    req = _request_in_scope(db, scope, request_id, current_admin.username)
    # Four eyes: nobody approves or rejects a change to their own target, and nobody decides their own request.
    if req.profile_id == scope.own_profile_id or req.requested_by_username == current_admin.username:
        raise HTTPException(status_code=403, detail="Someone else must decide this request.")
    try:
        tg.decide_request(db, req, payload.approve, current_admin.username, payload.note)
    except tg.TargetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(db, current_admin.username, "target_request_decided", "staff_profile", req.profile_id, {"approved": payload.approve, "request_id": req.id})
    return TargetRequestWriteResponse(message="Target change approved and applied." if payload.approve else "Request rejected.", data=_reads(db, [req], scope, current_admin.username)[0])


@router.post("/target-requests/{request_id}/withdraw", response_model=TargetRequestWriteResponse, dependencies=[manage_access])
def withdraw_target_request(request_id: str, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    req = _request_in_scope(db, scope, request_id, current_admin.username)
    if req.requested_by_username != current_admin.username:
        raise HTTPException(status_code=403, detail="Only the person who made the request can withdraw it.")
    try:
        tg.withdraw_request(db, req)
    except tg.TargetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TargetRequestWriteResponse(message="Request withdrawn.", data=_reads(db, [req], scope, current_admin.username)[0])
