"""
Keeping the unit list clean: link an imported unit to a website branch, find the empty duplicates that setup created next to them,
and remove only units that nothing refers to. A unit that has people, history, snapshots or cascade targets is never deleted.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.models.performance import CascadeTarget, PerformanceUnit, ScoreSnapshot, StaffAssignmentHistory, StaffProfile, UnitRegionLink


class UnitError(ValueError):
    pass


def _norm(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    s = re.sub(r"\brd\b", "road", s)
    s = re.sub(r"\bst\b", "street", s)
    return re.sub(r"[^a-z0-9]", "", s)


def _same_place(a: str, b: str) -> bool:
    x, y = _norm(a), _norm(b)
    return bool(x and y) and (x == y or (len(x) >= 3 and len(y) >= 3 and (x in y or y in x)))


def usage(db: Session, unit_id: str) -> dict[str, int]:
    return {
        "staff": db.query(StaffProfile).filter(StaffProfile.unit_id == unit_id).count(),
        "history": db.query(StaffAssignmentHistory).filter(StaffAssignmentHistory.unit_id == unit_id).count(),
        "snapshots": db.query(ScoreSnapshot).filter(ScoreSnapshot.unit_id == unit_id).count(),
        "cascade": db.query(CascadeTarget).filter(CascadeTarget.level == "unit", CascadeTarget.entity_id == unit_id).count(),
    }


def is_empty(db: Session, unit_id: str) -> bool:
    return not any(usage(db, unit_id).values())


def delete_if_empty(db: Session, unit: PerformanceUnit) -> None:
    if not is_empty(db, unit.id):
        raise UnitError(f"{unit.name} has people or history, so it can't be removed.")
    db.query(UnitRegionLink).filter(UnitRegionLink.unit_id == unit.id).delete()
    db.query(PerformanceUnit).filter(PerformanceUnit.id == unit.id).delete()


def link_to_branch(db: Session, unit: PerformanceUnit, branch_id: str | None, use_branch_name: bool) -> dict[str, Any]:
    """Points a unit at a website branch (or unlinks it). An EMPTY unit already on that branch is removed to make room; a unit with people is never touched."""
    removed = None
    if branch_id is None:
        unit.branch_id = None
        db.commit()
        return {"removed_duplicate": None, "name": unit.name}
    branch = db.get(Branch, branch_id)
    if branch is None:
        raise UnitError("That branch isn't on the website branch list.")
    if unit.kind != "branch":
        raise UnitError("Only a branch unit can be linked to a website branch.")
    other = db.query(PerformanceUnit).filter(PerformanceUnit.branch_id == branch_id, PerformanceUnit.id != unit.id).first()
    if other is not None:
        if not is_empty(db, other.id):
            raise UnitError(f"{branch.name} is already linked to {other.name}, which has people. Unlink that one first.")
        removed = other.name
        delete_if_empty(db, other)
        db.flush()
    unit.branch_id = branch.id
    if use_branch_name and unit.name != branch.name:
        clash = db.query(PerformanceUnit).filter(PerformanceUnit.name == branch.name, PerformanceUnit.id != unit.id).first()
        if clash is not None:
            raise UnitError(f'A unit called "{branch.name}" already exists.')
        unit.name = branch.name
    db.commit()
    return {"removed_duplicate": removed, "name": unit.name}


def duplicates(db: Session) -> list[dict[str, Any]]:
    """Pairs where a unit with people has an EMPTY twin (same place under another name): keep the one with people, remove the empty one."""
    units = db.query(PerformanceUnit).filter(PerformanceUnit.is_active.is_(True)).all()
    branches = {b.id: b.name for b in db.query(Branch)}
    empties = [u for u in units if is_empty(db, u.id)]
    pairs: list[dict[str, Any]] = []
    used: set[str] = set()
    for keep in sorted(units, key=lambda u: u.name):
        if keep in empties or keep.kind == "head_office":
            continue
        for twin in empties:
            if twin.id in used or twin.id == keep.id or twin.kind != keep.kind:
                continue
            names = [twin.name, branches.get(twin.branch_id or "", "")]
            if any(n and _same_place(keep.name, n) for n in names):
                used.add(twin.id)
                pairs.append({
                    "keep_id": keep.id, "keep_name": keep.name, "keep_staff": usage(db, keep.id)["staff"],
                    "remove_id": twin.id, "remove_name": twin.name,
                    "link_branch_id": twin.branch_id if keep.kind == "branch" and keep.branch_id is None else None,
                    "link_branch_name": branches.get(twin.branch_id or "") if keep.kind == "branch" and keep.branch_id is None else None,
                    "use_name": twin.name,
                })
                break
    return pairs


def clean_up(db: Session, items: list[dict[str, Any]]) -> dict[str, int]:
    """Each item: keep_id, remove_id, optional link (bool) and use_name (bool). One transaction."""
    removed = linked = renamed = 0
    try:
        for it in items:
            keep, twin = db.get(PerformanceUnit, it["keep_id"]), db.get(PerformanceUnit, it["remove_id"])
            if keep is None or twin is None or keep.id == twin.id:
                raise UnitError("One of the units doesn't exist.")
            twin_branch, twin_name = twin.branch_id, twin.name
            delete_if_empty(db, twin)  # raises if it has people or history
            db.flush()
            removed += 1
            if it.get("link") and twin_branch and keep.kind == "branch" and keep.branch_id is None:
                keep.branch_id = twin_branch
                linked += 1
            if it.get("use_name") and keep.name != twin_name:
                if db.query(PerformanceUnit).filter(PerformanceUnit.name == twin_name, PerformanceUnit.id != keep.id).first() is not None:
                    raise UnitError(f'A unit called "{twin_name}" already exists.')
                keep.name = twin_name
                renamed += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"removed": removed, "linked": linked, "renamed": renamed}


def close_unit(db: Session, unit: PerformanceUnit, move_to_id: str | None, username: str) -> dict[str, int]:
    """
    Closes a unit (a branch that shut down). Its past stays as it was: results already reported, assignment history and region history keep pointing at it.
    From today it takes no new work: its people move to another unit (or are left without one), its region link ends and it is hidden from the active lists.
    """
    from datetime import timedelta

    from app.services.performance_time import today_nairobi

    if not unit.is_active:
        raise UnitError("That unit is already closed.")
    dest = None
    if move_to_id is not None:
        dest = db.get(PerformanceUnit, move_to_id)
        if dest is None or not dest.is_active or dest.id == unit.id:
            raise UnitError("Choose an open unit to move the people to.")
    today = today_nairobi()
    people = db.query(StaffProfile).filter(StaffProfile.unit_id == unit.id).all()
    for p in people:
        p.unit_id = dest.id if dest else None
        open_row = db.query(StaffAssignmentHistory).filter(StaffAssignmentHistory.profile_id == p.id, StaffAssignmentHistory.effective_to.is_(None)).first()
        if open_row is not None:
            open_row.effective_to = max(open_row.effective_from, today - timedelta(days=1))
        db.add(StaffAssignmentHistory(profile_id=p.id, unit_id=p.unit_id, manager_id=p.manager_id, performance_role_id=p.performance_role_id, effective_from=today,
                                      reason=f"{unit.name} closed", created_by=username))
    link = db.query(UnitRegionLink).filter(UnitRegionLink.unit_id == unit.id, UnitRegionLink.effective_to.is_(None)).first()
    if link is not None:
        link.effective_to = max(link.effective_from, today - timedelta(days=1))
    unit.is_active = False
    unit.manager_profile_id = None
    db.commit()
    return {"people_moved": len(people)}
