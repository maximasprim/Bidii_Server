"""Permissions editable from the dashboard: the defaults are exactly the old hard-coded rules, and a change takes effect at once."""

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.models.admin_user import AdminUser
from app.models.performance import PerformanceAuditLog, PerformancePermissionGrant
from app.services import performance_permissions as perm
from app.services.role_permissions import get_effective_menus_for_role
from performance_tests.org import build_world

BASE = "/api/admin/performance"

# What the code did BEFORE permissions became editable: which roles could do each action. The catalogue's defaults must equal this.
ADMIN_ONLY = {"structure.edit", "orgsetup.manage", "retention.anonymise", "audit.view"}
ADMIN_AND_HR = {
    "settings.edit", "roles.edit", "roles.jd", "levels.edit", "org.placement", "org.coverage", "org.health", "staff.manage", "targets.set",
    "targets.decide", "cascade.manage", "entries.reopen", "appraisals.reopen", "threesixty.manage", "imports.manage", "imports.history",
    "reports.recompute", "packs.generate", "ops.data_quality", "notifications.status", "locks.manage", "governance.notice", "retention.view",
}


def put(client, w, who, role, capability, allowed, **extra):
    return client.put(f"{BASE}/permissions", json={"role": role, "capability": capability, "allowed": allowed, **extra}, headers=w.h[who])


def role_of(db, username):
    return db.query(AdminUser).filter_by(username=username).one().role


def staff_users(client, w, who):  # a route guarded by "staff.manage"
    return client.get(f"{BASE}/staff/linkable-users", headers=w.h[who])


def audit(client, w, who):  # a route guarded by "audit.view"
    return client.get(f"{BASE}/audit", headers=w.h[who])


def test_the_defaults_equal_the_old_hard_coded_rules():
    assert set(perm.BY_KEY) == ADMIN_ONLY | ADMIN_AND_HR and len(perm.BY_KEY) == 27
    for key in ADMIN_ONLY:
        assert perm.BY_KEY[key].default_roles == ("admin",)
    for key in ADMIN_AND_HR:
        assert perm.BY_KEY[key].default_roles == ("admin", "hr")


def test_every_permission_is_enforced_by_at_least_one_route(client):
    used = set()
    for route in client.app.routes:
        dependant = getattr(route, "dependant", None)
        for dep in getattr(dependant, "dependencies", []) or []:
            key = getattr(dep.call, "capability", None)
            if key:
                used.add(key)
    assert used == set(perm.BY_KEY)


def test_nothing_changes_until_someone_changes_a_box(client, db):
    w = build_world(client, db)
    assert staff_users(client, w, "hr1").status_code == 200 and staff_users(client, w, "admin1").status_code == 200
    assert staff_users(client, w, "lo1").status_code == 403
    assert audit(client, w, "admin1").status_code == 200 and audit(client, w, "hr1").status_code == 403
    assert db.query(PerformancePermissionGrant).count() == 0


def test_grant_a_permission_to_another_role_and_take_it_back(client, db):
    w = build_world(client, db)
    role = role_of(db, "lo1")
    assert staff_users(client, w, "lo1").status_code == 403
    r = put(client, w, "admin1", role, "staff.manage", True)
    assert r.status_code == 200 and r.json()["cell"]["allowed"] is True and r.json()["cell"]["overridden"] is True
    assert staff_users(client, w, "lo1").status_code == 200  # takes effect on the very next request
    r = put(client, w, "admin1", role, "staff.manage", False)  # back to the default: nothing is stored
    assert r.status_code == 200 and db.query(PerformancePermissionGrant).count() == 0
    assert staff_users(client, w, "lo1").status_code == 403


def test_deny_hr_something_it_normally_has_then_reset(client, db):
    w = build_world(client, db)
    assert put(client, w, "admin1", "hr", "staff.manage", False).status_code == 200
    assert staff_users(client, w, "hr1").status_code == 403 and staff_users(client, w, "admin1").status_code == 200
    assert client.post(f"{BASE}/permissions/reset", json={"role": "hr"}, headers=w.h["admin1"]).json() == {"removed": 1}
    assert staff_users(client, w, "hr1").status_code == 200


def test_a_denied_write_is_blocked_and_the_default_write_works(client, db):
    w = build_world(client, db)
    body = {"role_id": w.role.id, "label": "Perm Level", "rank": 9, "default_targets": {"sales": 100000}}
    assert put(client, w, "admin1", "hr", "levels.edit", False).status_code == 200
    assert client.post(f"{BASE}/levels", json=body, headers=w.h["hr1"]).status_code == 403
    assert client.post(f"{BASE}/permissions/reset", json={}, headers=w.h["admin1"]).status_code == 200
    assert client.post(f"{BASE}/levels", json=body, headers=w.h["hr1"]).status_code == 201


def test_administrators_can_never_be_restricted(client, db):
    w = build_world(client, db)
    assert put(client, w, "admin1", "admin", "staff.manage", False).status_code == 422
    assert put(client, w, "admin1", "not_a_role", "staff.manage", True).status_code == 422
    assert put(client, w, "admin1", "hr", "not_a_permission", True).status_code == 422
    assert db.query(PerformancePermissionGrant).count() == 0
    assert staff_users(client, w, "admin1").status_code == 200


