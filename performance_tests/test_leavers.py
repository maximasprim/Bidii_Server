"""People who have left: the months they worked still count in branch / region / organisation / team totals, but never as people."""

from datetime import date

import pytest

from app.models.performance import KpiEntry
from app.services.performance_targets import set_target
from app.services.performance_time import today_nairobi
from performance_tests.org import build_world

BASE = "/api/admin/performance/reports"
Y = today_nairobi().year
LAST = today_nairobi().month - 1  # the last complete month


@pytest.fixture()
def world(client, db):
    if LAST < 6:
        pytest.skip("needs six complete months in the current year")
    w = build_world(client, db)
    for p, series in ((w.p.lo1, [100] * LAST), (w.p.lo2, [100] * (LAST - 3) + [60, 55, 50]), (w.p.lo3, [95] * LAST)):
        set_target(db, p, "sales", date(Y, 1, 1), 100, "seed target", "hr1")
        for m, val in enumerate(series, start=1):
            db.add(KpiEntry(profile_id=p.id, kpi_key="sales", month=date(Y, m, 1), value=val, status="verified", source="manual", entered_by_username="x"))
    db.commit()
    return w


def get(client, w, who, path, **params):
    r = client.get(f"{BASE}{path}", params=params, headers=w.h[who])
    assert r.status_code == 200, r.text
    return r.json()


def leave(db, person, exit_on):
    person.status, person.exit_date = "exited", exit_on
    db.commit()


def unit_totals(client, w):
    d = get(client, w, "hr1", f"/unit/{w.alpha.id}", period="ytd")
    return d, d["summary"]


def test_someone_leaving_does_not_change_the_branch_totals(client, db, world):
    w = world
    before, s0 = unit_totals(client, w)
    assert s0["leavers_included"] == 0 and s0["actual_total"] > 0
    leave(db, w.p.lo2, date(Y, LAST, 28))  # left at the end of the last complete month: all their months count
    after, s1 = unit_totals(client, w)
    assert (s1["actual_total"], s1["target_total"], s1["achievement_pct"]) == (s0["actual_total"], s0["target_total"], s0["achievement_pct"])
    assert s1["leavers_included"] == 1


def test_a_leaver_is_not_a_person_in_the_lists_or_counts(client, db, world):
    w = world
    before, s0 = unit_totals(client, w)
    leave(db, w.p.lo2, date(Y, LAST, 28))
    after, s1 = unit_totals(client, w)
    assert w.p.lo2.id in {r["profile_id"] for r in before["people"]}
    assert w.p.lo2.id not in {r["profile_id"] for r in after["people"]}
    assert s1["people"] == s0["people"] - 1 and s1["scored_people"] == s0["scored_people"] - 1
    assert sum(s1["category_counts"].values()) == sum(s0["category_counts"].values()) - 1  # categories and the average score are current staff only


def test_months_after_the_exit_month_never_count(client, db, world):
    w = world
    _, s0 = unit_totals(client, w)
    leave(db, w.p.lo2, date(Y, LAST - 2, 15))  # left in the middle of the month their figures started to fall
    _, s1 = unit_totals(client, w)
    # their last two months (55 and 50, each against a target of 100) are no longer counted
    assert round(s0["actual_total"] - s1["actual_total"], 2) == 105.0
    assert round(s0["target_total"] - s1["target_total"], 2) == 200.0
    assert s1["leavers_included"] == 1


def test_a_deactivated_person_is_treated_the_same_way(client, db, world):
    w = world
    _, s0 = unit_totals(client, w)
    w.p.lo2.is_active = False
    db.commit()
    _, s1 = unit_totals(client, w)
    assert s1["actual_total"] == s0["actual_total"] and s1["leavers_included"] == 1


