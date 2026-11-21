"""Phase 4b-2 - action tracker, digest, anomalies, data quality, calibration, leaderboard, timeline, training themes."""

from datetime import date, datetime, timezone

import pytest

import app.routers.admin_performance_ops as ops_router
from app.models.admin_user import AdminUser
from app.models.internal_notification import InternalNotification
from app.models.performance import Appraisal, DevelopmentNeed, KpiEntry, PerformanceAction, PerformanceAuditLog
from app.services import performance_ops as ops
from app.services.performance_targets import set_target
from performance_tests.org import build_world

O = "/api/admin/performance/ops"
TODAY = date(2026, 11, 18)


@pytest.fixture()
def w(client, db, monkeypatch):
    monkeypatch.setattr(ops_router, "today_nairobi", lambda: TODAY)
    return build_world(client, db)


def act(db, p, typ="coaching", restricted=False, review=date(2026, 11, 1), outcome=None):
    a = PerformanceAction(profile_id=p.id, action_type=typ, restricted=restricted, started_on=date(2026, 10, 1), review_on=review, outcome=outcome, note="n")
    db.add(a)
    db.commit()
    return a


def test_tracker_visibility_and_update(client, db, w):
    c = act(db, w.p.lo1)
    pip = act(db, w.p.lo1, "pip", True)
    ids = lambda who: {i["id"] for i in client.get(O + "/actions/open", headers=w.h[who]).json()["items"]}
    assert ids("bm1") == {c.id, pip.id} and ids("hr1") == {c.id, pip.id}
    assert ids("gm") == {c.id}  # not the direct manager: no restricted item
    assert ids("lo1") == {c.id, pip.id}
    assert ids("bm2") == set()
    assert client.get(O + "/actions/open", headers=w.h["bm1"]).json()["items"][0]["overdue"] is True
    # who may change what
    assert client.patch(f"{O}/actions/{pip.id}", json={"note": "x"}, headers=w.h["gm"]).status_code == 404
    assert client.patch(f"{O}/actions/{pip.id}", json={"note": "x"}, headers=w.h["lo1"]).status_code == 403
    assert client.patch(f"{O}/actions/{pip.id}", json={"note": "x"}, headers=w.h["admin1"]).status_code == 200  # an administrator can, like HR
    assert client.patch(f"{O}/actions/{c.id}", json={"outcome": "no"}, headers=w.h["bm1"]).status_code == 422
    r = client.patch(f"{O}/actions/{c.id}", json={"review_on": "2026-12-01", "note": "Booked"}, headers=w.h["bm1"])
    assert r.status_code == 200 and r.json()["review_on"] == "2026-12-01"
    assert client.patch(f"{O}/actions/{c.id}", json={"outcome": "Back on target"}, headers=w.h["bm1"]).status_code == 200
    assert client.patch(f"{O}/actions/{c.id}", json={"note": "late"}, headers=w.h["bm1"]).status_code == 409
    assert c.id not in ids("bm1")
    assert client.patch(f"{O}/actions/{c.id}", json={"bogus": 1}, headers=w.h["bm1"]).status_code == 422
    assert db.query(PerformanceAuditLog).filter_by(action="action_closed").count() == 1


def test_digest_secret_and_idempotence(client, db, w, monkeypatch):
    url = "/api/internal/performance/daily-digest"
    monkeypatch.setattr(ops_router, "today_nairobi", lambda: TODAY)

    class S:
        performance_cron_token = None

    monkeypatch.setattr(ops_router, "get_settings", lambda: S)
    assert client.post(url, headers={"X-Cron-Secret": "x"}).status_code == 404  # not configured
    S.performance_cron_token = "s3cret-value"
    assert client.post(url).status_code == 401
    assert client.post(url, headers={"X-Cron-Secret": "wrong"}).status_code == 401
    act(db, w.p.lo1, "pip", True)  # a PIP check-in is due
    r = client.post(url, headers={"X-Cron-Secret": "s3cret-value"})
    assert r.status_code == 200 and r.json()["sent"] >= 1
    again = client.post(url, headers={"X-Cron-Secret": "s3cret-value"}).json()
    assert again["sent"] == 0 and again["skipped"] >= 1
    texts = " ".join(n.message for n in db.query(InternalNotification))
    assert "pip" not in texts.lower() and "warning" not in texts.lower() and "Officer" not in texts
    bm1 = db.query(AdminUser).filter_by(username="bm1").one()
    assert any("check-in" in n.message for n in db.query(InternalNotification).filter_by(recipient_admin_id=bm1.id))
    mine = client.get(O + "/reminders/me", headers=w.h["bm1"]).json()["items"]
    assert any("check-in" in i["message"] for i in mine)


