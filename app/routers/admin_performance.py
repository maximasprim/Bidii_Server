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
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import PerformancePermissionGrant
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
from app.services import performance_permissions as perm
from app.services.performance_permissions import needs
from app.services.role_permissions import CONFIGURABLE_ROLES, get_all_effective_permissions, get_effective_menus_for_role, require_menu_access

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
        parent_unit_id=unit.parent_unit_id,
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


@router.put("/config", response_model=ConfigUpdateResponse, dependencies=[needs("settings.edit")])
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


@router.post("/scales", response_model=ScaleWriteResponse, status_code=201, dependencies=[needs("settings.edit")])
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


@router.post("/scales/preview", response_model=ScalePreviewResponse, dependencies=[needs("settings.edit")])
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


@router.post("/roles", response_model=PerformanceRoleWriteResponse, status_code=201, dependencies=[needs("roles.edit")])
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


@router.patch("/roles/{role_id}", response_model=PerformanceRoleWriteResponse, dependencies=[needs("roles.edit")])
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


@router.delete("/roles/{role_id}", response_model=PerformanceRoleWriteResponse, dependencies=[needs("roles.edit")])
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


@router.post("/units", response_model=UnitWriteResponse, status_code=201, dependencies=[needs("structure.edit")])
def create_unit(payload: UnitCreate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    if db.query(PerformanceUnit).filter(PerformanceUnit.name == payload.name.strip()).first() is not None:
        raise HTTPException(status_code=409, detail="A unit with that name already exists.")
    _check_unit_links(db, payload.branch_id, payload.manager_profile_id)
    parent = None
    if payload.parent_unit_id is not None:
        parent = db.get(PerformanceUnit, payload.parent_unit_id)
        if parent is None or not parent.is_active:
            raise HTTPException(status_code=400, detail="That parent unit doesn't exist or is closed.")
        if parent.parent_unit_id is not None:
            raise HTTPException(status_code=400, detail="A unit can only sit one level under a branch.")
    unit = PerformanceUnit(name=payload.name.strip(), kind=payload.kind, branch_id=payload.branch_id, manager_profile_id=payload.manager_profile_id,
                           parent_unit_id=parent.id if parent else None)
    db.add(unit)
    db.flush()
    if parent is not None:  # in the same region as the branch it sits under
        link = db.query(UnitRegionLink).filter(UnitRegionLink.unit_id == parent.id, UnitRegionLink.effective_to.is_(None)).first()
        if link is not None:
            db.add(UnitRegionLink(unit_id=unit.id, region_id=link.region_id, effective_from=link.effective_from))
    db.commit()
    db.refresh(unit)
    log_audit(db, current_admin.username, "unit_created", "performance_unit", unit.id, {"name": unit.name})
    return UnitWriteResponse(message="Unit created.", data=_unit_read(unit, None, {}))


@router.patch("/units/{unit_id}", response_model=UnitWriteResponse, dependencies=[needs("structure.edit")])
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


@router.put("/units/{unit_id}/region", response_model=UnitWriteResponse, dependencies=[needs("structure.edit")])
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
    from app.services.performance_units import follow_parent_region

    follow_parent_region(db, unit, payload.region_id, start)  # what sits under a branch moves with it
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


@router.post("/regions", response_model=RegionWriteResponse, status_code=201, dependencies=[needs("structure.edit")])
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


@router.patch("/regions/{region_id}", response_model=RegionWriteResponse, dependencies=[needs("structure.edit")])
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


@router.get("/staff/linkable-users", response_model=LinkableUsersResponse, dependencies=[needs("staff.manage", org_wide=True)])
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


# What a line manager (staff.manage switched on for them, but not for their role) may and may not do. Everything else follows the normal rules.
_LIMITED_EDIT_BLOCKED = ("admin_user_id", "performance_role_id", "status", "is_active", "is_executive")


def _team_ids(scope: PerformanceScope) -> set[str]:
    return set(scope.visible_ids or ()) | ({scope.own_profile_id} if scope.own_profile_id else set())


def _limited_create(scope: PerformanceScope, payload: StaffProfileCreate) -> None:
    if payload.admin_user_id or payload.login is not None:
        raise HTTPException(status_code=403, detail="Linking or creating a login needs HR or an administrator.")
    if payload.status not in ("active", "probation"):
        raise HTTPException(status_code=403, detail="You can only add someone as active or on probation.")
    in_unit = payload.unit_id is not None and payload.unit_id in scope.managed_unit_ids
    under_me = payload.manager_id is not None and payload.manager_id in _team_ids(scope)
    if payload.unit_id is not None and not in_unit:
        raise HTTPException(status_code=403, detail="You can only add people to a unit you manage.")
    if payload.manager_id is not None and not under_me:
        raise HTTPException(status_code=403, detail="The manager must be you or someone in your team.")
    if not (in_unit or under_me):
        raise HTTPException(status_code=403, detail="Choose a unit you manage or a manager in your team.")


def _limited_update(scope: PerformanceScope, profile: StaffProfile, changes: dict[str, Any]) -> None:
    if profile.id == scope.own_profile_id:
        raise HTTPException(status_code=403, detail="You can't edit your own profile.")
    # the edit form re-sends every field, so only a field that actually CHANGES counts
    blocked = sorted(k for k in _LIMITED_EDIT_BLOCKED if k in changes and changes[k] != getattr(profile, k))
    if blocked:
        raise HTTPException(status_code=403, detail=f"Changing {', '.join(blocked)} needs HR or an administrator.")
    if "unit_id" in changes and changes["unit_id"] != profile.unit_id and changes["unit_id"] not in scope.managed_unit_ids:
        raise HTTPException(status_code=403, detail="You can only move someone to a unit you manage.")
    if "manager_id" in changes and changes["manager_id"] != profile.manager_id and changes["manager_id"] not in _team_ids(scope):
        raise HTTPException(status_code=403, detail="The manager must be you or someone in your team.")


@router.post("/staff", response_model=StaffProfileWriteResponse, status_code=201, dependencies=[needs("staff.manage")])
def create_staff(payload: StaffProfileCreate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    if payload.is_executive and not scope.is_admin:
        raise HTTPException(status_code=403, detail="Only an administrator can grant the executive view.")
    if payload.status == "pip" and not scope.is_hr:
        raise HTTPException(status_code=403, detail="Only HR can place someone on a PIP.")
    if perm.is_limited(db, current_admin, "staff.manage"):
        _limited_create(scope, payload)
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


@router.patch("/staff/{profile_id}", response_model=StaffProfileWriteResponse, dependencies=[needs("staff.manage")])
def update_staff(profile_id: str, payload: StaffProfileUpdate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    profile = _profile_in_scope(db, scope, profile_id)
    changes = payload.model_dump(exclude_unset=True)
    reason = changes.pop("change_reason", None)
    if perm.is_limited(db, current_admin, "staff.manage"):
        _limited_update(scope, profile, changes)
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


@router.get("/audit", response_model=AuditListResponse, dependencies=[needs("audit.view")])
def list_audit(limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db)):
    rows = db.query(PerformanceAuditLog).order_by(PerformanceAuditLog.created_at.desc()).limit(limit).all()
    return AuditListResponse(items=[AuditEntry.model_validate(r) for r in rows])


# --------------------------------------------------------------------------
# Who may do what - the Roles & Permissions screen
#
# Every action that used to be "administrators only" or "administrators and HR" is a named permission (performance_permissions.py).
# Administrators set, per role, which permissions are on. Only changes from the built-in defaults are stored, and "admin" can never be
# restricted. These routes themselves stay administrators-only, whatever is configured.
# --------------------------------------------------------------------------


class PermissionSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(min_length=1, max_length=40)
    capability: str = Field(min_length=1, max_length=60)
    allowed: bool
    confirm_sensitive: bool = False


class PermissionReset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str | None = Field(default=None, max_length=40)


def _permission_cell(role: str, cap: perm.Capability, over: dict[str, bool], menus: set[str]) -> dict[str, Any]:
    if role == perm.LINE_MANAGER:
        if not cap.manager_scope:  # an organisation-wide action: it can't be limited to a team, so it can't be given to line managers
            return {"allowed": False, "default": False, "overridden": False, "menu_missing": False, "locked": True}
        return {"allowed": over.get(cap.key, False), "default": False, "overridden": cap.key in over, "menu_missing": False, "locked": False}
    default = perm.default_allowed(role, cap.key)
    allowed = over.get(cap.key, default)
    # menu_missing: allowed, but the role cannot open the page this belongs to (it also needs that menu on the same screen)
    return {"allowed": allowed, "default": default, "overridden": cap.key in over, "menu_missing": bool(allowed and cap.menu not in menus), "locked": False}


@router.get("/permissions/me")
def my_performance_permissions(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    """
    The permissions the signed-in person has right now. The pages use it to show or hide buttons; the server still checks every request.
    `team_only` lists the ones they hold as a line manager, which work for their own team only.
    """
    caps = perm.effective_for_role(db, admin.role)
    team_only: set[str] = set()
    if admin.role != perm.ADMIN and perm.is_line_manager(get_performance_scope(admin, db)):
        team_only = perm.line_manager_keys(db) - caps
    return {"role": admin.role, "capabilities": sorted(caps | team_only), "team_only": sorted(team_only)}


@router.get("/permissions", dependencies=[admin_only])
def permission_matrix(db: Session = Depends(get_db)) -> dict[str, Any]:
    roles = [perm.LINE_MANAGER, *CONFIGURABLE_ROLES]  # "line managers" first: the team-only column
    over: dict[str, dict[str, bool]] = {r: {} for r in roles}
    for row in db.query(PerformancePermissionGrant):
        if row.role in over:
            over[row.role][row.capability] = bool(row.allowed)
    # every role's menus in one query (it used to be one query per role); same answer as get_effective_menus_for_role for each
    by_role = {p.role: set(p.allowed_menus) for p in get_all_effective_permissions(db)}
    menus = {r: by_role.get(r, set()) for r in roles if r != perm.LINE_MANAGER}
    menus[perm.LINE_MANAGER] = set()
    return {
        "roles": roles,
        "groups": list(perm.GROUPS),
        "capabilities": [
            {
                "key": c.key, "label": c.label, "description": c.description, "group": c.group, "sensitive": c.sensitive, "menu": c.menu,
                "default_roles": list(c.default_roles),
                "cells": {r: _permission_cell(r, c, over[r], menus[r]) for r in roles},
            }
            for c in perm.CAPABILITIES
        ],
    }


@router.put("/permissions", dependencies=[admin_only])
def set_permission(body: PermissionSet, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.role == perm.ADMIN:
        raise HTTPException(status_code=422, detail="Administrators always have every permission.")
    if body.role not in CONFIGURABLE_ROLES and body.role != perm.LINE_MANAGER:
        raise HTTPException(status_code=422, detail="Unknown role.")
    cap = perm.BY_KEY.get(body.capability)
    if cap is None:
        raise HTTPException(status_code=422, detail="Unknown permission.")
    if body.role == perm.LINE_MANAGER and not cap.manager_scope:
        raise HTTPException(status_code=422, detail="This covers the whole organisation, so it can't be limited to a team.")
    default = perm.default_allowed(body.role, cap.key)
    if body.allowed and cap.sensitive and not default and not body.confirm_sensitive:
        raise HTTPException(status_code=422, detail="This is a sensitive permission. Confirm to grant it.")
    row = db.get(PerformancePermissionGrant, (body.role, cap.key))
    if body.allowed == default:  # back to the default: keep nothing
        if row is not None:
            db.delete(row)
    elif row is not None:
        row.allowed, row.updated_by, row.updated_at = body.allowed, admin.username, datetime.now(timezone.utc)
    else:
        db.add(PerformancePermissionGrant(role=body.role, capability=cap.key, allowed=body.allowed, updated_by=admin.username))
    db.commit()
    log_audit(db, admin.username, "permissions.change", "permission", f"{body.role}:{cap.key}", {"allowed": body.allowed, "default": default})
    menus = set() if body.role == perm.LINE_MANAGER else set(get_effective_menus_for_role(db, body.role))
    cell = _permission_cell(body.role, cap, perm.overrides_for_role(db, body.role), menus)
    return {"role": body.role, "capability": cap.key, "cell": cell}


@router.post("/permissions/reset", dependencies=[admin_only])
def reset_permissions(body: PermissionReset, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Put one role (or every role) back to the built-in defaults."""
    q = db.query(PerformancePermissionGrant)
    if body.role is not None:
        if body.role not in CONFIGURABLE_ROLES and body.role != perm.LINE_MANAGER:
            raise HTTPException(status_code=422, detail="Unknown role.")
        q = q.filter(PerformancePermissionGrant.role == body.role)
    removed = q.delete(synchronize_session=False)
    db.commit()
    log_audit(db, admin.username, "permissions.reset", "permission", body.role or "all", {"removed": removed})
    return {"removed": removed}
