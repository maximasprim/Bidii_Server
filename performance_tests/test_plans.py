"""Saved improvement plans: the comparison maths (pure), the one-active-plan rule, scope, idempotency, and privacy."""

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.performance import KpiEntry, PerformancePlan, Recommendation
from app.services import performance_governance as gov
from app.services import performance_outlook as po
from app.services.performance_targets import set_target
from app.services.performance_time import today_nairobi
from performance_tests.org import build_world

# ---- pure -------------------------------------------------------------------------------------------------------------------

PATH = [{"month": "2026-06", "pct": 54.0}, {"month": "2026-07", "pct": 66.0}, {"month": "2026-08", "pct": 78.0}, {"month": "2026-09", "pct": 90.0}]


def test_evaluate_awaiting_until_a_month_after_the_start_is_scored():
    r = po.evaluate_plan(PATH, 90.0, {"2026-05": 55.0, "2026-06": 54.0})
    assert r["state"] == "awaiting" and r["latest"] is None
    assert [x["month"] for x in r["rows"]] == ["2026-07", "2026-08", "2026-09"]


def test_evaluate_on_track_and_behind_use_the_months_planned_figure():
    ahead = po.evaluate_plan(PATH, 90.0, {"2026-06": 54.0, "2026-07": 70.0})
    assert ahead["state"] == "on_track" and ahead["latest"] == {"month": "2026-07", "actual": 70.0, "planned": 66.0, "gap": 4.0}
    behind = po.evaluate_plan(PATH, 90.0, {"2026-06": 54.0, "2026-07": 60.0})
    assert behind["state"] == "behind" and behind["latest"]["gap"] == -6.0
    exactly = po.evaluate_plan(PATH, 90.0, {"2026-06": 54.0, "2026-07": 66.0})
    assert exactly["state"] == "on_track"


def test_evaluate_reached_beats_everything_and_can_happen_early():
    r = po.evaluate_plan(PATH, 90.0, {"2026-06": 54.0, "2026-07": 95.0})
    assert r["state"] == "reached"


def test_evaluate_ended_not_reached_once_the_last_month_is_scored_below_goal():
    r = po.evaluate_plan(PATH, 90.0, {"2026-06": 54.0, "2026-07": 60.0, "2026-08": 70.0, "2026-09": 80.0})
    assert r["state"] == "ended_not_reached"
    later = po.evaluate_plan(PATH, 90.0, {"2026-06": 54.0, "2026-10": 70.0})  # a later month scored, the planned ones skipped
    assert later["state"] == "ended_not_reached" and later["latest"]["planned"] == 90.0


def test_evaluate_ignores_months_before_the_start_and_handles_a_degenerate_path():
    assert po.evaluate_plan(PATH, 90.0, {"2026-01": 99.0})["state"] == "awaiting"
    assert po.evaluate_plan([], 90.0, {"2026-07": 1.0})["state"] == "awaiting"


def test_path_and_actuals_helpers_and_attaching_to_the_chart():
    trend = [{"month": f"2026-{i:02d}", "actual": v, "target": 100, "pct": v, "state": "scored"} for i, v in enumerate([70, 65, 60, 58, 55, 54], start=1)]
    cats = [{"key": "meeting_expectation", "label": "Meeting Expectation", "min_score": 90}, {"key": "below_expectation", "label": "Below", "min_score": 0}]
    res = po.build_outlook(trend, cats)
    path = po.plan_path(res)
    assert [p["pct"] for p in path] == [54.0, 66.0, 78.0, 90.0] and path[0]["month"] == "2026-06"
    assert po.actuals_by_month(res)["2026-06"] == 54.0
    later = po.build_outlook(trend + [{"month": "2026-07", "actual": 60, "target": 100, "pct": 60, "state": "scored"}], cats)  # path now starts later...
    drawn = po.attach_saved_plan(later, path)  # ...but the SAVED path is still drawn as saved
    pts = {p["month"]: p for p in drawn["points"]}
    assert [pts[m]["saved_plan"] for m in ("2026-06", "2026-07", "2026-08", "2026-09")] == [54.0, 66.0, 78.0, 90.0]
    assert pts["2026-05"]["saved_plan"] is None
    assert [p["month"] for p in drawn["points"]] == sorted(p["month"] for p in drawn["points"])
    assert po.attach_saved_plan(res, [])["points"] == res["points"]  # nothing saved, nothing changed


# ---- database and endpoints ---------------------------------------------------------------------------------------------------

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


def post(client, w, who, path, body):
    return client.post(BASE + path, json=body, headers=w.h[who])


def get(client, w, who, path):
    return client.get(BASE + path, headers=w.h[who])


