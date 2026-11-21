"""Phase 3 - 500 staff x 24 months: reports stay quick and the query count does not grow with headcount."""

import time
from datetime import date

import pytest
from sqlalchemy import event

from app.models.performance import KpiEntry, PerformanceTarget, StaffProfile
from app.services.performance_time import today_nairobi
from performance_tests.org import build_world

BASE = "/api/admin/performance/reports"


@pytest.fixture()
def big(client, db):
    w = build_world(client, db)
    start_year = today_nairobi().year - 2
    profiles = []
    for i in range(500):
        sp = StaffProfile(full_name=f"Bulk {i}", performance_role_id=w.role.id, unit_id=(w.alpha.id if i % 2 else w.beta.id), manager_id=w.p.bm1.id, status="active")
        profiles.append(sp)
    db.add_all(profiles)
    db.flush()
    for sp in profiles:
        db.add(PerformanceTarget(profile_id=sp.id, kpi_key="sales", value=100000, effective_from=date(start_year, 1, 1), reason="bulk", set_by_username="x"))
    db.add_all([KpiEntry(profile_id=sp.id, kpi_key="sales", month=date(start_year + (m // 12), m % 12 + 1, 1), value=90000 + (hash((sp.id, m)) % 40000), status="verified", source="import", entered_by_username="x")
                for sp in profiles for m in range(24)])
    db.commit()
    return w


def count_queries(db):
    n = {"q": 0}

    def hook(*_a, **_k):
        n["q"] += 1

    event.listen(db.get_bind(), "before_cursor_execute", hook)
    return n, lambda: event.remove(db.get_bind(), "before_cursor_execute", hook)


def test_org_report_500_staff_24_months(client, big, db):
    n, stop = count_queries(db)
    t = time.perf_counter()
    r = client.get(BASE + "/organisation", params={"period": f"{today_nairobi().year - 1}"}, headers=big.h["admin1"])
    took = time.perf_counter() - t
    stop()
    assert r.status_code == 200 and r.json()["summary"]["scored_people"] >= 500
    assert n["q"] < 80, n["q"]  # fixed number of batched queries, not one per person
    assert took < 20, took
