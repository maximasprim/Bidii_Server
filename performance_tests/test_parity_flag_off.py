"""
With PERFORMANCE_MODULE_ENABLED=false the app must be the ORIGINAL app.

golden_flag_off.json was captured from the untouched pre-module codebase
(routes, mounts, DB tables, menu registry, roles, default menus, the AdminRole
enum). Run only under PERF_TEST_FLAG=off (see run.sh).
"""

import json
from pathlib import Path
from typing import get_args

import pytest

from performance_tests.conftest import FLAG_ON, add_user, token_headers

pytestmark = pytest.mark.skipif(FLAG_ON, reason="parity checks run only with the module disabled")

GOLDEN = json.loads((Path(__file__).parent / "golden_flag_off.json").read_text())


def test_routes_and_mounts_are_exactly_the_original():
    from app.main import app

    routes = sorted([r.path, sorted(r.methods)] for r in app.routes if hasattr(r, "methods") and r.methods)
    assert routes == GOLDEN["routes"]
    assert sorted(r.path for r in app.routes if not hasattr(r, "methods")) == GOLDEN["mounts"]
    assert not [r for r in routes if "performance" in r[0]]


def test_no_performance_tables_exist():
    from app.database import Base

    assert sorted(Base.metadata.tables) == GOLDEN["tables"]
    assert "staff_profiles" not in Base.metadata.tables


def test_menus_roles_and_defaults_are_exactly_the_original():
    from app.services import role_permissions as rp

    assert [list(x) for x in rp.MENU_REGISTRY] == GOLDEN["menu_registry"]
    assert list(rp.CONFIGURABLE_ROLES) == GOLDEN["configurable_roles"]
    assert list(rp.ALL_ROLES) == GOLDEN["all_roles"]
    assert {k: list(v) for k, v in rp.DEFAULT_MENU_ACCESS.items()} == GOLDEN["default_menu_access"]


def test_admin_role_enum_is_the_original_and_rejects_staff():
    from pydantic import ValidationError

    from app.schemas.admin_user import AdminUserCreate

    assert list(get_args(AdminUserCreate.model_fields["role"].annotation)) == GOLDEN["admin_roles"]
    with pytest.raises(ValidationError):
        AdminUserCreate(username="someone", password="longenough123", role="staff")


def test_module_code_is_never_imported_when_disabled():
    import sys

    from app.main import app  # noqa: F401

    assert "app.models.performance" not in sys.modules
    assert "app.routers.admin_performance" not in sys.modules


def test_role_permissions_endpoint_has_no_performance_entries(client, db):
    admin = add_user(db, "admin1", "admin")
    r = client.get("/api/admin/role-permissions", headers=token_headers(admin))
    assert r.status_code == 200
    assert "performance" not in r.text.lower() and '"staff"' not in r.text
    assert client.get("/api/admin/performance/me", headers=token_headers(admin)).status_code == 404
