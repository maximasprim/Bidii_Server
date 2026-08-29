"""
Single source of truth (server-side) for the dashboard's menu registry and
each role's default access, plus resolving the *effective* permissions -
DB overrides (RolePermission rows) layered over the defaults.

This mirrors, and is meant to stay in sync with, the frontend's
src/lib/roleAccess.ts DEFAULT_MENU_ACCESS/MENU_REGISTRY - if you add a
new dashboard page, add it to both. The frontend still ships these same
defaults so the sidebar has something sensible to show before the
GET /api/admin/role-permissions/mine request resolves, and if that
request ever fails (offline, backend down) - it does NOT need them to
match byte-for-byte, but a mismatch just means the UI briefly shows a
slightly different menu set than what the backend will ultimately
enforce for page access, until the fetch completes.
"""

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.auth import get_current_admin

# (path, label) - path must match the routes registered in the frontend's
# App.tsx and the `to` values in AdminLayout's `tabs` list.
MENU_REGISTRY: list[tuple[str, str]] = [
    ("/admin", "Overview"),
    ("/admin/contacts", "Contact Messages"),
    ("/admin/loan-applications", "Loan Applications"),
    ("/admin/career-applications", "Career Applications"),
    ("/admin/ats", "Candidate Screening"),
    ("/admin/news", "News Articles"),
    ("/admin/jobs", "Job Listings"),
    ("/admin/notifications", "Candidate Notifications"),
    ("/admin/loan-terms", "Products"),
    ("/admin/branches", "Branches"),
    ("/admin/users", "Admin Users"),
    ("/admin/role-permissions", "Roles & Permissions"),
]
MENU_PATHS: set[str] = {path for path, _label in MENU_REGISTRY}

# "admin" is deliberately absent - it always has every menu, is never
# stored in the DB, and can't be edited via the settings endpoints below.
CONFIGURABLE_ROLES = ["loan_officer", "hr", "marketing_manager", "branch_office_admin"]
ALL_ROLES = ["admin", *CONFIGURABLE_ROLES]

# Same defaults as the frontend's DEFAULT_MENU_ACCESS in src/lib/roleAccess.ts.
DEFAULT_MENU_ACCESS: dict[str, list[str]] = {
    "loan_officer": ["/admin", "/admin/loan-applications", "/admin/loan-terms"],
    "hr": ["/admin", "/admin/career-applications", "/admin/ats", "/admin/jobs", "/admin/notifications"],
    "marketing_manager": ["/admin", "/admin/contacts", "/admin/news", "/admin/jobs"],
    "branch_office_admin": ["/admin", "/admin/loan-applications", "/admin/loan-terms"],
}


@dataclass
class RoleMenus:
    role: str
    allowed_menus: list[str]
    is_default: bool  # True if this is DEFAULT_MENU_ACCESS, not a saved DB override


def get_all_effective_permissions(db: Session) -> list[RoleMenus]:
    """Every configurable role's current effective permissions (DB override if one exists, else the default)."""
    from app.models.role_permission import RolePermission

    overrides = {row.role: row.allowed_menus for row in db.query(RolePermission).all()}
    return [
        RoleMenus(role=role, allowed_menus=overrides.get(role, DEFAULT_MENU_ACCESS.get(role, [])), is_default=role not in overrides)
        for role in CONFIGURABLE_ROLES
    ]


def get_effective_menus_for_role(db: Session, role: str) -> list[str]:
    """
    What one admin actually sees in their own sidebar right now. "admin"
    always gets every menu, unconditionally - there's no DB row to check
    and no way to restrict it via this system, by design (prevents an
    admin from ever locking themselves, or every admin, out).
    """
    if role == "admin":
        return [path for path, _label in MENU_REGISTRY]

    from app.models.role_permission import RolePermission

    row = db.query(RolePermission).filter(RolePermission.role == role).first()
    if row is not None:
        return row.allowed_menus
    return DEFAULT_MENU_ACCESS.get(role, [])


def require_menu_access(menu_path: str):
    """
    FastAPI dependency factory - restricts a route to admins whose
    effective menu permissions (a DB override if one has been saved for
    their role, else DEFAULT_MENU_ACCESS above) include `menu_path`.

    Before this existed, menu permissions only controlled what the admin
    *sidebar* showed - every ATS endpoint accepted any authenticated
    admin regardless of role, so a role with "/admin/ats" hidden from
    its sidebar (e.g. loan_officer, marketing_manager) could still call
    the ATS API directly and read candidate screening data. This closes
    that gap using the exact same effective-permissions data the
    sidebar already resolves via GET /api/admin/role-permissions/mine -
    nothing new to keep in sync, no separate list to maintain.

    "admin" always passes, same as get_effective_menus_for_role. A role
    change (or a saved RolePermission override) takes effect on an
    admin's very next request, since this re-reads the DB every time
    rather than trusting anything cached in the JWT.
    """

    def dependency(current_admin=Depends(get_current_admin), db: Session = Depends(get_db)):
        if menu_path not in get_effective_menus_for_role(db, current_admin.role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your role doesn't have access to this section.",
            )
        return current_admin

    return dependency

