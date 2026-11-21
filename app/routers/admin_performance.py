"""
Staff Performance module - foundation API.

Prefix: /api/admin/performance

EVERY route below carries an explicit authorization dependency (a menu or a
role requirement) and anything that returns people goes through
get_performance_scope(). A test enumerates this router and fails if a route
is ever added without one.

  /me                          menu "/admin/my-performance" (everyone)
  reads                        menu "/admin/performance", then scope-filtered
  profile / unit / region      admin or hr (units & regions: admin only)
  writes
  config, scales, roles,       admin only
  audit

Out-of-scope ids return exactly the same 404 as ids that don't exist.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.branch import Branch
from app.models.performance import (
    PerformanceAction,
    PerformanceAuditLog,
    PerformanceKpi,
    PerformanceLevel,
    PerformanceRole,
    PerformanceRoleVersion,
    PerformanceUnit,
    Region,
    StaffAssignmentHistory,
    StaffProfile,
    UnitRegionLink,
)
from app.schemas.performance import (
    ActionCreate,
    ActionListResponse,
    ActionRead,
    ActionWriteResponse,
    AssignmentListResponse,
    AssignmentRead,
    AuditEntry,
    AuditListResponse,
    ConfigRead,
    ConfigUpdate,
    ConfigUpdateResponse,
    LinkableUser,
    LinkableUsersResponse,
    MyPerformanceResponse,
    PerformanceRoleCreate,
    PerformanceRoleListResponse,
    PerformanceRoleRead,
    PerformanceRoleUpdate,
    PerformanceRoleWriteResponse,
    RegionCreate,
    RegionListResponse,
    RegionRead,
    RegionUpdate,
    RegionWriteResponse,
    ScaleCreate,
    ScaleListResponse,
    ScalePreviewRequest,
    ScalePreviewResponse,
    ScaleRead,
    ScaleWriteResponse,
    StaffProfileCreate,
    StaffProfileListResponse,
    StaffProfileRead,
    StaffProfileUpdate,
    StaffProfileWriteResponse,
    UnitCreate,
    UnitListResponse,
    UnitRead,
    UnitRegionAssign,
    UnitUpdate,
    UnitWriteResponse,
)
from app.services import performance_scales
from app.services.auth import get_current_admin, require_roles
from app.services.performance_config import (
    RESTRICTED_ACTION_TYPES,
    ensure_defaults,
    get_org_config,
    log_audit,
    save_org_config,
    validate_config,
)
from app.services.performance_roles import is_configured
from app.services.performance_scope import PerformanceScope, get_performance_scope
from app.services.performance_time import today_nairobi
from app.services.role_permissions import require_menu_access

logger = logging.getLogger("bidii.performance")

router = APIRouter(prefix="/api/admin/performance", tags=["admin-performance"])

MY_MENU = "/admin/my-performance"
MANAGE_MENU = "/admin/performance"
my_access = Depends(require_menu_access(MY_MENU))
manage_access = Depends(require_menu_access(MANAGE_MENU))
admin_only = Depends(require_roles("admin"))
admin_or_hr = Depends(require_roles("admin", "hr"))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60]


def _not_found(what: str = "Staff profile") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found.")


def _profile_in_scope(db: Session, scope: PerformanceScope, profile_id: str) -> StaffProfile:
    profile = db.get(StaffProfile, profile_id)
    # Identical response for "doesn't exist" and "not yours" so ids can't be probed.
    if profile is None or not scope.can_see(profile.id):
        raise _not_found()
    return profile


def _current_region_links(db: Session, unit_ids: set[str]) -> dict[str, UnitRegionLink]:
    if not unit_ids:
        return {}
    today = today_nairobi()
    rows = (
        db.query(UnitRegionLink)
        .filter(
            UnitRegionLink.unit_id.in_(unit_ids),
            UnitRegionLink.effective_from <= today,
            or_(UnitRegionLink.effective_to.is_(None), UnitRegionLink.effective_to >= today),
        )
        .order_by(UnitRegionLink.effective_from.asc())
        .all()
    )
    return {r.unit_id: r for r in rows}  # latest-starting wins


def _profiles_read(db: Session, scope: PerformanceScope, profiles: list[StaffProfile]) -> list[StaffProfileRead]:
    """Batched: a fixed handful of queries however many profiles, never one per row."""
    roles = {r.id: r for r in db.query(PerformanceRole).filter(PerformanceRole.id.in_({p.performance_role_id for p in profiles}))} if profiles else {}
    unit_ids = {p.unit_id for p in profiles if p.unit_id}
    units = {u.id: u for u in db.query(PerformanceUnit).filter(PerformanceUnit.id.in_(unit_ids))} if unit_ids else {}
    manager_ids = {p.manager_id for p in profiles if p.manager_id}
    managers = {m.id: m for m in db.query(StaffProfile).filter(StaffProfile.id.in_(manager_ids))} if manager_ids else {}
    user_ids = {p.admin_user_id for p in profiles if p.admin_user_id}
    users = {u.id: u for u in db.query(AdminUser).filter(AdminUser.id.in_(user_ids))} if user_ids else {}
    level_ids = {p.level_id for p in profiles if p.level_id}
    levels = {l.id: l for l in db.query(PerformanceLevel).filter(PerformanceLevel.id.in_(level_ids))} if level_ids else {}
    out = []
    for p in profiles:
        role, unit, manager, user = roles.get(p.performance_role_id), units.get(p.unit_id), managers.get(p.manager_id), users.get(p.admin_user_id)
        out.append(
            StaffProfileRead(
                id=p.id,
                full_name=p.full_name,
                employee_no=p.employee_no,
                performance_role_id=p.performance_role_id,
                performance_role_name=role.name if role else None,
                performance_role_kind=role.kind if role else None,
                admin_user_id=p.admin_user_id,
                username=user.username if user else None,
                unit_id=p.unit_id,
                unit_name=unit.name if unit else None,
                manager_id=p.manager_id,
                manager_name=manager.full_name if manager else None,
                level_id=p.level_id,
                level_label=levels[p.level_id].label if p.level_id in levels else None,
                hire_date=p.hire_date,
                probation_end_date=p.probation_end_date,
                status=scope.display_status(p),
                is_executive=p.is_executive if scope.unrestricted else False,
                is_active=p.is_active,
                created_at=p.created_at,
            )
        )
    return out


def _role_reads(db: Session, roles: list[PerformanceRole]) -> list[PerformanceRoleRead]:
    """Batched (a fixed number of queries however many roles): staff counts, current JD version, KPI totals."""
    ids = [r.id for r in roles]
    if not ids:
        return []
    staff_counts = dict(
        db.query(StaffProfile.performance_role_id, func.count(StaffProfile.id))
        .filter(StaffProfile.performance_role_id.in_(ids))
        .group_by(StaffProfile.performance_role_id)
        .all()
    )
    latest: dict[str, PerformanceRoleVersion] = {}
    for v in db.query(PerformanceRoleVersion).filter(PerformanceRoleVersion.role_id.in_(ids)).order_by(PerformanceRoleVersion.version.asc()):
        latest[v.role_id] = v  # ascending order, so the last one wins
    kpis_by_version: dict[str, list[PerformanceKpi]] = {}
    if latest:
        for k in db.query(PerformanceKpi).filter(PerformanceKpi.role_version_id.in_([v.id for v in latest.values()])):
            kpis_by_version.setdefault(k.role_version_id, []).append(k)
    out = []
    for role in roles:
        data = PerformanceRoleRead.model_validate(role)
        data.staff_count = staff_counts.get(role.id, 0)
        version = latest.get(role.id)
        kpis = kpis_by_version.get(version.id, []) if version else []
        data.current_version = version.version if version else None
        data.kpi_count = len(kpis)
        data.configured = is_configured(version, kpis)
        out.append(data)
    return out


def _role_read(db: Session, role: PerformanceRole) -> PerformanceRoleRead:
    return _role_reads(db, [role])[0]


def _unit_read(unit: PerformanceUnit, link: UnitRegionLink | None, regions: dict[str, Region]) -> UnitRead:
    region = regions.get(link.region_id) if link else None
    return UnitRead(
        id=unit.id,
        name=unit.name,
        kind=unit.kind,
        branch_id=unit.branch_id,
        manager_profile_id=unit.manager_profile_id,
        region_id=region.id if region else None,
        region_name=region.name if region else None,
        is_active=unit.is_active,
    )


def _region_map(db: Session) -> dict[str, Region]:
    return {r.id: r for r in db.query(Region).all()}


def _region_read(db: Session, region: Region) -> RegionRead:
    today = today_nairobi()
    count = (
        db.query(UnitRegionLink.unit_id)
        .filter(
            UnitRegionLink.region_id == region.id,
            UnitRegionLink.effective_from <= today,
            or_(UnitRegionLink.effective_to.is_(None), UnitRegionLink.effective_to >= today),
        )
        .distinct()
        .count()
    )
    return RegionRead(id=region.id, name=region.name, manager_profile_id=region.manager_profile_id, is_active=region.is_active, unit_count=count)


def _require_profile_exists(db: Session, profile_id: str | None, label: str) -> None:
    if profile_id is not None and db.get(StaffProfile, profile_id) is None:
        raise HTTPException(status_code=400, detail=f"That {label} doesn't exist.")


def _validate_level(db: Session, level_id: str | None, role_id: str) -> None:
    """A level belongs to one performance role; a person can only hold a level of their own role."""
    if level_id is None:
        return
    level = db.get(PerformanceLevel, level_id)
    if level is None or not level.is_active:
        raise HTTPException(status_code=400, detail="That level doesn't exist or is inactive.")
    if level.role_id != role_id:
        raise HTTPException(status_code=400, detail="That level belongs to a different job role.")


def _validate_links(
    db: Session,
    *,
    role_id: str | None = None,
    unit_id: str | None = None,
    manager_id: str | None = None,
    admin_user_id: str | None = None,
    this_profile_id: str | None = None,
) -> None:
    if role_id is not None:
        role = db.get(PerformanceRole, role_id)
        if role is None or not role.is_active:
            raise HTTPException(status_code=400, detail="That performance role doesn't exist or is inactive.")
    if unit_id is not None:
        unit = db.get(PerformanceUnit, unit_id)
        if unit is None or not unit.is_active:
            raise HTTPException(status_code=400, detail="That unit doesn't exist or is inactive.")
    if admin_user_id is not None:
        if db.get(AdminUser, admin_user_id) is None:
            raise HTTPException(status_code=400, detail="That dashboard user doesn't exist.")
        clash = db.query(StaffProfile).filter(StaffProfile.admin_user_id == admin_user_id)
        if this_profile_id:
            clash = clash.filter(StaffProfile.id != this_profile_id)
        if clash.first() is not None:
            raise HTTPException(status_code=409, detail="That dashboard user is already linked to another staff profile.")
    if manager_id is not None:
        if db.get(StaffProfile, manager_id) is None:
            raise HTTPException(status_code=400, detail="That manager doesn't exist.")
        if this_profile_id:
            # Walk up the proposed manager's own chain: reaching this profile means a loop.
            seen: set[str] = set()
            cursor: str | None = manager_id
            while cursor and cursor not in seen:
                if cursor == this_profile_id:
                    raise HTTPException(status_code=400, detail="That would create a reporting loop (someone can't report to themselves, directly or indirectly).")
                seen.add(cursor)
                nxt = db.get(StaffProfile, cursor)
                cursor = nxt.manager_id if nxt else None


def _record_assignment(db: Session, profile: StaffProfile, username: str, reason: str | None, effective_from=None) -> None:
    """Closes the open assignment row and opens a new one, so each period is attributed to the right unit/manager/role."""
    start = effective_from or today_nairobi()
    open_row = (
        db.query(StaffAssignmentHistory)
        .filter(StaffAssignmentHistory.profile_id == profile.id, StaffAssignmentHistory.effective_to.is_(None))
        .first()
    )
    if open_row is not None:
        open_row.effective_to = max(open_row.effective_from, start - timedelta(days=1))
    db.add(
        StaffAssignmentHistory(
            profile_id=profile.id,
            unit_id=profile.unit_id,
            manager_id=profile.manager_id,
            performance_role_id=profile.performance_role_id,
            effective_from=start,
            reason=reason,
            created_by=username,
        )
    )


def _status_filter(query, scope: PerformanceScope, wanted: str):
    """
    Filtering by status must not leak restricted information: for anyone outside
    HR, "pip" matches only profiles they may see PIPs for, and "active" also
    matches the PIP profiles they are NOT allowed to know about (shown as active).
    """
    if scope.is_hr or wanted not in ("pip", "active"):
        return query.filter(StaffProfile.status == wanted)
    own = scope.own_profile_id
    audience = or_(StaffProfile.id == own, StaffProfile.manager_id == own) if own else StaffProfile.id.is_(None)
    if wanted == "pip":
        return query.filter(StaffProfile.status == "pip", audience)
    return query.filter(or_(StaffProfile.status == "active", and_(StaffProfile.status == "pip", ~audience)))


# --------------------------------------------------------------------------
# Me
# --------------------------------------------------------------------------


@router.get("/me", response_model=MyPerformanceResponse, dependencies=[my_access])
def my_performance(db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """The caller's own profile plus the scoring legend currently in force."""
    scope = get_performance_scope(current_admin, db)
    scale = performance_scales.scale_as_of(db)
    profile = db.get(StaffProfile, scope.own_profile_id) if scope.own_profile_id else None
    return MyPerformanceResponse(
        profile=_profiles_read(db, scope, [profile])[0] if profile else None,
        categories=scale.categories,
        rating_bands=scale.rating_bands,
        team_size=max(0, scope.team_size),
        sees_everyone=scope.unrestricted,
        read_only=scope.read_only,
    )


