"""Phase 5 - cross-cutting checks: spreadsheet formula injection, every route's guard, the cron route, and speed at 500 staff x 24 months."""

import re
import time
from datetime import date

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import event

from app.main import app
from app.models.performance import KpiEntry, PerformanceTarget, StaffProfile
from app.services.performance_export import safe_cell
from app.services.performance_time import today_nairobi
from performance_tests.org import build_world


@pytest.mark.parametrize("raw,expected", [
    ("=1+1", "'=1+1"), ("+SUM(A1)", "'+SUM(A1)"), ("-2+3", "'-2+3"), ("@SUM(A1)", "'@SUM(A1)"), ("\t=1", "'\t=1"), ("\r=1", "'\r=1"),
    ("  =cmd|' /C calc'!A0", "'  =cmd|' /C calc'!A0"), ("Plain text", "Plain text"), ("Mary O'Neil", "Mary O'Neil"), (123, 123), (12.5, 12.5), (None, None),
])
def test_safe_cell(raw, expected):
    assert safe_cell(raw) == expected


def _routes(prefix):
    return [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith(prefix)]


def test_new_modules_are_in_the_enumeration():
    paths = {r.path for r in _routes("/api/admin/performance")}
    for needle in ("/insights/recommendations", "/governance/locks", "/ops/actions/open", "/reports/organisation"):
        assert any(needle in p for p in paths), needle
    assert len(paths) >= 100


def test_cron_route_is_the_only_performance_route_outside_the_menu_system(client):
    internal = _routes("/api/internal/performance")
    assert sorted(r.path for r in internal) == ["/api/internal/performance/daily-digest", "/api/internal/performance/monthly-pack", "/api/internal/performance/weekly-digest"]
    assert client.post("/api/internal/performance/daily-digest").status_code in (401, 404)
    for method in ("get", "put", "delete", "patch"):
        assert getattr(client, method)("/api/internal/performance/daily-digest").status_code == 405


def test_every_write_route_rejects_unknown_fields_or_has_no_body():
    """Mass-assignment guard: JSON bodies of the new modules forbid extra fields."""
    from pydantic import BaseModel

    bad = []
    for prefix in ("/api/admin/performance/insights", "/api/admin/performance/governance", "/api/admin/performance/ops"):
        for r in _routes(prefix):
            for f in r.dependant.body_params:
                model = f.field_info.annotation
                if isinstance(model, type) and issubclass(model, BaseModel) and model.model_config.get("extra") != "forbid":
                    bad.append(f"{r.path}: {model.__name__}")
    assert bad == []


@pytest.fixture()
def big(client, db):
    w = build_world(client, db)
    y = today_nairobi().year - 1
    profiles = [StaffProfile(full_name=f"Bulk {i}", performance_role_id=w.role.id, unit_id=(w.alpha.id if i % 2 else w.beta.id), manager_id=(w.p.bm1.id if i % 3 else w.p.bm2.id), status="active") for i in range(500)]
    db.add_all(profiles)
    db.flush()
    db.add_all([PerformanceTarget(profile_id=p.id, kpi_key="sales", value=100000, effective_from=date(y, 1, 1), reason="bulk", set_by_username="x") for p in profiles])
    db.add_all([KpiEntry(profile_id=p.id, kpi_key="sales", month=date(y + (m // 12), m % 12 + 1, 1), value=60000 + (hash((p.id, m)) % 70000), status="verified", source="import", entered_by_username="x")
                for p in profiles for m in range(24)])
    db.commit()
    return w


def _timed(db, fn):
    n = {"q": 0}
    hook = lambda *_a, **_k: n.__setitem__("q", n["q"] + 1)  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", hook)
    t = time.perf_counter()
    try:
        return fn(), time.perf_counter() - t, n["q"]
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", hook)


@pytest.mark.parametrize("path", ["/ops/calibration", "/ops/data-quality", "/ops/anomalies", "/ops/leaderboard/units", "/ops/training-themes", "/insights/projection/organisation"])
def test_new_screens_stay_fast_at_500_staff(client, db, big, path):
    r, took, queries = _timed(db, lambda: client.get("/api/admin/performance" + path, headers=big.h["hr1"] if "organisation" not in path else big.h["admin1"]))
    assert r.status_code == 200, r.text
    assert took < 15 and queries < 120, (path, round(took, 1), queries)


def test_generate_suggestions_for_500_staff_is_bounded(client, db, big):
    r, took, queries = _timed(db, lambda: client.post("/api/admin/performance/insights/recommendations/generate", headers=big.h["hr1"]))
    assert r.status_code == 200 and r.json()["people"] >= 500
    assert took < 30 and queries < 200, (round(took, 1), queries)
    again, _t, _q = _timed(db, lambda: client.post("/api/admin/performance/insights/recommendations/generate", headers=big.h["hr1"]))
    assert again.json()["created"] == 0
