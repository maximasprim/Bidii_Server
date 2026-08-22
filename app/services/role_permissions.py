"""
Single source of truth (server-side) for the dashboard's menu registry and
each role's default access, plus resolving the *effective* permissions —
DB overrides (RolePermission rows) layered over the defaults.

This mirrors, and is meant to stay in sync with, the frontend's
src/lib/roleAccess.ts DEFAULT_MENU_ACCESS/MENU_REGISTRY — if you add a
new dashboard page, add it to both. The frontend still ships these same
defaults so the sidebar has something sensible to show before the
GET /api/admin/role-permissions/mine request resolves, and if that
request ever fails (offline, backend down) — it does NOT need them to
match byte-for-byte, but a mismatch just means the UI briefly shows a
slightly different menu set than what the backend will ultimately
enforce for page access, until the fetch completes.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

# (path, label) — path must match the routes registered in the frontend's
# App.tsx and the `to` values in AdminLayout's `tabs` list.
MENU_REGISTRY: list[tuple[str, str]] = [
    ("/admin", "Overview"),
    ("/admin/contacts", "Contact Messages"),
    ("/admin/loan-applications", "Loan Applications"),
    ("/admin/career-applications", "Career Applications"),
    ("/admin/ats", "Candidate Screening"),
    ("/admin/news", "News Articles"),
    ("/admin/jobs", "Job Listings"),
    ("/admin/loan-terms", "Products"),
    ("/admin/users", "Admin Users"),
    ("/admin/role-permissions", "Roles & Permissions"),
]
MENU_PATHS: set[str] = {path for path, _label in MENU_REGISTRY}

# "admin" is deliberately absent — it always has every menu, is never
# stored in the DB, and can't be edited via the settings endpoints below.
CONFIGURABLE_ROLES = ["loan_officer", "hr", "marketing_manager"]
ALL_ROLES = ["admin", *CONFIGURABLE_ROLES]

# Same defaults as the frontend's DEFAULT_MENU_ACCESS in src/lib/roleAccess.ts.
DEFAULT_MENU_ACCESS: dict[str, list[str]] = {
    "loan_officer": ["/admin", "/admin/loan-applications", "/admin/loan-terms"],
    "hr": ["/admin", "/admin/career-applications", "/admin/ats", "/admin/jobs"],
    "marketing_manager": ["/admin", "/admin/contacts", "/admin/news", "/admin/jobs"],
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
    always gets every menu, unconditionally — there's no DB row to check
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
