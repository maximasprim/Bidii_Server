"""
Every login is also a staff member. This keeps the two lists in step:

  * a user created from Admin Users gets a staff profile (a placeholder role, "General staff", until HR assigns the real one);
  * a staff profile created with a login gets the login created and linked in the same step;
  * logins that already existed are backfilled at startup (idempotent).

A profile is matched to an existing UNLINKED one only on an exact, unique, case-insensitive full-name match, so people already
imported from the Excel tracker are linked rather than duplicated. Nothing here ever deletes or overwrites data.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.admin_user import AdminUser
from app.models.performance import PerformanceRole, PerformanceUnit, StaffAssignmentHistory, StaffProfile
from app.services.performance_time import today_nairobi

logger = logging.getLogger("bidii.performance")

DEFAULT_ROLE_KEY = "general_staff"


def display_name(username: str) -> str:
    """jane.doe / jane_doe / jane-doe -> Jane Doe. HR can correct it afterwards."""
    parts = [p for p in re.split(r"[._\-\s]+", username.strip()) if p]
    return " ".join(p.capitalize() for p in parts) or username


def default_role(db: Session) -> PerformanceRole:
    role = db.query(PerformanceRole).filter(PerformanceRole.key == DEFAULT_ROLE_KEY).first()
    if role is None:
        role = PerformanceRole(key=DEFAULT_ROLE_KEY, name="General staff", description="Placeholder until HR assigns the person's real role.", kind="appraisal", is_system=True)
        db.add(role)
        db.flush()
    return role


def _unit_for_branch(db: Session, branch_id: str | None) -> str | None:
    if not branch_id:
        return None
    ids = [i for (i,) in db.query(PerformanceUnit.id).filter(PerformanceUnit.branch_id == branch_id, PerformanceUnit.is_active.is_(True)).limit(2)]
    return ids[0] if len(ids) == 1 else None


def ensure_profile(db: Session, user: AdminUser, *, full_name: str | None = None, actor: str = "system", commit: bool = True) -> tuple[StaffProfile, str]:
    """Returns (profile, how) where how is 'existing' | 'linked' | 'created'."""
    profile = db.query(StaffProfile).filter(StaffProfile.admin_user_id == user.id).first()
    if profile is not None:
        return profile, "existing"
    name = (full_name or "").strip() or display_name(user.username)
    matches = db.query(StaffProfile).filter(StaffProfile.admin_user_id.is_(None), func.lower(StaffProfile.full_name) == name.lower()).limit(2).all()
    if len(matches) == 1:
        matches[0].admin_user_id = user.id
        profile, how = matches[0], "linked"
    else:
        profile = StaffProfile(admin_user_id=user.id, full_name=name, performance_role_id=default_role(db).id, unit_id=_unit_for_branch(db, user.branch_id), status="active", is_active=bool(user.is_active))
        db.add(profile)
        db.flush()
        db.add(StaffAssignmentHistory(profile_id=profile.id, unit_id=profile.unit_id, manager_id=None, performance_role_id=profile.performance_role_id, effective_from=today_nairobi(), reason="Created with the login", created_by=actor))
        how = "created"
    if commit:
        db.commit()
    return profile, how


def backfill(db: Session, actor: str = "system") -> dict[str, int]:
    """Gives every login without a profile one. Safe to run on every start."""
    linked_ids = {i for (i,) in db.query(StaffProfile.admin_user_id).filter(StaffProfile.admin_user_id.isnot(None))}
    out = {"created": 0, "linked": 0}
    for user in db.query(AdminUser).order_by(AdminUser.created_at).all():
        if user.id in linked_ids:
            continue
        _p, how = ensure_profile(db, user, actor=actor, commit=False)
        if how in out:
            out[how] += 1
    if out["created"] or out["linked"]:
        db.commit()
        logger.info("Staff profiles for existing logins: %s", out)
    return out


def mirror_active(db: Session, user: AdminUser) -> None:
    """A login switched off/on is mirrored on the profile's is_active (history is kept either way)."""
    p = db.query(StaffProfile).filter(StaffProfile.admin_user_id == user.id).first()
    if p is not None and p.is_active != bool(user.is_active):
        p.is_active = bool(user.is_active)
        db.commit()