def test_anomalies_and_data_quality(client, db, w):
    for m in range(1, 6):
        db.add(KpiEntry(profile_id=w.p.lo1.id, kpi_key="sales", month=date(2026, m, 1), value=1000000 if m == 3 else 100000, status="verified", source="manual", entered_by_username="x"))
    db.commit()
    r = client.get(O + "/anomalies", headers=w.h["hr1"]).json()["items"]
    assert [(i["name"], i["month"]) for i in r] == [("Officer One", "2026-03")]
    assert client.get(O + "/anomalies", headers=w.h["bm1"]).status_code == 403
    w.p.lo2.manager_id = None
    w.p.lo3.unit_id = None
    db.commit()
    kinds = {i["kind"]: i for i in client.get(O + "/data-quality", headers=w.h["hr1"]).json()["issues"]}
    assert {"no_manager", "no_unit", "no_targets"} <= set(kinds)
    assert any(x["name"] == "Officer Two" for x in kinds["no_manager"]["items"])
    assert client.get(O + "/data-quality", headers=w.h["lo1"]).status_code == 403


def test_calibration_and_leaderboard(client, db, w):
    for p, v in ((w.p.lo1, 150), (w.p.lo2, 140), (w.p.lo3, 50)):
        set_target(db, p, "sales", date(2026, 1, 1), 100, "seed", "hr1")
        for m in range(1, 6):
            db.add(KpiEntry(profile_id=p.id, kpi_key="sales", month=date(2026, m, 1), value=v, status="verified", source="manual", entered_by_username="x"))
    db.commit()
    assert client.get(O + "/calibration", headers=w.h["bm1"]).status_code == 404
    assert client.get(O + "/calibration", headers=w.h["lo1"]).status_code == 404
    c = client.get(O + "/calibration", headers=w.h["hr1"]).json()
    assert c["company_average"] is not None and "verdict" in c["note"]
    lb = client.get(O + "/leaderboard/units", headers=w.h["hr1"]).json()["items"]
    assert [u["name"] for u in lb][0] == "Alpha" and all(u["small_unit"] for u in lb)
    mine = client.get(O + "/leaderboard/units", headers=w.h["bm1"]).json()["items"]
    assert [u["name"] for u in mine] == ["Alpha"]
    assert client.get(O + "/leaderboard/units", headers=w.h["lo1"]).json()["items"] == []


def test_timeline_restricted_events(client, db, w):
    act(db, w.p.lo1, "pip", True)
    act(db, w.p.lo1, "coaching")
    kinds = lambda who: [e["text"] for e in client.get(f"{O}/timeline/{w.p.lo1.id}", headers=w.h[who]).json()["items"]]
    assert "Pip" in kinds("bm1") and "Pip" in kinds("lo1") and "Pip" in kinds("hr1")
    assert "Pip" not in kinds("gm") and "Coaching" in kinds("gm")
    assert client.get(f"{O}/timeline/{w.p.lo3.id}", headers=w.h["bm1"]).status_code == 404


def test_training_themes_hide_small_units(client, db, w):
    for p in (w.p.lo1, w.p.lo2, w.p.lo3):
        a = Appraisal(profile_id=p.id, period_key="2026-Q1", period_start=date(2026, 1, 1), period_end=date(2026, 3, 31), role_id=w.role.id, role_version_id="v", status="finalised", finalised_at=datetime.now(timezone.utc))
        db.add(a)
        db.flush()
        db.add(DevelopmentNeed(appraisal_id=a.id, slot=1, text="Negotiation and collections"))
    db.commit()
    r = client.get(O + "/training-themes", headers=w.h["hr1"]).json()
    assert r["units"] == [] and r["overall"][0]["count"] == 3  # units have fewer than 4 appraised people
    assert client.get(O + "/training-themes", headers=w.h["lo1"]).status_code == 404


def test_all_routes_need_login(client):
    for m, p in (("get", "/reminders/me"), ("get", "/actions/open"), ("patch", "/actions/x"), ("get", "/anomalies"), ("get", "/data-quality"),
                 ("get", "/calibration"), ("get", "/leaderboard/units"), ("get", "/timeline/x"), ("get", "/training-themes")):
        assert getattr(client, m)(O + p).status_code in (401, 403), p