def test_a_sensitive_permission_needs_confirmation_and_is_audited(client, db):
    w = build_world(client, db)
    assert put(client, w, "admin1", "hr", "audit.view", True).status_code == 422  # not confirmed
    assert audit(client, w, "hr1").status_code == 403
    assert put(client, w, "admin1", "hr", "audit.view", True, confirm_sensitive=True).status_code == 200
    assert audit(client, w, "hr1").status_code == 200
    assert put(client, w, "admin1", "hr", "locks.manage", False).status_code == 200  # taking a sensitive one away needs no confirmation
    actions = [a.action for a in db.query(PerformanceAuditLog).order_by(PerformanceAuditLog.created_at)]
    assert actions.count("permissions.change") == 2


def test_only_administrators_can_see_or_change_the_matrix_even_if_granted_everything(client, db):
    w = build_world(client, db)
    for cap in perm.BY_KEY:
        put(client, w, "admin1", "hr", cap, True, confirm_sensitive=True)
    assert client.get(f"{BASE}/permissions", headers=w.h["hr1"]).status_code == 403
    assert put(client, w, "hr1", "hr", "audit.view", False).status_code == 403
    assert client.post(f"{BASE}/permissions/reset", json={}, headers=w.h["hr1"]).status_code == 403


def test_my_permissions_follow_changes_at_once(client, db):
    w = build_world(client, db)
    mine = lambda who: set(client.get(f"{BASE}/permissions/me", headers=w.h[who]).json()["capabilities"])  # noqa: E731
    assert mine("admin1") == set(perm.BY_KEY)
    assert mine("hr1") == ADMIN_AND_HR
    put(client, w, "admin1", "hr", "staff.manage", False)
    put(client, w, "admin1", "hr", "audit.view", True, confirm_sensitive=True)
    assert mine("hr1") == (ADMIN_AND_HR - {"staff.manage"}) | {"audit.view"}
    assert "staff.manage" not in mine("lo1") or role_of(db, "lo1") == "hr"


def test_the_matrix_describes_every_permission_for_every_role(client, db):
    w = build_world(client, db)
    d = client.get(f"{BASE}/permissions", headers=w.h["admin1"]).json()
    assert "admin" not in d["roles"] and "hr" in d["roles"] and "line_manager" in d["roles"] and len(d["capabilities"]) == 27
    cap = next(c for c in d["capabilities"] if c["key"] == "staff.manage")
    assert cap["default_roles"] == ["admin", "hr"] and cap["group"] in d["groups"] and cap["sensitive"] is False
    assert cap["cells"]["hr"] == {"allowed": True, "default": True, "overridden": False, "menu_missing": cap["cells"]["hr"]["menu_missing"], "locked": False}
    assert cap["cells"][role_of(db, "lo1")]["allowed"] is False
    assert next(c for c in d["capabilities"] if c["key"] == "audit.view")["sensitive"] is True


def test_reset_one_role_or_everything(client, db):
    w = build_world(client, db)
    put(client, w, "admin1", "hr", "staff.manage", False)
    put(client, w, "admin1", "loan_officer", "staff.manage", True)
    assert client.post(f"{BASE}/permissions/reset", json={"role": "hr"}, headers=w.h["admin1"]).json() == {"removed": 1}
    assert db.query(PerformancePermissionGrant).filter_by(role="loan_officer").count() == 1
    assert client.post(f"{BASE}/permissions/reset", json={"role": "ghost"}, headers=w.h["admin1"]).status_code == 422
    assert client.post(f"{BASE}/permissions/reset", json={}, headers=w.h["admin1"]).json() == {"removed": 1}
    assert db.query(PerformancePermissionGrant).count() == 0


def test_if_the_changes_cannot_be_read_the_defaults_apply_not_everything(client, db, monkeypatch):
    w = build_world(client, db)
    monkeypatch.setattr(perm, "PerformancePermissionGrant", object)  # db.get(object, ...) raises a SQLAlchemy error
    with pytest.raises(SQLAlchemyError):
        db.get(object, ("hr", "staff.manage"))
    assert staff_users(client, w, "hr1").status_code == 200  # still the default
    assert staff_users(client, w, "lo1").status_code == 403  # and never "allow everything"
    assert audit(client, w, "hr1").status_code == 403


def test_the_menu_warnings_match_each_roles_real_menus(client, db):
    """The matrix reads every role's menus in one query; the answer must equal asking role by role, defaults and saved menus alike."""
    w = build_world(client, db)
    saved = client.put("/api/admin/role-permissions/hr", json={"allowed_menus": ["/admin/performance"]}, headers=w.h["admin1"])  # HR loses the settings menu
    assert saved.status_code == 200
    d = client.get(f"{BASE}/permissions", headers=w.h["admin1"]).json()
    checked = 0
    for cap in d["capabilities"]:
        for role, cell in cap["cells"].items():
            if role == "line_manager":
                continue
            expected = cell["allowed"] and cap["menu"] not in set(get_effective_menus_for_role(db, role))
            assert cell["menu_missing"] is expected, (role, cap["key"])
            checked += 1
    assert checked >= 27 * 5
    settings = next(c for c in d["capabilities"] if c["key"] == "settings.edit")
    assert settings["cells"]["hr"]["menu_missing"] is True  # HR may change settings but has lost the menu that opens the page