def test_save_plan_stores_the_server_computed_path(client, db, world):
    w = world
    r = post(client, w, "bm1", "/plans", {"profile_id": w.p.lo2.id, "months": 3, "note": "Weekly coaching"})
    assert r.status_code == 201, r.text
    plan = r.json()["plan"]
    assert r.json()["created"] and plan["status"] == "active" and plan["goal_pct"] == 90.0 and plan["start_pct"] == 50.0
    assert [p["pct"] for p in plan["path"]][0] == 50.0 and plan["path"][-1]["pct"] == 90.0 and len(plan["path"]) == 4
    assert plan["note"] == "Weekly coaching" and plan["evaluation"]["state"] == "awaiting"


def test_browser_cannot_supply_its_own_numbers(client, world):
    w = world
    r = post(client, w, "bm1", "/plans", {"profile_id": w.p.lo2.id, "path": [{"month": "2026-01", "pct": 1}], "goal_pct": 90})
    assert r.status_code == 422  # unknown field refused


def test_saving_twice_is_harmless_and_a_different_plan_supersedes(client, db, world):
    w = world
    body = {"profile_id": w.p.lo2.id, "goal_pct": 90, "months": 3}
    first = post(client, w, "bm1", "/plans", body)
    again = post(client, w, "bm1", "/plans", body)
    assert first.status_code == 201 and again.status_code == 200 and again.json()["created"] is False
    assert again.json()["plan"]["id"] == first.json()["plan"]["id"]
    assert db.query(PerformancePlan).filter_by(profile_id=w.p.lo2.id).count() == 1
    other = post(client, w, "bm1", "/plans", {"profile_id": w.p.lo2.id, "goal_pct": 60, "months": 4})
    assert other.status_code == 201
    rows = {p.status for p in db.query(PerformancePlan).filter_by(profile_id=w.p.lo2.id)}
    assert rows == {"active", "superseded"}
    old = db.get(PerformancePlan, first.json()["plan"]["id"])
    assert old.close_reason == "superseded" and old.active_slot is None and old.result["state"] == "awaiting"


def test_database_refuses_a_second_active_plan(db, world):
    w = world
    common = dict(profile_id=w.p.lo2.id, status="active", active_slot=1, goal_pct=90.0, start_month="2026-01", start_pct=50.0, plan_months=3, path=[], created_by_username="x")
    db.add(PerformancePlan(**common))
    db.commit()
    db.add(PerformancePlan(**common))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    for _ in range(3):  # any number of past plans is fine
        db.add(PerformancePlan(**{**common, "status": "closed", "active_slot": None}))
    db.commit()


def test_no_plan_when_already_at_the_goal_or_nothing_scored(client, world):
    w = world
    r = post(client, w, "bm1", "/plans", {"profile_id": w.p.lo1.id})  # lo1 is steady at 100%
    assert r.status_code == 422 and "already at or above" in r.json()["detail"]


def test_who_may_save_close_and_see(client, world):
    w = world
    body = {"profile_id": w.p.lo2.id}
    assert post(client, w, "lo2", "/plans", body).status_code == 403  # not your own
    assert post(client, w, "lo1", "/plans", body).status_code == 404  # a peer you can't see
    assert post(client, w, "ceo", "/plans", body).status_code in (403, 404)  # the executive view never changes anything
    assert post(client, w, "bm1", "/plans", {"profile_id": w.p.lo3.id}).status_code == 404  # another branch
    made = post(client, w, "hr1", "/plans", body)
    assert made.status_code == 201
    pid = made.json()["plan"]["id"]
    assert post(client, w, "lo2", f"/plans/{pid}/close", {}).status_code == 403
    assert post(client, w, "lo1", f"/plans/{pid}/close", {}).status_code == 404
    assert post(client, w, "bm1", "/plans/nope/close", {}).status_code == 404
    # the person can see their own plan, but not the manager's note or who saved it
    post(client, w, "hr1", "/plans", {**body, "goal_pct": 80, "note": "private note"})
    mine = get(client, w, "lo2", f"/outlook/person/{w.p.lo2.id}").json()
    assert mine["tracked_plan"] is not None and mine["can_manage_plan"] is False
    assert "note" not in mine["tracked_plan"] and "created_by" not in mine["tracked_plan"]
    theirs = get(client, w, "bm1", f"/outlook/person/{w.p.lo2.id}").json()
    assert theirs["can_manage_plan"] is True and theirs["tracked_plan"]["note"] == "private note"


