"""
Phase 6d - goal cascading: company -> region -> branch/department -> individual, for additive KPIs (sales disbursed, new customers...).
Higher levels hold an allocation; the individual targets already in the system are the leaf. Everything is computed live, so
changing a number at the top immediately shows how much is unallocated (or over-allocated) at each level below. Nothing is
rewritten automatically: a proposal splits a parent's target across its children in proportion to what they have now, and a
person with rights applies it, which writes ordinary effective-dated targets with a reason.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.performance import CascadeTarget, PerformanceUnit, Region, StaffProfile, UnitRegionLink
from app.services.performance_scope import PerformanceScope
from app.services.performance_targets import TargetError, clean_reason, clean_value, first_of_month, in_force_map, pick_in_force, set_target, targetable_kpis

LIVE = ("active", "probation", "pip")
ADDITIVE_UNITS = ("kes", "count")
COMPANY = "company"
TOLERANCE = Decimal("0.5")


class CascadeError(ValueError):
    pass


def kpi_options(db: Session) -> list[dict[str, str]]:
    """Additive, monthly-entry KPIs that individuals can have targets for."""
    seen: dict[str, dict[str, str]] = {}
    roles = {r for (r,) in db.query(StaffProfile.performance_role_id).distinct()}
    for rid in roles:
        for key, k in targetable_kpis(db, rid).items():
            if k.unit in ADDITIVE_UNITS and k.direction == "higher":
                seen.setdefault(key, {"key": key, "label": k.label, "unit": k.unit})
    return sorted(seen.values(), key=lambda x: x["label"])


def _value(db: Session, level: str, entity_id: str, kpi: str, month: date) -> Decimal | None:
    rows = db.query(CascadeTarget).filter_by(level=level, entity_id=entity_id, kpi_key=kpi).filter(CascadeTarget.effective_from <= first_of_month(month)).all()
    row = pick_in_force(rows, month)
    return Decimal(row.value) if row is not None else None


def _values(db: Session, level: str, kpi: str, month: date) -> dict[str, Decimal]:
    out: dict[str, tuple] = {}
    for r in db.query(CascadeTarget).filter(CascadeTarget.level == level, CascadeTarget.kpi_key == kpi, CascadeTarget.effective_from <= first_of_month(month)).order_by(CascadeTarget.effective_from, CascadeTarget.created_at):
        out[r.entity_id] = (r.effective_from, Decimal(r.value))
    return {k: v[1] for k, v in out.items()}


def set_cascade(db: Session, level: str, entity_id: str, kpi: str, month: date, value: Any, reason: Any, username: str, *, source: str = "manual", commit: bool = True) -> CascadeTarget | None:
    if level not in ("company", "region", "unit"):
        raise CascadeError("Unknown level.")
    if kpi not in {k["key"] for k in kpi_options(db)}:
        raise CascadeError("That KPI can't be cascaded (it must be an additive, higher-is-better figure with individual targets).")
    if level == "region" and db.get(Region, entity_id) is None or level == "unit" and db.get(PerformanceUnit, entity_id) is None:
        raise CascadeError("That region or unit doesn't exist.")
    if level == "company":
        entity_id = COMPANY
    try:
        v, why = clean_value(value), clean_reason(reason)
    except TargetError as exc:
        raise CascadeError(str(exc)) from exc
    m = first_of_month(month)
    cur = _value(db, level, entity_id, kpi, m)
    if cur is not None and cur == v:
        return None
    row = CascadeTarget(level=level, entity_id=entity_id, kpi_key=kpi, effective_from=m, value=v, reason=why, source=source, set_by_username=username)
    db.add(row)
    if commit:
        db.commit()
    return row


def _structure(db: Session, month: date) -> tuple[dict[str, list[str]], dict[str, str | None]]:
    """region -> unit ids (units with no region under None-key 'none'), and unit -> region."""
    m = first_of_month(month)
    unit_region: dict[str, str | None] = {u: None for (u,) in db.query(PerformanceUnit.id).filter(PerformanceUnit.is_active.is_(True))}
    for link in db.query(UnitRegionLink).filter(UnitRegionLink.effective_from <= m).filter((UnitRegionLink.effective_to.is_(None)) | (UnitRegionLink.effective_to >= m)).order_by(UnitRegionLink.effective_from):
        if link.unit_id in unit_region:
            unit_region[link.unit_id] = link.region_id
    by_region: dict[str, list[str]] = defaultdict(list)
    for u, r in unit_region.items():
        by_region[r or "none"].append(u)
    return by_region, unit_region


def _people(db: Session, kpi: str) -> list[StaffProfile]:
    cache: dict[str, bool] = {}
    out = []
    for p in db.query(StaffProfile).filter(StaffProfile.status.in_(LIVE), StaffProfile.is_active.is_(True), StaffProfile.unit_id.is_not(None)):
        if p.performance_role_id not in cache:
            cache[p.performance_role_id] = kpi in targetable_kpis(db, p.performance_role_id)
        if cache[p.performance_role_id]:
            out.append(p)
    return out


def _f(d: Decimal | None) -> float | None:
    return None if d is None else float(d)


def _gap(parent: Decimal | None, children: Decimal) -> dict[str, Any]:
    if parent is None:
        return {"gap": None, "state": "no_target"}
    g = parent - children
    return {"gap": _f(g), "state": "balanced" if abs(g) < TOLERANCE else ("unallocated" if g > 0 else "over")}


def tree(db: Session, scope: PerformanceScope, kpi: str, month: date) -> dict[str, Any]:
    if kpi not in {k["key"] for k in kpi_options(db)}:
        raise CascadeError("That KPI can't be cascaded.")
    by_region, unit_region = _structure(db, month)
    region_vals, unit_vals = _values(db, "region", kpi, month), _values(db, "unit", kpi, month)
    company = _value(db, "company", COMPANY, kpi, month)
    people = [p for p in _people(db, kpi) if scope.can_see(p.id)]
    targets = in_force_map(db, [p.id for p in people], month, [kpi])
    by_unit: dict[str, list[StaffProfile]] = defaultdict(list)
    for p in people:
        by_unit[p.unit_id].append(p)
    unit_names = {i: n for i, n in db.query(PerformanceUnit.id, PerformanceUnit.name)}
    region_names = {i: n for i, n in db.query(Region.id, Region.name)}
    full = scope.visible_ids is None
    regions_out = []
    for rid in sorted(by_region, key=lambda r: region_names.get(r, "~")):
        if not full and not (rid in scope.managed_region_ids or any(u in scope.managed_unit_ids for u in by_region[rid])):
            continue
        units_out = []
        for uid in sorted(by_region[rid], key=lambda u: unit_names.get(u, "")):
            if not full and uid not in scope.managed_unit_ids and rid not in scope.managed_region_ids:
                continue
            ppl = [{"profile_id": p.id, "name": p.full_name, "target": _f(Decimal(targets[(p.id, kpi)].value)) if (p.id, kpi) in targets else None} for p in sorted(by_unit.get(uid, []), key=lambda p: p.full_name)]
            ind = sum((Decimal(str(x["target"])) for x in ppl if x["target"] is not None), Decimal(0))
            if not ppl and uid not in unit_vals:
                continue
            units_out.append({"unit_id": uid, "name": unit_names.get(uid), "target": _f(unit_vals.get(uid)), "individuals_total": _f(ind), "people_without_target": sum(1 for x in ppl if x["target"] is None), "people": ppl, **_gap(unit_vals.get(uid), ind)})
        if not units_out and rid not in region_vals:
            continue
        child_sum = sum((unit_vals.get(u["unit_id"], Decimal(0)) for u in units_out), Decimal(0))
        regions_out.append({"region_id": None if rid == "none" else rid, "name": region_names.get(rid, "Not in a region"), "target": _f(region_vals.get(rid)), "units_total": _f(child_sum), "units": units_out, **_gap(region_vals.get(rid), child_sum)})
    region_sum = sum((region_vals.get(r["region_id"], Decimal(0)) for r in regions_out if r["region_id"]), Decimal(0))
    return {"kpi": kpi, "month": first_of_month(month).isoformat(), "company": {"target": _f(company), "regions_total": _f(region_sum), **_gap(company, region_sum)} if full else None,
            "regions": regions_out, "note": "Gap = what the level above holds minus what has been given to the level below. Positive: still to allocate. Negative: over-allocated."}


# ---- proposals ---------------------------------------------------------------------------------------------------------------


def split(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Whole-number split of `total` in proportion to `weights` (equal when they are all zero); the parts add up exactly."""
    n = len(weights)
    if n == 0:
        return []
    w = weights if sum(weights) > 0 else [Decimal(1)] * n
    s = sum(w)
    raw = [total * x / s for x in w]
    floors = [r.to_integral_value(rounding=ROUND_FLOOR) for r in raw]
    left = int(total.to_integral_value(rounding=ROUND_FLOOR) - sum(floors))
    order = sorted(range(n), key=lambda i: (-(raw[i] - floors[i]), i))
    for i in order[: max(left, 0)]:
        floors[i] += 1
    return floors


