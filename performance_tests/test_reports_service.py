"""Phase 3 - the scoring/reporting service (no HTTP)."""

from datetime import date
from decimal import Decimal

import pytest

from app.models.performance import KpiEntry, StaffAssignmentHistory
from app.services import performance_reports as pr
from app.services.performance_targets import set_target
from app.services.performance_time import today_nairobi
from performance_tests.org import build_world

YEAR = today_nairobi().year - 1
TODAY = date(YEAR + 1, 6, 15)


@pytest.fixture()
def world(client, db):
    return build_world(client, db)


def m(n):
    return date(YEAR, n, 1)


def entry(db, p, key, month, value, status="verified"):
    db.add(KpiEntry(profile_id=p.id, kpi_key=key, month=month, value=value, status=status, source="manual", entered_by_username="x"))
    db.commit()


def target(db, p, key, value, month=None):
    set_target(db, p, key, month or m(1), value, "test target", "hr1")


def run(db, ids, key):
    period = pr.resolve_period(key, TODAY)
    ctx = pr.load_ctx(db, ids, period.start, period.end, today=TODAY)
    return ctx, period, pr.score_many(ctx, ids, period)


def test_period_keys():
    assert pr.resolve_period("ytd", date(2026, 5, 20)).start == date(2026, 1, 1)
    c = pr.resolve_period("2026-01..2026-03", TODAY)
    assert (c.start, c.end) == (date(2026, 1, 1), date(2026, 3, 31))
    with pytest.raises(pr.PeriodError):
        pr.resolve_period("2026-05..2026-01", TODAY)
    with pytest.raises(pr.PeriodError):
        pr.resolve_period("2020-01..2026-12", TODAY)
    prev = pr.previous_period(pr.resolve_period(f"{YEAR}-Q2", TODAY))
    assert (prev.start, prev.end) == (date(YEAR, 1, 1), date(YEAR, 3, 31))


def test_person_period_is_sum_over_sum_and_ignores_unverified(db, world):
    lo = world.p.lo1
    target(db, lo, "sales", 100)
    entry(db, lo, "sales", m(1), 50)
    entry(db, lo, "sales", m(2), 150)
    entry(db, lo, "sales", m(3), 999, status="submitted")
    _, _, (r,) = run(db, [lo.id], f"{YEAR}-Q1")
    k = next(x for x in r["kpis"] if x["key"] == "sales")
    assert k["actual"] == 200 and k["target"] == 200 and k["achievement_pct"] == 100
    assert k["months_pending"] == 1 and k["months_scored"] == 2
    assert r["headline"]["pct"] == 100
    assert r["legacy_band"] == "80%-99%"  # exactly 100 is not ">100%"


def test_no_target_means_not_scored_not_zero(db, world):
    lo = world.p.lo2
    entry(db, lo, "sales", m(1), 500)
    _, _, (r,) = run(db, [lo.id], f"{YEAR}-01")
    assert r["score"] is None and r["category"] is None
    k = next(x for x in r["kpis"] if x["key"] == "sales")
    assert "target" in k["not_scored_reason"].lower()


def test_transfer_keeps_months_with_the_unit_they_were_in(db, world):
    lo = world.p.lo1
    target(db, lo, "sales", 100)
    for n in (1, 2):
        entry(db, lo, "sales", m(n), 100)
    db.add_all([
        StaffAssignmentHistory(profile_id=lo.id, unit_id=world.alpha.id, effective_from=date(YEAR - 1, 1, 1), effective_to=date(YEAR, 1, 31)),
        StaffAssignmentHistory(profile_id=lo.id, unit_id=world.beta.id, effective_from=date(YEAR, 2, 1), effective_to=None),
    ])
    lo.unit_id = world.beta.id
    db.commit()
    period = pr.resolve_period(f"{YEAR}-Q1", TODAY)
    ctx = pr.load_ctx(db, [lo.id], period.start, period.end, today=TODAY)
    a = pr.score_person(ctx, lo.id, period, unit_filter=world.alpha.id)
    b = pr.score_person(ctx, lo.id, period, unit_filter=world.beta.id)
    assert next(k for k in a["kpis"] if k["key"] == "sales")["months_scored"] == 1
    assert next(k for k in b["kpis"] if k["key"] == "sales")["months_scored"] == 1
    assert world.p.lo1.id in pr.unit_member_ids(db, world.alpha.id, period.start, period.end, None)


def test_group_rollup_is_not_an_average_of_percentages(db, world):
    a, b = world.p.lo1, world.p.lo2
    target(db, a, "sales", 100)
    target(db, b, "sales", 900)
    entry(db, a, "sales", m(1), 200)  # 200%
    entry(db, b, "sales", m(1), 450)  # 50%
    ctx, _, res = run(db, [a.id, b.id], f"{YEAR}-01")
    g = pr.group_summary(ctx, res)
    assert g["achievement_pct"] == 65  # 650/1000, not 125
    assert sum(g["category_counts"].values()) == 2


def test_reporting_line_is_transitive(db, world):
    ids = pr.reporting_line(db, world.p.gm.id)
    assert {world.p.bm1.id, world.p.lo1.id, world.p.lo3.id} <= set(ids) and world.p.gm.id not in ids


def test_public_view_hides_working_fields(db, world):
    _, _, (r,) = run(db, [world.p.lo1.id], f"{YEAR}-01")
    assert not [k for k in pr.public(r) if k.startswith("_")]
