"""
Where everyone sits in the organisation. Reads the structure (regions, units, public branches) and proposes, for people who have no
unit or no line manager yet, where they belong; nothing is written until an administrator or HR accepts rows. The proposal is only
ever a suggestion built from facts already in the system:

  unit     the branch on the person's login -> that branch's unit, or its Debt Collection unit for a debt collector; else head-office
           style roles (and the Debt Collection Manager) -> Head Office, check-off agents -> Check-off
  manager  the unit's manager if one is set, else the one branch manager in that unit; a branch manager's manager is their region's
           manager; a debt collector's manager is the Debt Collection Manager, whose manager is the General Manager. Never the person
           themselves, and never a loop.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.models.admin_user import AdminUser
from app.models.branch import Branch
from app.models.performance import PerformanceRole, PerformanceUnit, Region, StaffAssignmentHistory, StaffProfile, UnitRegionLink
from app.services.performance_time import today_nairobi

LIVE = ("active", "probation", "pip")
HEAD_OFFICE_ROLES = {"hr_officer", "head_office_staff", "marketing_manager", "customer_service_manager", "general_manager", "ceo", "credit_risk_analyst"}
TEAM_BY_ROLE = {"check_off_agent": "Check-off"}  # debt collection is not one team: each branch has its own Debt Collection unit
DEBT_COLLECTOR_KEY = "debt_recovery"
GENERAL_MANAGER_KEY = "general_manager"
_DCM_NAME = re.compile(r"debt\s+(collection|collector|collectors|recovery)\s+manager", re.I)


def is_debt_manager(role: PerformanceRole | None) -> bool:
    """The Debt Collection Manager: found by the role's key or name, so a role you add yourself is recognised."""
    if role is None:
        return False
    return role.key in {"debt_collection_manager", "debt_collector_manager", "debt_recovery_manager"} or bool(_DCM_NAME.search(role.name or ""))


class PlacementError(ValueError):
    pass


def _region_of(db: Session) -> dict[str, str]:
    return {u: r for u, r in db.query(UnitRegionLink.unit_id, UnitRegionLink.region_id).filter(UnitRegionLink.effective_to.is_(None))}


def layout(db: Session) -> dict[str, Any]:
    all_units = db.query(PerformanceUnit).filter(PerformanceUnit.is_active.is_(True)).order_by(PerformanceUnit.name).all()
    kids_of: dict[str, list[PerformanceUnit]] = {}
    for u in all_units:
        if u.parent_unit_id:
            kids_of.setdefault(u.parent_unit_id, []).append(u)
    units = [u for u in all_units if not u.parent_unit_id]  # units under a branch are shown with it, not as units of their own
    region_of = _region_of(db)
    staff_in = {}
    for uid in [p.unit_id for p in db.query(StaffProfile.unit_id).filter(StaffProfile.status.in_(LIVE), StaffProfile.is_active.is_(True), StaffProfile.unit_id.isnot(None))]:
        staff_in[uid] = staff_in.get(uid, 0) + 1
    names = {i: n for i, n in db.query(StaffProfile.id, StaffProfile.full_name)}
    branch_names = {i: n for i, n in db.query(Branch.id, Branch.name)}

    def unit_row(u: PerformanceUnit) -> dict[str, Any]:
        return {"id": u.id, "name": u.name, "kind": u.kind, "branch_id": u.branch_id, "branch_name": branch_names.get(u.branch_id), "staff": staff_in.get(u.id, 0),
                "manager_profile_id": u.manager_profile_id, "manager_name": names.get(u.manager_profile_id),
                "children": [{"id": c.id, "name": c.name, "kind": c.kind, "staff": staff_in.get(c.id, 0), "manager_profile_id": c.manager_profile_id,
                              "manager_name": names.get(c.manager_profile_id)} for c in kids_of.get(u.id, [])]}

    regions = db.query(Region).filter(Region.is_active.is_(True)).order_by(Region.name).all()
    out_regions = [{"id": r.id, "name": r.name, "manager_profile_id": r.manager_profile_id, "manager_name": names.get(r.manager_profile_id),
                    "units": [unit_row(u) for u in units if region_of.get(u.id) == r.id]} for r in regions]
    have = {u.branch_id for u in units if u.branch_id}
    unplaced = db.query(StaffProfile).filter(StaffProfile.status.in_(LIVE), StaffProfile.is_active.is_(True), StaffProfile.unit_id.is_(None)).count()
    no_manager = db.query(StaffProfile).filter(StaffProfile.status.in_(LIVE), StaffProfile.is_active.is_(True), StaffProfile.manager_id.is_(None), StaffProfile.is_executive.is_(False)).count()
    from app.services.performance_units import is_empty

    unit_of_branch = {u.branch_id: u for u in units if u.branch_id}
    all_branches = [{"id": i, "name": n, "unit_id": unit_of_branch[i].id if i in unit_of_branch else None, "unit_empty": bool(i in unit_of_branch and is_empty(db, unit_of_branch[i].id))}
                    for i, n in sorted(branch_names.items(), key=lambda x: x[1])]
    from app.services.performance_units import branches_missing_debt_unit

    return {
        "debt_units_missing": len(branches_missing_debt_unit(db)),
        "all_branches": all_branches,
        "regions": out_regions,
        "units_without_region": [unit_row(u) for u in units if u.id not in region_of],
        "branches_without_unit": [_with_suggestion(db, units, staff_in, i, n) for i, n in sorted(branch_names.items(), key=lambda x: x[1]) if i not in have],
        "people_without_unit": unplaced, "people_without_manager": no_manager,
    }


