"""
Who may DO what in the Staff Performance module, editable from the dashboard (Roles & Permissions).

Every action that used to be hard-coded as "administrators only" or "administrators and HR" is now a named permission (a "capability").
Each has a built-in default that is exactly what the code did before, so nothing changes until an administrator changes a box.
Changes are stored as differences from the defaults (performance_permission_grants) and are read on every request, so they take
effect immediately and a removed permission can never linger in a cache.

LINE MANAGERS. Besides the dashboard roles there is one extra column, "line_manager": anyone who manages people or a branch in the
performance structure, whatever their dashboard role. Ticking it lets them do an action for THEIR OWN TEAM only (the people and units inside
their scope), never anyone else. Only some actions can be limited to a team (`manager_scope`); the handlers of those actions enforce the limits.
A role tick (organisation-wide) always wins over a line-manager tick.

What this does NOT control, on purpose:
* What a person can SEE (their team, their branch...). That comes from the reporting structure (performance_scope).
* "Administrator" always has everything and cannot be restricted, so nobody can lock the organisation out.
* The permissions screen itself, which stays administrators-only.
* Rules like "you can't verify your own figures".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.performance import PerformancePermissionGrant
from app.services.auth import get_current_admin
from app.services.performance_scope import PerformanceScope, get_performance_scope

log = logging.getLogger(__name__)

ADMIN = "admin"
LINE_MANAGER = "line_manager"  # not a dashboard role: a column meaning "managers, for their own team only"
SETTINGS_MENU = "/admin/performance-settings"
STAFF_MENU = "/admin/performance"


@dataclass(frozen=True)
class Capability:
    key: str
    label: str
    description: str
    group: str
    default_roles: tuple[str, ...]
    sensitive: bool = False  # granting it to another role needs an explicit confirmation
    menu: str = STAFF_MENU  # the menu a role needs in order to reach the page this belongs to
    manager_scope: bool = False  # can be given to line managers for their own team only (the handlers enforce the team limits)


_AH = (ADMIN, "hr")
_A = (ADMIN,)

CAPABILITIES: tuple[Capability, ...] = (
    # --- Settings and job roles ---
    Capability("settings.edit", "Change performance settings and rating scales", "The organisation-wide scoring settings, the rating scale and its preview.", "Settings and job roles", _AH, menu=SETTINGS_MENU),
    Capability("roles.edit", "Create, rename and delete job roles", "The list of job roles used for performance.", "Settings and job roles", _AH, menu=SETTINGS_MENU),
    Capability("roles.jd", "Edit job descriptions and KPIs", "Save a job description and its KPIs, including the AI helpers that read a JD and suggest KPIs.", "Settings and job roles", _AH, menu=SETTINGS_MENU),
    Capability("levels.edit", "Edit levels (job grades)", "Create and change levels and their default targets.", "Settings and job roles", _AH),
    # --- Organisation ---
    Capability("structure.edit", "Edit branches / units and regions", "Create and rename units and regions, and place a unit in a region.", "Organisation", _A),
    Capability("orgsetup.manage", "Run the organisation set-up tools", "Bulk set-up, duplicate clean-up, linking a unit to a branch, closing or deleting a unit.", "Organisation", _A, sensitive=True),
    Capability("org.placement", "Place staff in units", "View the layout and apply staff placements.", "Organisation", _AH),
    Capability("org.coverage", "Set who covers for a person", "See and change a person's coverage arrangements.", "Organisation", _AH, manager_scope=True),
    Capability("org.health", "See the organisation health check", "The checklist of gaps in the structure and set-up.", "Organisation", _AH),
    # --- Staff and targets ---
    Capability("staff.manage", "Add and edit staff profiles", "Create a staff profile or change one, and link it to a login.", "Staff and targets", _AH, manager_scope=True),
    Capability("targets.set", "Set staff targets", "Set a person's targets, or many at once.", "Staff and targets", _AH, manager_scope=True),
    Capability("targets.decide", "Approve or decline target change requests", "Decide the requests managers send in to change a target.", "Staff and targets", _AH, manager_scope=True),
    Capability("cascade.manage", "Cascade goals down the structure", "Set organisation targets and apply the proposed split to units and people.", "Staff and targets", _AH),
    # --- Figures and appraisals ---
    Capability("entries.reopen", "Reopen a verified monthly figure", "Take a verified figure back to be corrected.", "Figures and appraisals", _AH, manager_scope=True),
    Capability("appraisals.reopen", "Reopen a finalised appraisal", "Take a finalised appraisal back to be corrected.", "Figures and appraisals", _AH),
    Capability("threesixty.manage", "Run 360 feedback cycles", "Create cycles, choose who is reviewed and move a cycle along.", "Figures and appraisals", _AH),
    # --- Imports and data ---
    Capability("imports.manage", "Import figures from Excel", "Upload, check, commit and remove an import.", "Imports and data", _AH),
    Capability("imports.history", "Compare, undo and restore past imports", "Compare versions of an import and restore or unpost an earlier one.", "Imports and data", _AH, sensitive=True),
    Capability("reports.recompute", "Recompute stored report snapshots", "Rebuild the saved monthly report figures.", "Imports and data", _AH),
    Capability("packs.generate", "Generate the board pack", "Create the board pack.", "Imports and data", _AH),
    Capability("ops.data_quality", "See anomalies and data-quality checks", "The Operations page's checks on odd or missing figures.", "Imports and data", _AH),
    Capability("notifications.status", "See notification status", "Whether reminders and notices are going out.", "Imports and data", _AH),
    # --- Governance ---
    Capability("locks.manage", "Lock and reopen months", "Close a month to further changes, or reopen a closed month.", "Governance", _AH, sensitive=True),
    Capability("governance.notice", "See outstanding privacy notices", "Who has not yet been given the staff privacy notice.", "Governance", _AH),
    Capability("retention.view", "See staff due for data retention", "The list of leavers whose records are past the retention period.", "Governance", _AH),
    Capability("retention.anonymise", "Anonymise a person (cannot be undone)", "Permanently remove a leaver's personal details from performance records.", "Governance", _A, sensitive=True),
    Capability("audit.view", "View the audit log", "Who changed what, and when.", "Governance", _A, sensitive=True),
)

BY_KEY: dict[str, Capability] = {c.key: c for c in CAPABILITIES}
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(c.group for c in CAPABILITIES))
assert len(BY_KEY) == len(CAPABILITIES), "duplicate capability key"


def default_allowed(role: str, key: str) -> bool:
    return role in BY_KEY[key].default_roles  # the line-manager column is never on by default


def overrides_for_role(db: Session, role: str) -> dict[str, bool]:
    return {row.capability: bool(row.allowed) for row in db.query(PerformancePermissionGrant).filter(PerformancePermissionGrant.role == role)}


def effective_for_role(db: Session, role: str) -> set[str]:
    """Every permission this role has right now. The administrator role always has them all."""
    if role == ADMIN:
        return set(BY_KEY)
    try:
        over = overrides_for_role(db, role)
    except SQLAlchemyError:  # if the table can't be read, fall back to the built-in defaults, never to "everything"
        db.rollback()
        log.exception("could not read performance permission changes; using the defaults")
        over = {}
    return {k for k in BY_KEY if over.get(k, default_allowed(role, k))}


def has_capability(db: Session, role: str, key: str) -> bool:
    if role == ADMIN:
        return True
    try:
        row = db.get(PerformancePermissionGrant, (role, key))
    except SQLAlchemyError:
        db.rollback()
        log.exception("could not read performance permission changes; using the defaults")
        row = None
    return bool(row.allowed) if row is not None else default_allowed(role, key)


def line_manager_keys(db: Session) -> set[str]:
    """The team-only permissions currently switched on for line managers."""
    try:
        rows = overrides_for_role(db, LINE_MANAGER)
    except SQLAlchemyError:
        db.rollback()
        log.exception("could not read performance permission changes; using the defaults")
        return set()
    return {k for k, v in rows.items() if v and k in BY_KEY and BY_KEY[k].manager_scope}


def is_line_manager(scope: PerformanceScope) -> bool:
    """Someone who manages people or a branch, and whose view is limited to them. Administrators, HR and the executive view are not 'line managers'."""
    if scope.read_only or scope.visible_ids is None or scope.own_profile_id is None:
        return False
    return bool(scope.managed_unit_ids) or scope.team_size > 0


def is_limited(db: Session, admin, key: str) -> bool:
    """True when this person holds `key` only as a line manager, so the action must stay inside their own team. Call it inside a handler that `needs(key)`."""
    return not has_capability(db, admin.role, key)


def needs(key: str, org_wide: bool = False):
    """
    Use as `dependencies=[needs("levels.edit")]` on a route. Checked against the database on every request.
    A role that holds the permission passes (organisation-wide). A line manager passes too when it is switched on for line managers and the
    action can be limited to a team (`manager_scope`), unless `org_wide=True` - then only the roles pass. A handler that accepts line managers
    must keep them inside their team (see `is_limited`).
    """
    if key not in BY_KEY:
        raise KeyError(f"unknown performance permission: {key}")

    def dependency(current_admin=Depends(get_current_admin), db: Session = Depends(get_db)):
        if has_capability(db, current_admin.role, key):
            return current_admin
        if BY_KEY[key].manager_scope and not org_wide and key in line_manager_keys(db) and is_line_manager(get_performance_scope(current_admin, db)):
            return current_admin
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You don't have permission to access this.")

    dependency.capability = key  # type: ignore[attr-defined]  - lets a test confirm every permission is enforced somewhere
    return Depends(dependency)