def _children(db: Session, level: str, entity_id: str, kpi: str, month: date) -> tuple[str, str, list[tuple[str, str, Decimal | None]]]:
    """(parent label, child level, [(child id, child name, current value)])"""
    by_region, _ur = _structure(db, month)
    region_vals, unit_vals = _values(db, "region", kpi, month), _values(db, "unit", kpi, month)
    if level == "company":
        names = {i: n for i, n in db.query(Region.id, Region.name)}
        return "Company", "region", [(r, names.get(r, r), region_vals.get(r)) for r in sorted(names, key=lambda r: names[r])]
    if level == "region":
        r = db.get(Region, entity_id)
        if r is None:
            raise CascadeError("That region doesn't exist.")
        names = {i: n for i, n in db.query(PerformanceUnit.id, PerformanceUnit.name)}
        return r.name, "unit", [(u, names.get(u, u), unit_vals.get(u)) for u in sorted(by_region.get(entity_id, []), key=lambda u: names.get(u, ""))]
    if level == "unit":
        u = db.get(PerformanceUnit, entity_id)
        if u is None:
            raise CascadeError("That branch or department doesn't exist.")
        people = [p for p in _people(db, kpi) if p.unit_id == entity_id]
        cur = in_force_map(db, [p.id for p in people], month, [kpi])
        return u.name, "person", [(p.id, p.full_name, Decimal(cur[(p.id, kpi)].value) if (p.id, kpi) in cur else None) for p in sorted(people, key=lambda p: p.full_name)]
    raise CascadeError("Unknown level.")


