"""
Units a person works in besides their main one: a debt collector who covers several branches is in one branch's Debt Collection unit
(their main unit, kept on the profile) and also covers the others. This only records who covers what; it never moves anyone.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.performance import PerformanceUnit, StaffProfile, StaffUnitCoverage

MAX_EXTRA_UNITS = 40


class CoverageError(ValueError):
    pass


def read(db: Session, profile: StaffProfile) -> dict[str, Any]:
    ids = [i for (i,) in db.query(StaffUnitCoverage.unit_id).filter(StaffUnitCoverage.profile_id == profile.id)]
    units = {u.id: u for u in db.query(PerformanceUnit).filter(PerformanceUnit.id.in_(ids))} if ids else {}
    rows = [{"id": u.id, "name": u.name, "kind": u.kind, "parent_unit_id": u.parent_unit_id, "is_active": u.is_active} for u in sorted(units.values(), key=lambda x: x.name)]
    return {"profile_id": profile.id, "main_unit_id": profile.unit_id, "units": rows}


def replace(db: Session, profile: StaffProfile, unit_ids: list[str], username: str) -> dict[str, Any]:
    """Sets the full list of extra units. Anything not listed is removed; the person's main unit is never an "extra"."""
    wanted = [i for i in dict.fromkeys(unit_ids) if i != profile.unit_id]
    if len(wanted) > MAX_EXTRA_UNITS:
        raise CoverageError(f"That is more than {MAX_EXTRA_UNITS} units.")
    found = {u.id: u for u in db.query(PerformanceUnit).filter(PerformanceUnit.id.in_(wanted))} if wanted else {}
    for uid in wanted:
        unit = found.get(uid)
        if unit is None or not unit.is_active:
            raise CoverageError("One of those units doesn't exist or has been closed.")
    current = {c.unit_id: c for c in db.query(StaffUnitCoverage).filter(StaffUnitCoverage.profile_id == profile.id)}
    for uid, row in current.items():
        if uid not in wanted:
            db.delete(row)
    for uid in wanted:
        if uid not in current:
            db.add(StaffUnitCoverage(profile_id=profile.id, unit_id=uid, created_by=username))
    db.commit()
    return read(db, profile)