def test_suggestion_must_belong_to_the_same_person(client, db, world):
    w = world
    client.post(BASE + "/recommendations/generate", headers=w.h["hr1"])
    rec = db.query(Recommendation).filter_by(entity_id=w.p.lo2.id).first()
    other = db.query(Recommendation).filter(Recommendation.entity_id != w.p.lo2.id, Recommendation.entity_type == "staff").first()
    if rec is None:
        pytest.skip("rules did not raise a suggestion for this data")
    ok = post(client, w, "hr1", "/plans", {"profile_id": w.p.lo2.id, "recommendation_id": rec.id})
    assert ok.status_code == 201 and ok.json()["plan"]["recommendation_id"] == rec.id
    assert post(client, w, "hr1", "/plans", {"profile_id": w.p.lo2.id, "recommendation_id": "nope"}).status_code == 422
    if other is not None:
        assert post(client, w, "hr1", "/plans", {"profile_id": w.p.lo2.id, "goal_pct": 70, "recommendation_id": other.id}).status_code == 422
    assert db.get(Recommendation, rec.id).outcome is None  # the suggestion's own follow-up field is left alone


def test_chart_draws_the_saved_plan_and_tracks_new_months(client, db, world):
    w = world
    post(client, w, "bm1", "/plans", {"profile_id": w.p.lo2.id, "goal_pct": 90, "months": 3})
    before = get(client, w, "bm1", f"/outlook/person/{w.p.lo2.id}").json()
    assert before["tracked_plan"]["evaluation"]["state"] == "awaiting"
    assert [p["saved_plan"] for p in before["points"] if p["saved_plan"] is not None][0] == 50.0
    db.add(KpiEntry(profile_id=w.p.lo2.id, kpi_key="sales", month=date(Y, LAST + 1, 1), value=80, status="verified", source="manual", entered_by_username="x"))
    db.commit()
    after = get(client, w, "bm1", f"/outlook/person/{w.p.lo2.id}").json()
    ev = after["tracked_plan"]["evaluation"]
    assert ev["state"] == "on_track" and ev["latest"]["actual"] == 80.0 and ev["latest"]["gap"] > 0  # 80% against the ~63% the path asked for
    assert [p["saved_plan"] for p in after["points"] if p["saved_plan"] is not None] == [p["saved_plan"] for p in before["points"] if p["saved_plan"] is not None]  # frozen


def test_close_keeps_a_record_and_frees_the_slot(client, db, world):
    w = world
    pid = post(client, w, "bm1", "/plans", {"profile_id": w.p.lo2.id}).json()["plan"]["id"]
    r = post(client, w, "bm1", f"/plans/{pid}/close", {"note": "Moved to another role"})
    assert r.status_code == 200 and r.json()["plan"]["status"] == "closed"
    assert post(client, w, "bm1", f"/plans/{pid}/close", {}).status_code == 409  # already closed
    row = db.get(PerformancePlan, pid)
    assert row.active_slot is None and row.close_reason == "manual" and row.result["state"] == "awaiting"
    assert get(client, w, "bm1", f"/outlook/person/{w.p.lo2.id}").json()["tracked_plan"] is None
    again = post(client, w, "bm1", "/plans", {"profile_id": w.p.lo2.id})
    assert again.status_code == 201
    hist = get(client, w, "bm1", f"/plans/person/{w.p.lo2.id}").json()
    assert hist["can_manage"] and len(hist["items"]) == 2


def test_a_plan_problem_never_takes_the_chart_down(client, db, world, monkeypatch):
    from sqlalchemy.exc import OperationalError

    import app.routers.admin_performance_insights as router

    def boom(*a, **k):
        raise OperationalError("select", {}, Exception("table missing"))

    monkeypatch.setattr(router.plans, "active_plan", boom)
    r = get(client, world, "bm1", f"/outlook/person/{world.p.lo2.id}")
    assert r.status_code == 200 and r.json()["available"] and r.json()["plans_unavailable"] is True and r.json()["tracked_plan"] is None


def test_anonymising_a_person_clears_the_text_on_their_plans(client, db, monkeypatch):
    from performance_tests.test_governance import TODAY, _leaver
    import app.routers.admin_performance_entries as em
    import app.routers.admin_performance_governance as gm

    monkeypatch.setattr(em, "today_nairobi", lambda: TODAY)
    monkeypatch.setattr(gm, "today_nairobi", lambda: TODAY)
    monkeypatch.setattr(gov.storage, "remove", lambda path: None)
    w = build_world(client, db)
    p = _leaver(db, w, 8)
    db.add(PerformancePlan(profile_id=p.id, status="closed", goal_pct=90.0, start_month="2019-01", start_pct=50.0, plan_months=3, path=[{"month": "2019-01", "pct": 50.0}, {"month": "2019-02", "pct": 90.0}],
                           note="coach on closing", created_by_username="bm1", close_note="left the company", close_reason="manual"))
    db.commit()
    assert client.post(f"/api/admin/performance/governance/retention/{p.id}/anonymise", json={"confirm": "ANONYMISE"}, headers=w.h["admin1"]).status_code == 200
    row = db.query(PerformancePlan).filter_by(profile_id=p.id).one()
    assert row.note is None and row.close_note is None and row.goal_pct == 90.0  # the numbers stay