def test_organisation_region_and_team_totals_keep_the_leavers_months(client, db, world):
    w = world
    org0 = get(client, w, "admin1", "/organisation", period="ytd")["summary"]
    team0 = get(client, w, "bm1", "/team", period="ytd", leader_id=w.p.bm1.id)["summary"]
    leave(db, w.p.lo2, date(Y, LAST, 28))
    org1 = get(client, w, "admin1", "/organisation", period="ytd")
    team1 = get(client, w, "bm1", "/team", period="ytd", leader_id=w.p.bm1.id)["summary"]
    assert (org1["summary"]["actual_total"], org1["summary"]["target_total"]) == (org0["actual_total"], org0["target_total"]) and org1["summary"]["leavers_included"] == 1
    assert (team1["actual_total"], team1["target_total"]) == (team0["actual_total"], team0["target_total"]) and team1["leavers_included"] == 1
    assert org1["kes"]["achievement_pct"] == org0["achievement_pct"]
    assert sum(b["actual_total"] for b in org1["branches"]) > 0


def test_compare_and_month_by_month_keep_them_too(client, db, world):
    w = world
    cmp0 = get(client, w, "hr1", "/compare", a="ytd", b=f"{Y}-Q1", unit_id=w.alpha.id)
    by0 = get(client, w, "admin1", "/organisation", period="ytd")["by_month"]
    leave(db, w.p.lo2, date(Y, LAST, 28))
    cmp1 = get(client, w, "hr1", "/compare", a="ytd", b=f"{Y}-Q1", unit_id=w.alpha.id)
    by1 = get(client, w, "admin1", "/organisation", period="ytd")["by_month"]
    assert cmp1["a"]["actual_total"] == cmp0["a"]["actual_total"] and cmp1["b"]["actual_total"] == cmp0["b"]["actual_total"]
    assert [(m["month"], m["actual_total"], m["target_total"]) for m in by1] == [(m["month"], m["actual_total"], m["target_total"]) for m in by0]


def test_nothing_changes_when_nobody_has_left(client, world):
    w = world
    _, s = unit_totals(client, w)
    assert s["leavers_included"] == 0
    assert get(client, w, "admin1", "/organisation", period="ytd")["summary"]["leavers_included"] == 0


def test_the_branch_report_lists_who_has_left_and_what_they_add(client, db, world):
    w = world
    leave(db, w.p.lo2, date(Y, LAST - 2, 15))
    d, s = unit_totals(client, w)
    rows = s["leavers"]
    assert [r["profile_id"] for r in rows] == [w.p.lo2.id] and s["leavers_included"] == 1
    r = rows[0]
    assert r["reason"] == "left" and r["exit_date"] == date(Y, LAST - 2, 15).isoformat()
    assert r["months_counted"] == LAST - 2 and r["first_month"] == f"{Y}-01" and r["last_month"] == f"{Y}-{LAST - 2:02d}"
    assert r["target"] == 100 * (LAST - 2) and r["actual"] == 100 * (LAST - 3) + 60
    assert w.p.lo2.id not in {x["profile_id"] for x in d["people"]}


def test_nobody_is_listed_when_nobody_has_left(client, world):
    assert unit_totals(client, world)[1]["leavers"] == []


def test_the_list_shows_only_people_in_that_report(client, db, world):
    w = world
    leave(db, w.p.lo2, date(Y, LAST, 28))
    other = get(client, w, "hr1", f"/unit/{w.beta.id}", period="ytd")["summary"]
    assert w.p.lo2.id not in {r["profile_id"] for r in other["leavers"]}  # lo2 only ever worked in the first branch
    assert w.p.lo2.id in {r["profile_id"] for r in get(client, w, "admin1", "/organisation", period="ytd")["summary"]["leavers"]}


def test_no_new_improvement_plan_for_someone_who_has_left(client, db, world):
    w = world
    leave(db, w.p.lo2, date(Y, LAST, 28))
    out = client.get(f"/api/admin/performance/insights/outlook/person/{w.p.lo2.id}", headers=w.h["hr1"])
    assert out.status_code == 200 and out.json()["can_manage_plan"] is False
    r = client.post("/api/admin/performance/insights/plans", json={"profile_id": w.p.lo2.id}, headers=w.h["hr1"])
    assert r.status_code == 422 and "has left" in r.json()["detail"]
