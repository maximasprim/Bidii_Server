"""The idempotent step that appends the performance menus to roles an admin has already customised."""

from app.models.performance import PerformanceConfig
from app.models.role_permission import RolePermission
from app.services import performance_bootstrap as boot
from app.services import role_permissions as rp
from performance_tests.conftest import add_user, token_headers


def _rows(db):
    return {r.role: list(r.allowed_menus) for r in db.query(RolePermission)}


def test_defaults_include_performance_menus_for_every_role():
    for role, menus in rp.DEFAULT_MENU_ACCESS.items():
        assert "/admin/my-performance" in menus, role
        assert "/admin/performance" in menus, role
    assert "staff" in rp.CONFIGURABLE_ROLES
    assert "/admin/performance-settings" not in rp.DEFAULT_MENU_ACCESS["hr"]  # admin always has it


def test_existing_menus_are_never_removed_or_reordered(db):
    db.add(RolePermission(role="loan_officer", allowed_menus=["/admin/loans", "/admin/branches"]))
    db.add(RolePermission(role="hr", allowed_menus=["/admin/ats"]))
    db.commit()
    assert boot.append_menus_to_role_overrides(db) == 2
    rows = _rows(db)
    assert rows["loan_officer"] == ["/admin/loans", "/admin/branches", "/admin/my-performance", "/admin/performance"]
    assert rows["hr"][0] == "/admin/ats"


def test_rerun_changes_nothing_and_never_duplicates(db):
    db.add(RolePermission(role="loan_officer", allowed_menus=["/admin/loans"]))
    db.commit()
    boot.append_menus_to_role_overrides(db)
    first = _rows(db)
    assert boot.append_menus_to_role_overrides(db) == 0
    assert boot.append_menus_to_role_overrides(db) == 0
    assert _rows(db) == first
    assert len(first["loan_officer"]) == len(set(first["loan_officer"]))


def test_path_already_present_is_not_duplicated(db):
    db.add(RolePermission(role="hr", allowed_menus=["/admin/my-performance", "/admin/ats"]))
    db.commit()
    boot.append_menus_to_role_overrides(db)
    assert _rows(db)["hr"] == ["/admin/my-performance", "/admin/ats", "/admin/performance"]


def test_a_menu_an_admin_later_removes_is_not_re_added(db):
    row = RolePermission(role="loan_officer", allowed_menus=["/admin/loans"])
    db.add(row)
    db.commit()
    boot.append_menus_to_role_overrides(db)
    row.allowed_menus = ["/admin/loans", "/admin/performance"]  # admin un-ticked My Performance
    db.commit()
    boot.append_menus_to_role_overrides(db)
    assert "/admin/my-performance" not in _rows(db)["loan_officer"]


def test_a_newly_introduced_path_is_appended_once_later(db, monkeypatch):
    db.add(RolePermission(role="loan_officer", allowed_menus=["/admin/loans"]))
    db.commit()
    boot.append_menus_to_role_overrides(db)
    monkeypatch.setattr(boot, "PERFORMANCE_DEFAULT_PATHS", [*boot.PERFORMANCE_DEFAULT_PATHS, "/admin/performance-new"])
    assert boot.append_menus_to_role_overrides(db) == 1
    assert _rows(db)["loan_officer"].count("/admin/performance-new") == 1
    assert boot.append_menus_to_role_overrides(db) == 0


def test_roles_without_an_override_are_untouched_and_marker_is_recorded(db):
    assert boot.append_menus_to_role_overrides(db) == 0
    assert db.query(RolePermission).count() == 0  # defaults already cover them
    marker = db.get(PerformanceConfig, boot.MENU_MIGRATION_KEY)
    assert set(marker.value["appended"]) == {"/admin/my-performance", "/admin/performance"}


def test_role_permissions_endpoint_lists_the_new_menus_and_staff_role(client, db):
    admin = add_user(db, "admin1", "admin")
    r = client.get("/api/admin/role-permissions", headers=token_headers(admin))
    assert r.status_code == 200, r.text
    body = r.text
    assert "/admin/my-performance" in body and "/admin/performance-settings" in body and '"staff"' in body
