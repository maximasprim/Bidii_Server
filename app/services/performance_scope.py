"""
Staff Performance - the ONE place that decides who may see whom.

Every performance query goes through get_performance_scope(). The scope is
computed from the database on every request (the logged-in user is re-read by
get_current_admin each time); nothing here trusts a JWT claim.

Visibility
  * admin, hr                 everyone
  * executive (profile flag)  everyone, READ-ONLY (the CEO view)
  * everyone else             own profile + their whole reporting line (direct
                              and indirect reports) + every profile in units
                              they manage + every profile in regions they manage
                              (+ the branch(es) of a branch_office_admin login)

Restricted actions (PIP, warnings, ...) are narrower still: only HR, the
person's DIRECT manager and the person themselves. Admins, executives, skip-level
managers, branch heads and peers do not see them, and a profile whose status is
"pip" is shown as "active" to all of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from app.models.admin_user import AdminUser
from app.models.performance import PerformanceUnit, Region, StaffProfile, UnitRegionLink
from app.services.performance_time import today_nairobi


@dataclass(frozen=True)
class PerformanceScope:
    username: str
    own_profile_id: str | None
    is_admin: bool
    is_hr: bool
    is_executive: bool
    # None means "everyone"; otherwise the exact set of profile ids that may be seen.
    visible_ids: frozenset[str] | None
    managed_unit_ids: frozenset[str] = field(default_factory=frozenset)
    managed_region_ids: frozenset[str] = field(default_factory=frozenset)

    @property
    def unrestricted(self) -> bool:
        return self.visible_ids is None

    @property
    def read_only(self) -> bool:
        """The executive view can look at everything but is never allowed to change anything."""
        return self.is_executive and not self.is_admin and not self.is_hr

    @property
    def team_size(self) -> int:
        """People other than themselves they can see (0 for most staff; drives whether the Team menu is useful)."""
        if self.visible_ids is None:
            return -1
        return len(self.visible_ids - ({self.own_profile_id} if self.own_profile_id else set()))

    def can_see(self, profile_id: str) -> bool:
        return self.visible_ids is None or profile_id in self.visible_ids

    def can_see_restricted(self, profile: StaffProfile) -> bool:
        """PIP / warning-type actions: HR, the direct manager, or the person concerned - nobody else."""
        if self.is_hr:
            return True
        if self.own_profile_id is None:
            return False
        return profile.id == self.own_profile_id or profile.manager_id == self.own_profile_id

    def display_status(self, profile: StaffProfile) -> str:
        """A PIP status is itself restricted information: everyone outside the audience sees 'active'."""
        if profile.status == "pip" and not self.can_see_restricted(profile):
            return "active"
        return profile.status


def _reports_closure(own_id: str, rows: list[tuple[str, str | None, str | None]]) -> set[str]:
    children: dict[str, list[str]] = {}
    for pid, manager_id, _unit in rows:
        if manager_id:
            children.setdefault(manager_id, []).append(pid)
    seen: set[str] = set()
    stack = [own_id]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in seen and child != own_id:
                seen.add(child)
                stack.append(child)
    return seen


def get_performance_scope(current_admin: AdminUser, db: Session) -> PerformanceScope:
    role = current_admin.role
    profile = db.query(StaffProfile).filter(StaffProfile.admin_user_id == current_admin.id).first()
    own_id = profile.id if profile else None
    # An administrator can do everything HR can (restricted PIP/warning records, disputes, the HR sign-off, imports of restricted notes);
    # every such access is still audited. is_admin stays separate for the few admin-only settings.
    is_admin, is_hr = role == "admin", role in ("admin", "hr")
    is_exec = bool(profile and profile.is_executive)

    if is_admin or is_hr or is_exec:
        return PerformanceScope(current_admin.username, own_id, is_admin, is_hr, is_exec, None)

    # One query for the whole org chart: the walk below is in memory, so scope
    # costs the same for 50 staff or 500.
    rows = db.query(StaffProfile.id, StaffProfile.manager_id, StaffProfile.unit_id).all()
    unit_of = {pid: unit for pid, _m, unit in rows}

    unit_ids: set[str] = set()
    if own_id:
        unit_ids |= {u for (u,) in db.query(PerformanceUnit.id).filter(PerformanceUnit.manager_profile_id == own_id).all()}
    # A branch_office_admin login is tied to branch(es) on the account itself.
    if role == "branch_office_admin":
        branch_ids = set(current_admin.managed_branch_ids or [])
        if current_admin.branch_id:
            branch_ids.add(current_admin.branch_id)
        if branch_ids:
            unit_ids |= {u for (u,) in db.query(PerformanceUnit.id).filter(PerformanceUnit.branch_id.in_(branch_ids)).all()}

    region_ids: set[str] = set()
    if own_id:
        region_ids = {r for (r,) in db.query(Region.id).filter(Region.manager_profile_id == own_id).all()}
    if region_ids:
        today = today_nairobi()
        links = db.query(UnitRegionLink.unit_id).filter(
            UnitRegionLink.region_id.in_(region_ids),
            UnitRegionLink.effective_from <= today,
            (UnitRegionLink.effective_to.is_(None)) | (UnitRegionLink.effective_to >= today),
        )
        unit_ids |= {u for (u,) in links.all()}

    visible: set[str] = set()
    if own_id:
        visible.add(own_id)
        visible |= _reports_closure(own_id, rows)
    if unit_ids:
        visible |= {pid for pid, unit in unit_of.items() if unit in unit_ids}

    return PerformanceScope(
        current_admin.username,
        own_id,
        False,
        False,
        False,
        frozenset(visible),
        frozenset(unit_ids),
        frozenset(region_ids),
    )
