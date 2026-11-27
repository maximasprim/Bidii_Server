"""
Staff Performance - organisation structure: the reporting tree, structure health, first-time setup.

Prefix: /api/admin/performance
  org-tree            menu "/admin/performance", scope-filtered (you only ever see your own part of the tree)
  org-structure       same visibility as org-tree: flat people/units/regions for the Structure view
  org-health          admin or hr
  org-setup/*         admin only (writes units/regions; READS the public branches table, never changes it)
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.routers.admin_performance import admin_only, admin_or_hr, manage_access
from app.schemas.performance import (
    OrgHealthResponse,
    OrgTreeResponse,
    SetupApplyRequest,
    SetupApplyResponse,
    SetupPreview,
)
from app.services import performance_org as org
from app.services import performance_placement as placement
from app.services import performance_units as units_svc
from app.models.performance import PerformanceUnit, StaffProfile
from app.services import performance_coverage as coverage
from app.services.auth import get_current_admin
from app.services.performance_config import log_audit
from app.services.performance_scope import get_performance_scope
from app.services.performance_permissions import is_limited, needs

router = APIRouter(prefix="/api/admin/performance", tags=["admin-performance-org"])


@router.get("/org-tree", response_model=OrgTreeResponse, dependencies=[manage_access])
def org_tree(include_inactive: bool = False, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """The reporting tree limited to the people the caller may see. A PIP never shows: it appears as "active"."""
    scope = get_performance_scope(current_admin, db)
    roots, total = org.build_tree(db, scope, include_inactive)
    return OrgTreeResponse(roots=roots, total=total)


@router.get("/org-structure", dependencies=[manage_access])
def org_structure(include_inactive: bool = False, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """Flat people / units / regions for the chain-of-command views, limited to the people the caller may see."""
    scope = get_performance_scope(current_admin, db)
    return org.build_structure(db, scope, include_inactive)


@router.post("/org-setup/debt-collection-units", dependencies=[needs("orgsetup.manage")])
def add_debt_collection_units(db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """Gives every branch its own Debt Collection unit (under the branch, in its region). Safe to run again."""
    result = units_svc.ensure_debt_collection_units(db)
    log_audit(db, current_admin.username, "debt_collection_units_added", "performance_unit", None, result)
    return result


class CoverageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit_ids: list[str] = Field(default_factory=list, max_length=coverage.MAX_EXTRA_UNITS)


@router.get("/staff/{profile_id}/coverage", dependencies=[needs("org.coverage")])
def get_staff_coverage(profile_id: str, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """The extra units someone works in besides their main one (a debt collector covering several branches)."""
    profile = db.get(StaffProfile, profile_id)
    if profile is None or (is_limited(db, current_admin, "org.coverage") and not get_performance_scope(current_admin, db).can_see(profile.id)):
        raise HTTPException(status_code=404, detail="Staff profile not found.")
    return coverage.read(db, profile)


@router.put("/staff/{profile_id}/coverage", dependencies=[needs("org.coverage")])
def set_staff_coverage(profile_id: str, payload: CoverageBody, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    profile = db.get(StaffProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Staff profile not found.")
    unit_ids = payload.unit_ids
    if is_limited(db, current_admin, "org.coverage"):  # a line manager: their team only, and only units they manage
        scope = get_performance_scope(current_admin, db)
        if not scope.can_see(profile.id):
            raise HTTPException(status_code=404, detail="Staff profile not found.")
        if profile.id == scope.own_profile_id:
            raise HTTPException(status_code=403, detail="You can't change your own coverage.")
        if any(u not in scope.managed_unit_ids for u in unit_ids):
            raise HTTPException(status_code=403, detail="You can only give coverage of units you manage.")
        # coverage of units they do not manage was set by someone else: leave it exactly as it is
        unit_ids = [*unit_ids, *[u["id"] for u in coverage.read(db, profile)["units"] if u["id"] not in scope.managed_unit_ids]]
    try:
        result = coverage.replace(db, profile, unit_ids, current_admin.username)
    except coverage.CoverageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(db, current_admin.username, "staff_coverage_set", "staff_profile", profile.id, {"units": [u["id"] for u in result["units"]]})
    return result


@router.get("/org-health", response_model=OrgHealthResponse, dependencies=[needs("org.health")])
def org_health(db: Session = Depends(get_db)):
    return org.org_health(db)


@router.get("/org-setup/preview", response_model=SetupPreview, dependencies=[needs("orgsetup.manage")])
def setup_preview(db: Session = Depends(get_db)):
    """A proposal only: units for every public branch, the extra teams, and the three regions from the workbook."""
    return org.setup_preview(db)


@router.post("/org-setup/apply", response_model=SetupApplyResponse, dependencies=[needs("orgsetup.manage")])
def setup_apply(payload: SetupApplyRequest, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    try:
        result = org.apply_setup(db, [r.model_dump() for r in payload.regions], payload.effective_from, current_admin.username)
    except org.SetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(db, current_admin.username, "org_setup_applied", "organisation", None, {
        "units_created": result.units_created, "regions_created": result.regions_created, "links_created": result.links_created})
    return SetupApplyResponse(units_created=result.units_created, units_linked=result.units_linked, regions_created=result.regions_created,
                              links_created=result.links_created, conflicts=result.conflicts or [])


class PlacementItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(max_length=36)
    unit_id: str | None = Field(default=None, max_length=36)
    manager_id: str | None = Field(default=None, max_length=36)


class PlacementApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[PlacementItem] = Field(min_length=1, max_length=2000)
    effective_from: date | None = None
    confirm: bool


@router.get("/org-setup/layout", dependencies=[needs("org.placement")])
def org_layout(db: Session = Depends(get_db)):
    """The structure as it is now: regions with their units, units without a region, public branches without a unit."""
    return placement.layout(db)


@router.get("/org-setup/placement", dependencies=[needs("org.placement")])
def placement_preview(db: Session = Depends(get_db)):
    """A proposal only: where people with no unit or no line manager probably belong, and why."""
    return placement.preview(db)


@router.post("/org-setup/placement/apply", dependencies=[needs("org.placement")])
def placement_apply(payload: PlacementApply, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    if not payload.confirm:
        raise HTTPException(status_code=422, detail="Confirm to continue.")
    try:
        result = placement.apply(db, [i.model_dump() for i in payload.items], current_admin.username, payload.effective_from)
    except placement.PlacementError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(db, current_admin.username, "org_placement_applied", "organisation", None, result)
    return result


class LinkBranch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    branch_id: str | None = Field(default=None, max_length=36)
    use_branch_name: bool = False
    confirm: bool


class CleanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep_id: str = Field(max_length=36)
    remove_id: str = Field(max_length=36)
    link: bool = False
    use_name: bool = False


class CleanUp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[CleanItem] = Field(min_length=1, max_length=200)
    confirm: bool


@router.get("/org-setup/duplicates", dependencies=[needs("orgsetup.manage")])
def unit_duplicates(db: Session = Depends(get_db)):
    """Empty units that look like a second copy of a unit that has people (for example one made by setup next to an imported one)."""
    return {"pairs": units_svc.duplicates(db)}


@router.post("/org-setup/duplicates/clean-up", dependencies=[needs("orgsetup.manage")])
def unit_clean_up(payload: CleanUp, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    if not payload.confirm:
        raise HTTPException(status_code=422, detail="Confirm to continue.")
    try:
        result = units_svc.clean_up(db, [i.model_dump() for i in payload.items])
    except units_svc.UnitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(db, current_admin.username, "org_units_cleaned_up", "organisation", None, result)
    return result


@router.post("/org-setup/units/{unit_id}/link-branch", dependencies=[needs("orgsetup.manage")])
def link_unit_branch(unit_id: str, payload: LinkBranch, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    if not payload.confirm:
        raise HTTPException(status_code=422, detail="Confirm to continue.")
    unit = db.get(PerformanceUnit, unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="That unit wasn't found.")
    try:
        result = units_svc.link_to_branch(db, unit, payload.branch_id, payload.use_branch_name)
    except units_svc.UnitError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(db, current_admin.username, "unit_linked_to_branch", "performance_unit", unit_id, {"branch_id": payload.branch_id})
    return result


@router.delete("/org-setup/units/{unit_id}", dependencies=[needs("orgsetup.manage")])
def remove_empty_unit(unit_id: str, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """Removes a unit nothing refers to (no people, history, snapshots or cascade targets)."""
    unit = db.get(PerformanceUnit, unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="That unit wasn't found.")
    name = unit.name
    try:
        units_svc.delete_if_empty(db, unit)
        db.commit()
    except units_svc.UnitError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    log_audit(db, current_admin.username, "unit_removed", "performance_unit", unit_id, {"name": name})
    return {"message": "Removed."}


class CloseUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    move_people_to: str | None = Field(default=None, max_length=36)
    confirm: bool


@router.post("/org-setup/units/{unit_id}/close", dependencies=[needs("orgsetup.manage")])
def close_unit(unit_id: str, payload: CloseUnit, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """Closes a branch/team that no longer operates. History is kept; its people move to another unit (or none)."""
    if not payload.confirm:
        raise HTTPException(status_code=422, detail="Confirm to continue.")
    unit = db.get(PerformanceUnit, unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="That unit wasn't found.")
    try:
        result = units_svc.close_unit(db, unit, payload.move_people_to, current_admin.username)
    except units_svc.UnitError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit(db, current_admin.username, "unit_closed", "performance_unit", unit_id, {"people_moved": result["people_moved"], "to": payload.move_people_to})
    return result