def _with_suggestion(db: Session, units: list[PerformanceUnit], staff_in: dict[str, int], branch_id: str, name: str) -> dict[str, Any]:
    """An existing, unlinked branch unit that looks like this website branch (for example 'Cbd' for 'Nairobi CBD')."""
    from app.services.performance_units import _same_place

    for u in units:
        if u.kind == "branch" and u.branch_id is None and _same_place(u.name, name):
            return {"id": branch_id, "name": name, "suggested_unit_id": u.id, "suggested_unit_name": u.name, "suggested_unit_staff": staff_in.get(u.id, 0)}
    return {"id": branch_id, "name": name, "suggested_unit_id": None, "suggested_unit_name": None, "suggested_unit_staff": 0}


def preview(db: Session) -> dict[str, Any]:
    units = {u.id: u for u in db.query(PerformanceUnit).filter(PerformanceUnit.is_active.is_(True))}
    by_name = {u.name: u for u in units.values()}
    by_branch = {u.branch_id: u for u in units.values() if u.branch_id}
    region_of = _region_of(db)
    regions = {r.id: r for r in db.query(Region).filter(Region.is_active.is_(True))}
    roles = {r.id: r for r in db.query(PerformanceRole)}
    login_branch = {i: b for i, b in db.query(AdminUser.id, AdminUser.branch_id)}
    profiles = db.query(StaffProfile).filter(StaffProfile.status.in_(LIVE), StaffProfile.is_active.is_(True)).order_by(StaffProfile.full_name).all()
    by_id = {p.id: p for p in profiles}
    names = {p.id: p.full_name for p in profiles}
    from app.services.performance_units import debt_unit_of

    debt_unit_for = {u.id: debt_unit_of(db, u.id) for u in units.values() if u.kind == "branch" and not u.parent_unit_id}
    debt_managers = [p for p in profiles if is_debt_manager(roles.get(p.performance_role_id))]
    general_managers = [p for p in profiles if (roles.get(p.performance_role_id) is not None and roles[p.performance_role_id].key == GENERAL_MANAGER_KEY)]

    # one branch manager per unit, when unambiguous
    bm: dict[str, list[str]] = {}
    for p in profiles:
        r = roles.get(p.performance_role_id)
        if r is not None and r.key == "branch_manager" and p.unit_id:
            bm.setdefault(p.unit_id, []).append(p.id)

    rows: list[dict[str, Any]] = []
    for p in profiles:
        role = roles.get(p.performance_role_id)
        unit_id, why_unit = p.unit_id, None
        if unit_id is None:
            b = login_branch.get(p.admin_user_id) if p.admin_user_id else None
            debt_unit = debt_unit_for.get(by_branch[b].id) if b and b in by_branch and role is not None and role.key == DEBT_COLLECTOR_KEY else None
            if debt_unit is not None:
                unit_id, why_unit = debt_unit.id, f"debt collector for the branch on their login ({by_branch[b].name})"
            elif b and b in by_branch:
                unit_id, why_unit = by_branch[b].id, "the branch on their login"
            elif role is not None and (role.key in HEAD_OFFICE_ROLES or is_debt_manager(role)) and "Head Office" in by_name:
                unit_id, why_unit = by_name["Head Office"].id, f"{role.name} sits at Head Office"
            elif role is not None and role.key in TEAM_BY_ROLE and TEAM_BY_ROLE[role.key] in by_name:
                unit_id, why_unit = by_name[TEAM_BY_ROLE[role.key]].id, f"{role.name} works in {TEAM_BY_ROLE[role.key]}"
        manager_id, why_mgr = p.manager_id, None
        if manager_id is None and not p.is_executive:
            unit = units.get(unit_id) if unit_id else None
            cand = None
            if is_debt_manager(role):
                if len(general_managers) == 1 and general_managers[0].id != p.id:
                    cand, why_mgr = general_managers[0].id, "the General Manager"
            elif role is not None and role.key == DEBT_COLLECTOR_KEY:
                if unit is not None and unit.manager_profile_id and unit.manager_profile_id != p.id:
                    cand, why_mgr = unit.manager_profile_id, f"manager of {unit.name}"
                elif len(debt_managers) == 1 and debt_managers[0].id != p.id:
                    cand, why_mgr = debt_managers[0].id, "the Debt Collection Manager"
            elif role is not None and role.key == "branch_manager" and unit is not None:
                reg = regions.get(region_of.get(unit.id, ""))
                if reg is not None and reg.manager_profile_id and reg.manager_profile_id != p.id:
                    cand, why_mgr = reg.manager_profile_id, f"manager of region {reg.name}"
            if cand is None and unit is not None and not is_debt_manager(role) and not (role is not None and role.key == "branch_manager"):
                # everyone else (and a debt collector with no Debt Collection Manager yet): the unit's manager, else its one branch manager
                if unit.manager_profile_id and unit.manager_profile_id != p.id:
                    cand, why_mgr = unit.manager_profile_id, f"manager of {unit.name}"
                elif len(bm.get(unit.id, [])) == 1 and bm[unit.id][0] != p.id:
                    cand, why_mgr = bm[unit.id][0], f"the branch manager of {unit.name}"
            if cand and cand in by_id and not _loops(by_id, p.id, cand):
                manager_id = cand
        changed_unit, changed_mgr = unit_id != p.unit_id, manager_id != p.manager_id
        if changed_unit or changed_mgr:
            rows.append({
                "profile_id": p.id, "name": p.full_name, "role": role.name if role else None,
                "current_unit": units[p.unit_id].name if p.unit_id in units else None, "current_manager": names.get(p.manager_id),
                "unit_id": unit_id if changed_unit else None, "unit_name": units[unit_id].name if changed_unit and unit_id in units else None, "unit_reason": why_unit,
                "manager_id": manager_id if changed_mgr else None, "manager_name": names.get(manager_id) if changed_mgr else None, "manager_reason": why_mgr,
            })
    unresolved = [{"profile_id": p.id, "name": p.full_name, "role": roles[p.performance_role_id].name if p.performance_role_id in roles else None}
                  for p in profiles if p.unit_id is None and p.id not in {r["profile_id"] for r in rows if r["unit_id"]}]
    return {"proposals": rows, "unplaced": unresolved}