# --------------------------------------------------------------------------
# Config & rating scales
# --------------------------------------------------------------------------


@router.get("/config", response_model=ConfigRead, dependencies=[manage_access])
def read_config(db: Session = Depends(get_db)):
    cfg, version = get_org_config(db)
    return ConfigRead(config=cfg, version=version)


@router.put("/config", response_model=ConfigUpdateResponse, dependencies=[admin_only])
def update_config(payload: ConfigUpdate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    try:
        cfg, version = save_org_config(db, payload.config, current_admin.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(db, current_admin.username, "config_updated", "config", "org", {"version": version})
    return ConfigUpdateResponse(data=ConfigRead(config=cfg, version=version))


@router.get("/scales", response_model=ScaleListResponse, dependencies=[manage_access])
def list_scales(db: Session = Depends(get_db)):
    current = performance_scales.scale_as_of(db)
    items = []
    for row in performance_scales.list_scales(db):
        item = ScaleRead.model_validate(row)
        item.is_current = row.id == current.id
        items.append(item)
    return ScaleListResponse(items=items)


@router.post("/scales", response_model=ScaleWriteResponse, status_code=201, dependencies=[admin_only])
def create_scale(payload: ScaleCreate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """Adds a NEW effective-dated scale. Past scales are never edited, so closed periods never change."""
    try:
        row = performance_scales.create_scale(
            db,
            effective_from=payload.effective_from,
            categories=payload.categories,
            rating_bands=payload.rating_bands,
            rating_to_pi=payload.rating_to_pi,
            note=payload.note,
            username=current_admin.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(db, current_admin.username, "scale_created", "rating_scale", row.id, {"effective_from": row.effective_from.isoformat()})
    data = ScaleRead.model_validate(row)
    data.is_current = row.id == performance_scales.scale_as_of(db).id
    return ScaleWriteResponse(message="Rating scale saved.", data=data)


@router.post("/scales/preview", response_model=ScalePreviewResponse, dependencies=[admin_only])
def preview_scale(payload: ScalePreviewRequest, db: Session = Depends(get_db)):
    """What would these thresholds do to the supplied scores, compared with today's? Writes nothing."""
    current = performance_scales.scale_as_of(db)
    try:
        result = performance_scales.preview_distribution(payload.scores, current.categories, payload.categories)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ScalePreviewResponse(**result)


# --------------------------------------------------------------------------
# Performance roles (job families)
# --------------------------------------------------------------------------


@router.get("/roles", response_model=PerformanceRoleListResponse, dependencies=[manage_access])
def list_roles(include_inactive: bool = False, db: Session = Depends(get_db)):
    ensure_defaults(db)
    query = db.query(PerformanceRole)
    if not include_inactive:
        query = query.filter(PerformanceRole.is_active.is_(True))
    return PerformanceRoleListResponse(items=_role_reads(db, query.order_by(PerformanceRole.name.asc()).all()))


def _check_overrides(overrides: dict | None) -> None:
    if overrides:
        try:
            validate_config(overrides)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid role override: {exc}") from exc


@router.post("/roles", response_model=PerformanceRoleWriteResponse, status_code=201, dependencies=[admin_only])
def create_role(payload: PerformanceRoleCreate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    ensure_defaults(db)
    key = _slug(payload.key or payload.name)
    if not key:
        raise HTTPException(status_code=400, detail="Give the role a name made of letters or numbers.")
    if db.query(PerformanceRole).filter(PerformanceRole.key == key).first() is not None:
        raise HTTPException(status_code=409, detail="A performance role with that name already exists.")
    _check_overrides(payload.config_overrides)
    role = PerformanceRole(
        key=key, name=payload.name.strip(), description=payload.description, department=payload.department,
        kind=payload.kind, config_overrides=payload.config_overrides or None,
    )
    db.add(role)
    db.commit()
    db.refresh(role)
    log_audit(db, current_admin.username, "role_created", "performance_role", role.id, {"name": role.name})
    return PerformanceRoleWriteResponse(message="Performance role created.", data=_role_read(db, role))


@router.patch("/roles/{role_id}", response_model=PerformanceRoleWriteResponse, dependencies=[admin_only])
def update_role(role_id: str, payload: PerformanceRoleUpdate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    role = db.get(PerformanceRole, role_id)
    if role is None:
        raise _not_found("Performance role")
    changes = payload.model_dump(exclude_unset=True)
    clear = changes.pop("clear_config_overrides", False)
    if "config_overrides" in changes:
        _check_overrides(changes["config_overrides"])
    for field, value in changes.items():
        setattr(role, field, value)
    if clear:
        role.config_overrides = None
    db.commit()
    db.refresh(role)
    log_audit(db, current_admin.username, "role_updated", "performance_role", role.id, {"fields": sorted(changes)})
    return PerformanceRoleWriteResponse(message="Performance role updated.", data=_role_read(db, role))


@router.delete("/roles/{role_id}", response_model=PerformanceRoleWriteResponse, dependencies=[admin_only])
def delete_role(role_id: str, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    role = db.get(PerformanceRole, role_id)
    if role is None:
        raise _not_found("Performance role")
    if role.is_system:
        raise HTTPException(status_code=400, detail="Built-in roles can't be deleted - deactivate it instead.")
    if db.query(StaffProfile).filter(StaffProfile.performance_role_id == role.id).count():
        raise HTTPException(status_code=409, detail="Staff are still assigned to this role - reassign them or deactivate the role instead.")
    data = _role_read(db, role)
    db.delete(role)
    db.commit()
    log_audit(db, current_admin.username, "role_deleted", "performance_role", role_id, {"name": data.name})
    return PerformanceRoleWriteResponse(message="Performance role deleted.", data=data)


# --------------------------------------------------------------------------
# Units & regions
# --------------------------------------------------------------------------


@router.get("/units", response_model=UnitListResponse, dependencies=[manage_access])
def list_units(include_inactive: bool = False, db: Session = Depends(get_db)):
    query = db.query(PerformanceUnit)
    if not include_inactive:
        query = query.filter(PerformanceUnit.is_active.is_(True))
    units = query.order_by(PerformanceUnit.name.asc()).all()
    links, regions = _current_region_links(db, {u.id for u in units}), _region_map(db)
    return UnitListResponse(items=[_unit_read(u, links.get(u.id), regions) for u in units])


def _check_unit_links(db: Session, branch_id: str | None, manager_profile_id: str | None) -> None:
    if branch_id is not None and db.get(Branch, branch_id) is None:
        raise HTTPException(status_code=400, detail="That branch doesn't exist.")
    _require_profile_exists(db, manager_profile_id, "manager")


@router.post("/units", response_model=UnitWriteResponse, status_code=201, dependencies=[admin_only])
def create_unit(payload: UnitCreate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    if db.query(PerformanceUnit).filter(PerformanceUnit.name == payload.name.strip()).first() is not None:
        raise HTTPException(status_code=409, detail="A unit with that name already exists.")
    _check_unit_links(db, payload.branch_id, payload.manager_profile_id)
    unit = PerformanceUnit(name=payload.name.strip(), kind=payload.kind, branch_id=payload.branch_id, manager_profile_id=payload.manager_profile_id)
    db.add(unit)
    db.commit()
    db.refresh(unit)
    log_audit(db, current_admin.username, "unit_created", "performance_unit", unit.id, {"name": unit.name})
    return UnitWriteResponse(message="Unit created.", data=_unit_read(unit, None, {}))


@router.patch("/units/{unit_id}", response_model=UnitWriteResponse, dependencies=[admin_only])
def update_unit(unit_id: str, payload: UnitUpdate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    unit = db.get(PerformanceUnit, unit_id)
    if unit is None:
        raise _not_found("Unit")
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("name") is None:
        changes.pop("name", None)
    if "name" in changes:
        changes["name"] = changes["name"].strip()
        clash = db.query(PerformanceUnit).filter(PerformanceUnit.name == changes["name"], PerformanceUnit.id != unit.id).first()
        if clash is not None:
            raise HTTPException(status_code=409, detail="A unit with that name already exists.")
    if changes.get("kind") is None:
        changes.pop("kind", None)
    _check_unit_links(db, changes.get("branch_id"), changes.get("manager_profile_id"))
    for field, value in changes.items():
        setattr(unit, field, value)
    db.commit()
    db.refresh(unit)
    log_audit(db, current_admin.username, "unit_updated", "performance_unit", unit.id, {"fields": sorted(changes)})
    link = _current_region_links(db, {unit.id}).get(unit.id)
    return UnitWriteResponse(message="Unit updated.", data=_unit_read(unit, link, _region_map(db)))


@router.put("/units/{unit_id}/region", response_model=UnitWriteResponse, dependencies=[admin_only])
def assign_unit_region(unit_id: str, payload: UnitRegionAssign, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """Moves a unit to a region (or out of every region) from a date; earlier history is kept."""
    unit = db.get(PerformanceUnit, unit_id)
    if unit is None:
        raise _not_found("Unit")
    if payload.region_id is not None and db.get(Region, payload.region_id) is None:
        raise HTTPException(status_code=400, detail="That region doesn't exist.")
    start = payload.effective_from or today_nairobi()
    open_link = db.query(UnitRegionLink).filter(UnitRegionLink.unit_id == unit.id, UnitRegionLink.effective_to.is_(None)).first()
    if open_link is not None:
        if open_link.effective_from >= start:
            raise HTTPException(status_code=400, detail="The move must take effect after the unit's current region started.")
        open_link.effective_to = start - timedelta(days=1)
    if payload.region_id is not None:
        db.add(UnitRegionLink(unit_id=unit.id, region_id=payload.region_id, effective_from=start))
    db.commit()
    log_audit(db, current_admin.username, "unit_region_changed", "performance_unit", unit.id, {"region_id": payload.region_id, "from": start.isoformat()})
    link = _current_region_links(db, {unit.id}).get(unit.id)
    return UnitWriteResponse(message="Region updated.", data=_unit_read(unit, link, _region_map(db)))


@router.get("/regions", response_model=RegionListResponse, dependencies=[manage_access])
def list_regions(include_inactive: bool = False, db: Session = Depends(get_db)):
    query = db.query(Region)
    if not include_inactive:
        query = query.filter(Region.is_active.is_(True))
    return RegionListResponse(items=[_region_read(db, r) for r in query.order_by(Region.name.asc()).all()])


@router.post("/regions", response_model=RegionWriteResponse, status_code=201, dependencies=[admin_only])
def create_region(payload: RegionCreate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    if db.query(Region).filter(Region.name == payload.name.strip()).first() is not None:
        raise HTTPException(status_code=409, detail="A region with that name already exists.")
    _require_profile_exists(db, payload.manager_profile_id, "manager")
    region = Region(name=payload.name.strip(), manager_profile_id=payload.manager_profile_id)
    db.add(region)
    db.commit()
    db.refresh(region)
    log_audit(db, current_admin.username, "region_created", "region", region.id, {"name": region.name})
    return RegionWriteResponse(message="Region created.", data=_region_read(db, region))


@router.patch("/regions/{region_id}", response_model=RegionWriteResponse, dependencies=[admin_only])
def update_region(region_id: str, payload: RegionUpdate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    region = db.get(Region, region_id)
    if region is None:
        raise _not_found("Region")
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("name") is None:
        changes.pop("name", None)
    if "name" in changes:
        changes["name"] = changes["name"].strip()
        if db.query(Region).filter(Region.name == changes["name"], Region.id != region.id).first() is not None:
            raise HTTPException(status_code=409, detail="A region with that name already exists.")
    _require_profile_exists(db, changes.get("manager_profile_id"), "manager")
    for field, value in changes.items():
        setattr(region, field, value)
    db.commit()
    db.refresh(region)
    log_audit(db, current_admin.username, "region_updated", "region", region.id, {"fields": sorted(changes)})
    return RegionWriteResponse(message="Region updated.", data=_region_read(db, region))


# --------------------------------------------------------------------------
# Staff profiles
# --------------------------------------------------------------------------


@router.get("/staff", response_model=StaffProfileListResponse, dependencies=[manage_access])
def list_staff(
    unit_id: str | None = None,
    region_id: str | None = None,
    role_id: str | None = None,
    manager_id: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = None,
    include_inactive: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
):
    scope = get_performance_scope(current_admin, db)
    query = db.query(StaffProfile)
    if scope.visible_ids is not None:
        if not scope.visible_ids:
            return StaffProfileListResponse(items=[], total=0)
        query = query.filter(StaffProfile.id.in_(scope.visible_ids))
    if not include_inactive:
        query = query.filter(StaffProfile.is_active.is_(True))
    if unit_id:
        query = query.filter(StaffProfile.unit_id == unit_id)
    if region_id:
        today = today_nairobi()
        in_region = db.query(UnitRegionLink.unit_id).filter(
            UnitRegionLink.region_id == region_id,
            UnitRegionLink.effective_from <= today,
            or_(UnitRegionLink.effective_to.is_(None), UnitRegionLink.effective_to >= today),
        )
        query = query.filter(StaffProfile.unit_id.in_(in_region))
    if role_id:
        query = query.filter(StaffProfile.performance_role_id == role_id)
    if manager_id:
        query = query.filter(StaffProfile.manager_id == manager_id)
    if status_filter:
        query = _status_filter(query, scope, status_filter)
    if q:
        query = query.filter(StaffProfile.full_name.ilike(f"%{q.strip()}%"))
    total = query.count()
    rows = query.order_by(StaffProfile.full_name.asc()).offset(offset).limit(limit).all()
    return StaffProfileListResponse(items=_profiles_read(db, scope, rows), total=total)


@router.get("/staff/linkable-users", response_model=LinkableUsersResponse, dependencies=[admin_or_hr])
def linkable_users(db: Session = Depends(get_db)):
    """Active dashboard accounts not yet linked to a staff profile (admin/hr only)."""
    linked = {uid for (uid,) in db.query(StaffProfile.admin_user_id).filter(StaffProfile.admin_user_id.is_not(None)).all()}
    users = db.query(AdminUser).filter(AdminUser.is_active.is_(True)).order_by(AdminUser.username.asc()).all()
    return LinkableUsersResponse(
        items=[LinkableUser(id=u.id, username=u.username, role=u.role, branch_id=u.branch_id) for u in users if u.id not in linked]
    )


@router.get("/staff/{profile_id}", response_model=StaffProfileRead, dependencies=[manage_access])
def get_staff(profile_id: str, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    return _profiles_read(db, scope, [_profile_in_scope(db, scope, profile_id)])[0]


@router.post("/staff", response_model=StaffProfileWriteResponse, status_code=201, dependencies=[admin_or_hr])
def create_staff(payload: StaffProfileCreate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    if payload.is_executive and not scope.is_admin:
        raise HTTPException(status_code=403, detail="Only an administrator can grant the executive view.")
    if payload.status == "pip" and not scope.is_hr:
        raise HTTPException(status_code=403, detail="Only HR can place someone on a PIP.")
    _validate_links(db, role_id=payload.performance_role_id, unit_id=payload.unit_id, manager_id=payload.manager_id, admin_user_id=payload.admin_user_id)
    _validate_level(db, payload.level_id, payload.performance_role_id)
    data = payload.model_dump(exclude={"login"})
    if payload.login is not None:
        if not scope.is_admin:
            raise HTTPException(status_code=403, detail="Only an administrator can create a login.")
        if payload.admin_user_id:
            raise HTTPException(status_code=422, detail="Choose an existing login or create a new one, not both.")
        from app.services.auth import hash_password
        from pydantic import TypeAdapter, ValidationError

        from app.schemas.admin_user import AdminRole

        try:
            TypeAdapter(AdminRole).validate_python(payload.login.role)
            valid = payload.login.role != "admin"  # administrator logins are made from Admin Users
        except ValidationError:
            valid = False
        if not valid:
            raise HTTPException(status_code=422, detail="That isn't a valid role.")
        if db.query(AdminUser).filter(AdminUser.username == payload.login.username).first():
            raise HTTPException(status_code=409, detail=f'A login with username "{payload.login.username}" already exists.')
        new_user = AdminUser(username=payload.login.username, password_hash=hash_password(payload.login.password), role=payload.login.role, email=payload.login.email)
        db.add(new_user)
        db.flush()
        data["admin_user_id"] = new_user.id
    profile = StaffProfile(**data)
    db.add(profile)
    db.flush()
    _record_assignment(db, profile, current_admin.username, "Profile created", profile.hire_date)
    db.commit()
    db.refresh(profile)
    log_audit(db, current_admin.username, "staff_created", "staff_profile", profile.id, None)
    return StaffProfileWriteResponse(message="Staff profile created.", data=_profiles_read(db, scope, [profile])[0])


@router.patch("/staff/{profile_id}", response_model=StaffProfileWriteResponse, dependencies=[admin_or_hr])
def update_staff(profile_id: str, payload: StaffProfileUpdate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    profile = _profile_in_scope(db, scope, profile_id)
    changes = payload.model_dump(exclude_unset=True)
    reason = changes.pop("change_reason", None)
    for required in ("performance_role_id", "full_name", "status", "is_executive", "is_active"):
        if changes.get(required, "") is None:
            changes.pop(required)  # these can't be cleared
    if "is_executive" in changes and not scope.is_admin:
        raise HTTPException(status_code=403, detail="Only an administrator can grant the executive view.")
    if "status" in changes and "pip" in (profile.status, changes["status"]) and changes["status"] != profile.status:
        if not scope.can_see_restricted(profile):
            raise HTTPException(status_code=403, detail="Only HR or the direct manager can change PIP status.")
    _validate_links(
        db,
        role_id=changes.get("performance_role_id"),
        unit_id=changes.get("unit_id"),
        manager_id=changes.get("manager_id"),
        admin_user_id=changes.get("admin_user_id"),
        this_profile_id=profile.id,
    )
    # A level belongs to a role: changing the role without choosing a level of the new role clears the old level.
    new_role = changes.get("performance_role_id", profile.performance_role_id)
    if "level_id" not in changes and new_role != profile.performance_role_id:
        changes["level_id"] = None
    _validate_level(db, changes.get("level_id", profile.level_id), new_role)
    before = (profile.unit_id, profile.manager_id, profile.performance_role_id)
    for field, value in changes.items():
        setattr(profile, field, value)
    if changes.get("status") == "exited":
        profile.is_active = False
    if (profile.unit_id, profile.manager_id, profile.performance_role_id) != before:
        _record_assignment(db, profile, current_admin.username, reason)
    db.commit()
    db.refresh(profile)
    log_audit(db, current_admin.username, "staff_updated", "staff_profile", profile.id, {"fields": sorted(changes)})
    return StaffProfileWriteResponse(message="Staff profile updated.", data=_profiles_read(db, scope, [profile])[0])


@router.get("/staff/{profile_id}/history", response_model=AssignmentListResponse, dependencies=[manage_access])
def staff_history(profile_id: str, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    profile = _profile_in_scope(db, scope, profile_id)
    rows = (
        db.query(StaffAssignmentHistory)
        .filter(StaffAssignmentHistory.profile_id == profile.id)
        .order_by(StaffAssignmentHistory.effective_from.desc(), StaffAssignmentHistory.created_at.desc())
        .all()
    )
    return AssignmentListResponse(items=[AssignmentRead.model_validate(r) for r in rows])


# --------------------------------------------------------------------------
# Actions (talk / coaching / PIP / ...)
# --------------------------------------------------------------------------


@router.get("/staff/{profile_id}/actions", response_model=ActionListResponse, dependencies=[manage_access])
def list_actions(profile_id: str, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """Restricted actions (PIP, warnings) appear only for HR, the direct manager and the person concerned."""
    scope = get_performance_scope(current_admin, db)
    profile = _profile_in_scope(db, scope, profile_id)
    query = db.query(PerformanceAction).filter(PerformanceAction.profile_id == profile.id)
    if not scope.can_see_restricted(profile):
        query = query.filter(PerformanceAction.restricted.is_(False))
    rows = query.order_by(PerformanceAction.started_on.desc(), PerformanceAction.created_at.desc()).all()
    return ActionListResponse(items=[ActionRead.model_validate(r) for r in rows])


@router.post("/staff/{profile_id}/actions", response_model=ActionWriteResponse, status_code=201, dependencies=[manage_access])
def create_action(profile_id: str, payload: ActionCreate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    profile = _profile_in_scope(db, scope, profile_id)
    restricted = payload.action_type in RESTRICTED_ACTION_TYPES
    is_direct_manager = scope.own_profile_id is not None and profile.manager_id == scope.own_profile_id
    # Restricted actions: HR or the direct manager only (not admin, not executives).
    # Everything else: additionally admin. The read-only executive view never writes.
    allowed = (scope.is_hr or is_direct_manager) if restricted else (scope.is_hr or scope.is_admin or is_direct_manager)
    if scope.read_only or not allowed:
        raise HTTPException(status_code=403, detail="You don't have permission to record that action.")
    action = PerformanceAction(
        profile_id=profile.id,
        action_type=payload.action_type,
        restricted=restricted,
        started_on=payload.started_on,
        review_on=payload.review_on,
        ends_on=payload.ends_on,
        outcome=payload.outcome,
        note=payload.note,
        raised_by_username=current_admin.username,
    )
    db.add(action)
    if payload.action_type in ("pip", "pip_extended"):
        profile.status = "pip"
    elif payload.action_type == "pip_lifted" and profile.status == "pip":
        profile.status = "active"
    db.commit()
    db.refresh(action)
    # Ids and the action type only - never the note text.
    log_audit(db, current_admin.username, "action_created", "performance_action", action.id, {"profile_id": profile.id, "type": action.action_type})
    return ActionWriteResponse(message="Action recorded.", data=ActionRead.model_validate(action))


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------


@router.get("/audit", response_model=AuditListResponse, dependencies=[admin_only])
def list_audit(limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db)):
    rows = db.query(PerformanceAuditLog).order_by(PerformanceAuditLog.created_at.desc()).limit(limit).all()
    return AuditListResponse(items=[AuditEntry.model_validate(r) for r in rows])
