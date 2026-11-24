"""Outlook chart series: the maths (pure) and the endpoints' scope."""

from datetime import date

import pytest

from app.models.performance import KpiEntry
from app.services import performance_outlook as po
from app.services.performance_targets import set_target
from app.services.performance_time import today_nairobi
from performance_tests.org import build_world

CATS = [
    {"key": "above_expectation", "label": "Above Expectation", "min_score": 120},
    {"key": "meeting_expectation", "label": "Meeting Expectation", "min_score": 90},
    {"key": "approaching_expectation", "label": "Approaching Expectation", "min_score": 60},
    {"key": "below_expectation", "label": "Below Expectation", "min_score": 0},
]


def trend(values, start=1):
    return [{"month": f"2026-{start + i:02d}", "actual": v, "target": 100, "pct": v, "state": "scored"} for i, v in enumerate(values)]


def test_nothing_scored_is_not_charted():
    r = po.build_outlook([{"month": "2026-01", "actual": None, "target": None, "pct": None, "state": "no_target"}], CATS)
    assert r["available"] is False and r["points"] == []


def test_fewer_than_three_months_has_a_plan_but_no_projection():
    r = po.build_outlook(trend([30, 20]), CATS)
    assert r["projection"]["available"] is False and "Insufficient history" in r["projection"]["reason"]
    assert r["plan"]["available"] is True
    assert all(p["projected"] is None for p in r["points"])


def test_projection_joins_the_last_real_point_and_has_a_range():
    r = po.build_outlook(trend([70, 65, 60, 58, 55, 54]), CATS)
    pts = {p["month"]: p for p in r["points"]}
    assert pts["2026-06"]["projected"] == 54.0 and pts["2026-06"]["low"] == pts["2026-06"]["high"] == 54.0
    for m in ("2026-07", "2026-08", "2026-09"):
        assert pts[m]["actual"] is None and pts[m]["low"] <= pts[m]["projected"] <= pts[m]["high"]
    assert r["projection"]["available"] and r["projection"]["range_pct_points"] > 0


def test_plan_is_a_straight_line_to_the_goal():
    r = po.build_outlook(trend([70, 65, 60, 58, 55, 54]), CATS)  # default goal = Meeting Expectation (90)
    plan = [p["plan"] for p in r["points"] if p["plan"] is not None]
    assert plan == [54.0, 66.0, 78.0, 90.0]
    assert r["plan"]["goal_pct"] == 90.0 and r["plan"]["goal_label"] == "Meeting Expectation"


def test_plan_length_and_goal_can_be_chosen():
    r = po.build_outlook(trend([50, 55, 60]), CATS, goal_pct=100, plan_months=5)
    plan = [p["plan"] for p in r["points"] if p["plan"] is not None]
    assert len(plan) == 6 and plan[0] == 60.0 and plan[-1] == 100.0


def test_no_plan_when_already_at_the_goal():
    r = po.build_outlook(trend([140, 141, 142]), CATS)
    assert r["plan"]["available"] is False and "already at or above" in r["plan"]["reason"]
    assert all(p["plan"] is None for p in r["points"])


def test_months_roll_over_the_year_end():
    r = po.build_outlook(trend([50, 55, 60, 58, 57, 56, 55, 54, 53, 52, 51]), CATS)
    assert [p["month"] for p in r["points"]][-3:] == ["2026-12", "2027-01", "2027-02"]


def test_skipped_months_stay_as_gaps_not_zeros():
    t = trend([60, 62, 64, 66])
    t[1] = {"month": "2026-02", "actual": None, "target": 100, "pct": None, "state": "unreported"}
    r = po.build_outlook(t, CATS)
    assert next(p for p in r["points"] if p["month"] == "2026-02")["actual"] is None


def test_same_input_gives_same_output():
    assert po.build_outlook(trend([70, 65, 60, 58]), CATS) == po.build_outlook(trend([70, 65, 60, 58]), CATS)


# ---- endpoints --------------------------------------------------------------------------------------------------------------

BASE = "/api/admin/performance/insights"
Y = today_nairobi().year
LAST = today_nairobi().month - 1


@pytest.fixture()
def world(client, db):
    if LAST < 5:
        pytest.skip("needs five complete months in the current year")
    w = build_world(client, db)
    for p, series in ((w.p.lo1, [100] * LAST), (w.p.lo2, [100] * (LAST - 3) + [60, 55, 50]), (w.p.lo3, [95] * LAST)):
        set_target(db, p, "sales", date(Y, 1, 1), 100, "seed target", "hr1")
        for m, val in enumerate(series, start=1):
            db.add(KpiEntry(profile_id=p.id, kpi_key="sales", month=date(Y, m, 1), value=val, status="verified", source="manual", entered_by_username="x"))
    db.commit()
    return w


def get(client, w, who, path, **kw):
    return client.get(BASE + path, headers=w.h[who], **kw)


def test_outlook_me_and_person_scope(client, world):
    w = world
    r = get(client, w, "lo1", "/outlook/me")
    assert r.status_code == 200 and r.json()["available"] and r.json()["points"]
    assert get(client, w, "bm1", f"/outlook/person/{w.p.lo1.id}").status_code == 200
    assert get(client, w, "bm1", f"/outlook/person/{w.p.lo3.id}").status_code == 404  # other branch
    assert get(client, w, "lo1", f"/outlook/person/{w.p.lo2.id}").status_code == 404  # peer
    assert get(client, w, "hr1", f"/outlook/person/{w.p.lo3.id}").status_code == 200
    assert get(client, w, "bm1", "/outlook/person/does-not-exist").status_code == 404


def test_falling_person_gets_projection_and_a_plan_steady_person_does_not(client, world):
    w = world
    low = get(client, w, "hr1", f"/outlook/person/{w.p.lo2.id}").json()
    assert low["projection"]["available"] and low["plan"]["available"] and low["plan"]["from_pct"] == 50.0
    steady = get(client, w, "hr1", f"/outlook/person/{w.p.lo1.id}").json()
    assert steady["projection"]["available"] and steady["plan"]["available"] is False


def test_goal_and_months_are_validated(client, world):
    w = world
    p = f"/outlook/person/{w.p.lo2.id}"
    assert get(client, w, "hr1", p, params={"goal_pct": 100, "months": 6}).json()["plan"]["months"] == 6
    assert get(client, w, "hr1", p, params={"months": 99}).status_code == 422
    assert get(client, w, "hr1", p, params={"goal_pct": 0}).status_code == 422