def proposal(db: Session, level: str, entity_id: str, kpi: str, month: date) -> dict[str, Any]:
    if kpi not in {k["key"] for k in kpi_options(db)}:
        raise CascadeError("That KPI can't be cascaded.")
    entity_id = COMPANY if level == "company" else entity_id
    parent = _value(db, level, entity_id, kpi, month)
    label, child_level, kids = _children(db, level, entity_id, kpi, month)
    if parent is None or parent <= 0:
        raise CascadeError(f"Set the {label} target first.")
    if not kids:
        raise CascadeError("There is nothing below it to allocate to.")
    parts = split(parent, [c[2] or Decimal(0) for c in kids])
    rows = [{"child_id": c[0], "name": c[1], "current": _f(c[2]), "proposed": _f(p), "change": _f(p - (c[2] or Decimal(0)))} for c, p in zip(kids, parts)]
    return {"level": level, "entity_id": entity_id, "child_level": child_level, "parent": label, "parent_target": _f(parent), "rows": rows, "basis": "In proportion to their current targets (equal shares if none are set yet)."}


def apply(db: Session, level: str, entity_id: str, kpi: str, month: date, username: str) -> dict[str, Any]:
    p = proposal(db, level, entity_id, kpi, month)
    changed = 0
    m = first_of_month(month)
    reason = f"Cascade from {p['parent']} target"
    for row in p["rows"]:
        v = Decimal(str(row["proposed"]))
        if p["child_level"] == "person":
            prof = db.get(StaffProfile, row["child_id"])
            try:
                if set_target(db, prof, kpi, m, v, reason, username, source="cascade", commit=False) is not None:
                    changed += 1
            except TargetError as exc:
                raise CascadeError(str(exc)) from exc
        else:
            if set_cascade(db, p["child_level"], row["child_id"], kpi, m, v, reason, username, source="cascade", commit=False) is not None:
                changed += 1
    db.commit()
    return {"changed": changed, "children": len(p["rows"])}