def _loops(by_id: dict[str, StaffProfile], profile_id: str, manager_id: str) -> bool:
    seen: set[str] = set()
    cur: str | None = manager_id
    while cur and cur not in seen:
        if cur == profile_id:
            return True
        seen.add(cur)
        nxt = by_id.get(cur)
        cur = nxt.manager_id if nxt else None
    return False


def apply(db: Session, items: list[dict[str, Any]], username: str, effective_from: date | None = None) -> dict[str, int]:
    """Writes accepted placements (and records each one in the person's assignment history). One transaction."""
    start = effective_from or today_nairobi()
    placed = managed = 0
    all_profiles = {p.id: p for p in db.query(StaffProfile)}
    for it in items:
        p = all_profiles.get(it.get("profile_id"))
        if p is None:
            raise PlacementError("One of the people doesn't exist.")
        changed = False
        uid = it.get("unit_id")
        if uid:
            unit = db.get(PerformanceUnit, uid)
            if unit is None or not unit.is_active:
                raise PlacementError(f"{p.full_name}: that unit doesn't exist.")
            if p.unit_id != uid:
                p.unit_id, changed = uid, True
                placed += 1
        mid = it.get("manager_id")
        if mid:
            if mid == p.id or mid not in all_profiles:
                raise PlacementError(f"{p.full_name}: that isn't a valid manager.")
            if _loops(all_profiles, p.id, mid):
                raise PlacementError(f"{p.full_name}: that would create a reporting loop.")
            if p.manager_id != mid:
                p.manager_id, changed = mid, True
                managed += 1
        if changed:
            open_row = db.query(StaffAssignmentHistory).filter(StaffAssignmentHistory.profile_id == p.id, StaffAssignmentHistory.effective_to.is_(None)).first()
            if open_row is not None:
                from datetime import timedelta
                open_row.effective_to = max(open_row.effective_from, start - timedelta(days=1))
            db.add(StaffAssignmentHistory(profile_id=p.id, unit_id=p.unit_id, manager_id=p.manager_id, performance_role_id=p.performance_role_id, effective_from=start, reason="Placed in the organisation structure", created_by=username))
    db.commit()
    return {"units_set": placed, "managers_set": managed}
