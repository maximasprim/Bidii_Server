"""
Organisation structure: the reporting tree (scope-filtered), health checks, and first-time setup.

Structure (from the HR induction deck): Directors > CEO > {General Manager - Operations, IT, HR, Finance,
Business Development, Legal}; the GM Operations has the Customer Service Manager (contact centre / office
admins), the Regional Managers (> Branch Managers > Sales Team Leaders > sales executives: DSEs, BDOs,
Check-off officers, Auto champions; Office Admins also sit under the Branch Manager) and the Credit, Risk and
Compliance Manager. Each person has ONE line manager (staff_profiles.manager_id). Functional sign-off steps
(the CSM on an office admin's appraisal) are resolved by role in the appraisal phase, not by a second manager link.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.models.performance import (
    PerformanceLevel,
    PerformanceRole,
    PerformanceUnit,
    StaffUnitCoverage,
    Region,
    StaffProfile,
    UnitRegionLink,
)
from app.services.performance_roles import is_configured, kpis_for, latest_version
from app.services.performance_scope import PerformanceScope
from app.services.performance_time import today_nairobi

# ---- structure (flat facts for the chain-of-command views) ---------------------------------------------------


def build_structure(db: Session, scope: PerformanceScope, include_inactive: bool = False) -> dict[str, Any]:
    """Flat people, units and regions for the Structure view, limited to what `scope` may see. The page builds its trees from these.

    A manager the caller may not see is reported as no manager (the same rule as the reporting tree), and someone who can only see
    part of the organisation gets only the units and regions that contain people they can see.
    """
    q = db.query(StaffProfile)
    if not include_inactive:
        q = q.filter(StaffProfile.is_active.is_(True))
    profiles = [p for p in q.all() if scope.can_see(p.id)]
    seen = {p.id for p in profiles}
    roles = {r.id: r for r in db.query(PerformanceRole)}
    levels = {l.id: l.label for l in db.query(PerformanceLevel.id, PerformanceLevel.label)}
    region_of = {u: r for u, r in db.query(UnitRegionLink.unit_id, UnitRegionLink.region_id).filter(UnitRegionLink.effective_to.is_(None))}

    def visible(profile_id: str | None) -> str | None:
        return profile_id if profile_id in seen else None

    covers: dict[str, list[str]] = {}
    for pid, uid in db.query(StaffUnitCoverage.profile_id, StaffUnitCoverage.unit_id):
        if pid in seen:
            covers.setdefault(pid, []).append(uid)

    people = []
    for p in sorted(profiles, key=lambda x: x.full_name.lower()):
        role = roles.get(p.performance_role_id)
        people.append({
            "id": p.id, "full_name": p.full_name, "role_key": role.key if role else "", "role_name": role.name if role else None,
            "department": role.department if role else None, "level_label": levels.get(p.level_id) if p.level_id else None,
            "is_executive": p.is_executive, "status": scope.display_status(p), "is_active": p.is_active,
            "unit_id": p.unit_id, "manager_id": visible(p.manager_id), "covers": covers.get(p.id, []),
        })

    unit_rows = db.query(PerformanceUnit).filter(PerformanceUnit.is_active.is_(True)).order_by(PerformanceUnit.name).all()
    occupied = {p.unit_id for p in profiles if p.unit_id} | {uid for ids in covers.values() for uid in ids}
    if scope.visible_ids is not None:
        keep = {u.id for u in unit_rows if u.id in occupied or visible(u.manager_profile_id)}
        keep |= {u.parent_unit_id for u in unit_rows if u.id in keep and u.parent_unit_id}  # a unit under a branch brings its branch with it
        unit_rows = [u for u in unit_rows if u.id in keep]
    units = [{"id": u.id, "name": u.name, "kind": u.kind, "manager_profile_id": visible(u.manager_profile_id), "region_id": region_of.get(u.id), "parent_unit_id": u.parent_unit_id} for u in unit_rows]

    kept_regions = {u["region_id"] for u in units if u["region_id"]}
    region_rows = db.query(Region).filter(Region.is_active.is_(True)).order_by(Region.name).all()
    if scope.visible_ids is not None:
        region_rows = [r for r in region_rows if r.id in kept_regions or visible(r.manager_profile_id)]
    regions = [{"id": r.id, "name": r.name, "manager_profile_id": visible(r.manager_profile_id)} for r in region_rows]
    return {"people": people, "units": units, "regions": regions}


# ---- tree ----------------------------------------------------------------------------------------------------


def build_tree(db: Session, scope: PerformanceScope, include_inactive: bool = False) -> tuple[list[dict[str, Any]], int]:
    """Nested reporting tree of exactly the people `scope` may see. Returns (roots, number of people)."""
    q = db.query(StaffProfile)
    if not include_inactive:
        q = q.filter(StaffProfile.is_active.is_(True))
    profiles = [p for p in q.all() if scope.can_see(p.id)]
    roles = {r.id: r.name for r in db.query(PerformanceRole.id, PerformanceRole.name)}
    units = {u.id: u.name for u in db.query(PerformanceUnit.id, PerformanceUnit.name)}
    levels = {l.id: l.label for l in db.query(PerformanceLevel.id, PerformanceLevel.label)}

    nodes: dict[str, dict[str, Any]] = {
        p.id: {
            "id": p.id, "full_name": p.full_name, "role_name": roles.get(p.performance_role_id),
            "level_label": levels.get(p.level_id) if p.level_id else None,
            "unit_name": units.get(p.unit_id) if p.unit_id else None,
            "status": scope.display_status(p),  # a PIP is shown as "active" to anyone not entitled to know
            "is_active": p.is_active, "direct_reports": 0, "total_reports": 0, "children": [],
        }
        for p in profiles
    }
    ordered = sorted(profiles, key=lambda x: x.full_name.lower())
    children_of: dict[str, list[str]] = {}
    root_ids: list[str] = []
    for p in ordered:
        if p.manager_id in nodes:
            children_of.setdefault(p.manager_id, []).append(p.id)
        else:
            root_ids.append(p.id)

    visited: set[str] = set()

    def build(pid: str) -> dict[str, Any]:
        """Depth-first with a visited set: a corrupt reporting loop is broken at one edge instead of recursing forever."""
        visited.add(pid)
        node = nodes[pid]
        node["children"] = [build(cid) for cid in children_of.get(pid, []) if cid not in visited]
        node["direct_reports"] = len(node["children"])
        node["total_reports"] = sum(1 + c["total_reports"] for c in node["children"])
        return node

    roots = [build(rid) for rid in root_ids]
    # Anyone left over sits in a reporting loop (writes forbid loops, so this means bad data): show them, don't hide them.
    for p in ordered:
        if p.id not in visited:
            roots.append(build(p.id))
    return roots, len(nodes)


# ---- health ----------------------------------------------------------------------------------------------------


def org_health(db: Session) -> dict[str, Any]:
    """What is wrong or missing in the structure (admin/HR). Severity: error = results will be wrong; warning = incomplete."""
    issues: list[dict[str, Any]] = []

    def add(code: str, severity: str, message: str, **ids: str | None) -> None:
        issues.append({"code": code, "severity": severity, "message": message, **{k: v for k, v in ids.items() if v}})

    profiles = db.query(StaffProfile).all()
    by_id = {p.id: p for p in profiles}
    roles = {r.id: r for r in db.query(PerformanceRole)}
    units = {u.id: u for u in db.query(PerformanceUnit)}
    active = [p for p in profiles if p.is_active]

    for p in profiles:  # loops
        seen: set[str] = set()
        cur: str | None = p.id
        while cur and cur not in seen:
            seen.add(cur)
            nxt = by_id.get(cur)
            cur = nxt.manager_id if nxt else None
        if cur == p.id:
            add("reporting_loop", "error", f"{p.full_name} is part of a reporting loop.", profile_id=p.id)

    for p in active:
        role_key = roles[p.performance_role_id].key if p.performance_role_id in roles else None
        top = p.is_executive or role_key == "ceo"
        mgr = by_id.get(p.manager_id) if p.manager_id else None
        if mgr is not None and (not mgr.is_active or mgr.status == "exited"):
            add("manager_inactive", "error", f"{p.full_name}'s line manager has left or is inactive.", profile_id=p.id)
        if p.manager_id is None and not top:
            add("no_manager", "warning", f"{p.full_name} has no line manager.", profile_id=p.id)
        if p.unit_id is None and not top:
            add("no_unit", "warning", f"{p.full_name} isn't assigned to a branch or team.", profile_id=p.id)

    levelled_roles = {rid for (rid,) in db.query(PerformanceLevel.role_id).filter(PerformanceLevel.is_active.is_(True)).distinct()}
    for p in active:
        if p.performance_role_id in levelled_roles and p.level_id is None:
            add("no_level", "info", f"{p.full_name} has no level (their role has levels).", profile_id=p.id)

    staff_per_role: dict[str, int] = {}
    for p in active:
        staff_per_role[p.performance_role_id] = staff_per_role.get(p.performance_role_id, 0) + 1
    for rid, n in staff_per_role.items():
        role = roles.get(rid)
        version = latest_version(db, rid) if role else None
        if role and not is_configured(version, kpis_for(db, version.id) if version else []):
            add("role_unconfigured", "info", f"{n} active staff are on '{role.name}', which has no complete KPI set yet.", role_id=rid)

    staffed_units = {p.unit_id for p in active if p.unit_id}
    for uid in staffed_units:
        u = units.get(uid)
        if u and u.is_active and u.kind == "branch" and not u.manager_profile_id:
            add("unit_no_manager", "warning", f"{u.name} has staff but no branch manager set.", unit_id=uid)

    today = today_nairobi()
    in_region = {
        uid for (uid,) in db.query(UnitRegionLink.unit_id).filter(
            UnitRegionLink.effective_from <= today,
            (UnitRegionLink.effective_to.is_(None)) | (UnitRegionLink.effective_to >= today),
        )
    }
    for u in units.values():
        if u.is_active and u.kind == "branch" and u.id not in in_region:
            add("unit_no_region", "warning", f"{u.name} isn't in any region.", unit_id=u.id)
    for r in db.query(Region).filter(Region.is_active.is_(True)):
        if not r.manager_profile_id:
            add("region_no_manager", "warning", f"Region {r.name} has no regional manager.", region_id=r.id)

    counts = {s: sum(1 for i in issues if i["severity"] == s) for s in ("error", "warning", "info")}
    return {"issues": issues, "counts": counts}


# ---- first-time setup ----------------------------------------------------------------------------------------------

# The 11 branches in the induction deck fall into the three groups the workbook's "2025 COMPANY" sheet totals by
# (checked arithmetically on January). Closed or not-yet-open places in the workbook (Kasarani, Eldoret, Mombasa,
# Rongai, Nyeri) are not current branches and are deliberately absent. This is a PROPOSAL the admin confirms.
REGION_PROPOSAL: list[tuple[str, list[str]]] = [
    ("N. RIFT", ["Kawangware", "CBD", "Ngong Road", "Nakuru"]),
    ("N. EAST", ["Kitengela", "Embakasi", "Machakos", "Kajiado"]),
    ("CENTRAL", ["Ruiru", "Thika", "Karatina"]),
]
# Places that aren't public branches but need a home for staff and results.
# Debt collection is deliberately not here: every branch has its own debt collection staff account, placed in that branch.
EXTRA_UNITS: list[tuple[str, str]] = [("Head Office", "head_office"), ("Check-off", "team")]


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def match_branch(name: str, branches: list[Branch]) -> Branch | None:
    """Match a proposal name ('CBD', 'Ngong Road') to a public branch ('Nairobi CBD', 'Head Office & Ngong Road')."""
    target = _norm(name)
    for b in branches:
        if _norm(b.name) == target:
            return b
    for b in branches:
        n = _norm(b.name)
        if len(target) >= 3 and (target in n or n in target):
            return b
    return None


def setup_preview(db: Session) -> dict[str, Any]:
    """
    What "apply" would do. Once any branch is already placed in a region, the preview shows the REAL current grouping (what you set up
    in Regions and branches), so it never contradicts your own changes; branches not yet in a region are listed as
    unplaced for you to put somewhere. The built-in starting proposal (REGION_PROPOSAL) is used only on a first-time
    setup, when no branch has been placed in a region yet.
    """
    branches = db.query(Branch).order_by(Branch.name.asc()).all()
    unit_by_branch = {u.branch_id: u for u in db.query(PerformanceUnit).filter(PerformanceUnit.branch_id.is_not(None))}
    existing_regions = {r.name for r in db.query(Region)}
    existing_units = {u.name for u in db.query(PerformanceUnit)}
    proposed_region: dict[str, str] = {}
    regions: list[dict[str, Any]] = []
    unmatched: list[str] = []

    placed_units = {uid for (uid,) in db.query(UnitRegionLink.unit_id).filter(UnitRegionLink.effective_to.is_(None))}
    if any(u.id in placed_units for u in unit_by_branch.values()):
        branch_of_unit = {u.id: bid for bid, u in unit_by_branch.items()}
        region_name = {r.id: r.name for r in db.query(Region)}
        ids_by_region: dict[str, list[str]] = {name: [] for name in sorted(existing_regions)}
        for unit_id, region_id in db.query(UnitRegionLink.unit_id, UnitRegionLink.region_id).filter(UnitRegionLink.effective_to.is_(None)):
            bid = branch_of_unit.get(unit_id)
            if bid is not None and region_id in region_name:
                ids_by_region[region_name[region_id]].append(bid)
                proposed_region[bid] = region_name[region_id]
        regions = [{"name": n, "branch_ids": ids, "exists": True} for n, ids in ids_by_region.items()]
    else:
        for region_name_, names in REGION_PROPOSAL:
            ids: list[str] = []
            for n in names:
                b = match_branch(n, branches)
                if b is None:
                    unmatched.append(n)
                else:
                    ids.append(b.id)
                    proposed_region[b.id] = region_name_
            regions.append({"name": region_name_, "branch_ids": ids, "exists": False})
    return {
        "branches": [
            {"branch_id": b.id, "name": b.name, "unit_id": unit_by_branch[b.id].id if b.id in unit_by_branch else None,
             "proposed_region": proposed_region.get(b.id)}
            for b in branches
        ],
        "regions": regions,
        "extra_units": [{"name": n, "kind": k, "exists": n in existing_units} for n, k in EXTRA_UNITS],
        "unmatched": unmatched,
    }


@dataclass
class SetupResult:
    units_created: int = 0
    units_linked: int = 0
    regions_created: int = 0
    links_created: int = 0
    conflicts: list[str] | None = None


class SetupError(ValueError):
    pass


def apply_setup(db: Session, layout: list[dict[str, Any]], effective_from: date | None, username: str) -> SetupResult:
    """
    Idempotent. Makes a performance unit for every public branch (linking an existing same-named unit instead of
    duplicating), creates the extra units, and places branches in the regions the admin confirmed. It never moves a
    unit out of a region it is already in; such cases are reported as conflicts. The public branches table is only READ.
    """
    start = effective_from or date(today_nairobi().year, 1, 1)
    branches = {b.id: b for b in db.query(Branch)}
    seen: set[str] = set()
    for entry in layout:
        for bid in entry.get("branch_ids", []):
            if bid not in branches:
                raise SetupError("One of the branches doesn't exist.")
            if bid in seen:
                raise SetupError(f"{branches[bid].name} is in more than one region.")
            seen.add(bid)
        if not str(entry.get("name", "")).strip():
            raise SetupError("Every region needs a name.")

    res = SetupResult(conflicts=[])
    units_by_branch = {u.branch_id: u for u in db.query(PerformanceUnit).filter(PerformanceUnit.branch_id.is_not(None))}
    units_by_name = {u.name: u for u in db.query(PerformanceUnit)}
    for b in branches.values():
        if b.id in units_by_branch:
            continue
        same_name = units_by_name.get(b.name)
        if same_name is not None and same_name.branch_id is None:
            same_name.branch_id = b.id
            units_by_branch[b.id] = same_name
            res.units_linked += 1
        elif same_name is None:
            u = PerformanceUnit(name=b.name, kind="branch", branch_id=b.id)
            db.add(u)
            units_by_branch[b.id] = u
            units_by_name[b.name] = u
            res.units_created += 1
    for name, kind in EXTRA_UNITS:
        if name not in units_by_name:
            u = PerformanceUnit(name=name, kind=kind)
            db.add(u)
            units_by_name[name] = u
            res.units_created += 1
    db.flush()

    regions = {r.name: r for r in db.query(Region)}
    for entry in layout:
        name = entry["name"].strip()
        region = regions.get(name)
        if region is None:
            region = Region(name=name)
            db.add(region)
            db.flush()
            regions[name] = region
            res.regions_created += 1
        for bid in entry.get("branch_ids", []):
            unit = units_by_branch.get(bid)
            if unit is None:
                continue
            open_link = db.query(UnitRegionLink).filter(UnitRegionLink.unit_id == unit.id, UnitRegionLink.effective_to.is_(None)).first()
            if open_link is None:
                db.add(UnitRegionLink(unit_id=unit.id, region_id=region.id, effective_from=start))
                from app.services.performance_units import follow_parent_region

                follow_parent_region(db, unit, region.id, start)
                res.links_created += 1
            elif open_link.region_id != region.id:
                res.conflicts.append(f"{unit.name} is already in another region and was not moved.")
    db.commit()
    return res
