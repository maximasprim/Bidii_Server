"""Phase 6b - the manager's weekly view and the weekly message."""

from datetime import date

import pytest

import app.routers.admin_performance_ops as ops_router
from app.models.admin_user import AdminUser
from app.models.performance import KpiEntry, PerformanceAction, Recommendation
from app.services import email_sender
from app.services import performance_channels as ch
from app.services.performance_targets import set_target
from performance_tests.org import build_world

O = "/api/admin/performance/ops"
TODAY = date(2026, 11, 18)


@pytest.fixture()
def w(client, db, monkeypatch):
    monkeypatch.setattr(ops_router, "today_nairobi", lambda: TODAY)
    w = build_world(client, db)
    for p, series in ((w.p.lo1, [100] * 10), (w.p.lo2, [100] * 7 + [50, 45, 40]), (w.p.lo3, [60] * 10)):
        set_target(db, p, "sales", date(2026, 1, 1), 100, "seed", "hr1")
        for m, v in enumerate(series, start=1):
            db.add(KpiEntry(profile_id=p.id, kpi_key="sales", month=date(2026, m, 1), value=v, status="verified", source="manual", entered_by_username="x"))
    db.commit()
    return w


def get(client, w, who):
    return client.get(O + "/weekly", headers=w.h[who])


def test_manager_sees_only_their_team_ranked(client, db, w):
    r = get(client, w, "bm1").json()
    names = [p["name"] for p in r["people"]]
    assert names[0] == "Officer Two" and "Officer Three" not in names and r["team_size"] == 2  # lo3 is in the other branch
    assert any("Below 70% for 3 months running" == x["text"] for x in r["people"][0]["reasons"])
    one = next(p for p in r["people"] if p["name"] == "Officer One")  # fine on sales: only housekeeping items
    assert {x["kind"] for x in one["reasons"]} <= {"performance", "appraisal", "data"} and not any("Below" in x["text"] for x in one["reasons"])
    assert one["priority"] < r["people"][0]["priority"]
    assert r["week_of"] == "2026-11-16"
    r2 = get(client, w, "bm2").json()
    assert [p["name"] for p in r2["people"]][0] == "Officer Three" and r2["team_size"] == 1


def test_region_manager_sees_both_branches_most_urgent_first(client, db, w):
    r = get(client, w, "gm").json()
    names = [p["name"] for p in r["people"]]
    assert set(names) >= {"Officer Two", "Officer Three"}
    pr = [p["priority"] for p in r["people"]]
    assert pr == sorted(pr, reverse=True)


def test_staff_and_executive_have_no_weekly_view(client, db, w):
    assert get(client, w, "lo1").status_code == 200 and get(client, w, "lo1").json()["team_size"] == 0
    assert get(client, w, "hr1").status_code == 200  # HR has no profile: everyone they can see


def test_restricted_items_follow_entitlement(client, db, w):
    db.add(PerformanceAction(profile_id=w.p.lo1.id, action_type="pip", restricted=True, started_on=date(2026, 10, 1), review_on=date(2026, 11, 10), raised_by_username="bm1"))
    db.add(PerformanceAction(profile_id=w.p.lo1.id, action_type="coaching", restricted=False, started_on=date(2026, 10, 1), review_on=date(2026, 11, 20), raised_by_username="bm1"))
    db.add(Recommendation(entity_type="staff", entity_id=w.p.lo1.id, period_key="2026-11", rule_key="pip_suggested", severity="urgent", restricted=True, audience=["manager"], title="Consider a PIP", content={"detail": "d"}))
    db.commit()
    direct = {x["text"] for p in get(client, w, "bm1").json()["people"] if p["name"] == "Officer One" for x in p["reasons"]}
    assert any("overdue since 10 Nov" in t for t in direct) and "Consider a PIP" in direct and any("due 20 Nov" in t for t in direct)
    gm = {x["text"] for p in get(client, w, "gm").json()["people"] if p["name"] == "Officer One" for x in p["reasons"]}
    assert "Consider a PIP" not in gm and not any("overdue since 10 Nov" in t for t in gm) and any("due 20 Nov" in t for t in gm)


def test_recognise_and_verify_lists(client, db, w):
    db.add(Recommendation(entity_type="staff", entity_id=w.p.lo1.id, period_key="2026-11", rule_key="sustained_above", severity="info", restricted=False, audience=["self", "manager"], title="Strong", content={"detail": "d"}))
    db.add(KpiEntry(profile_id=w.p.lo2.id, kpi_key="sales", month=date(2026, 11, 1), value=10, status="submitted", source="manual", entered_by_username="lo2"))
    db.commit()
    r = get(client, w, "bm1").json()
    assert [x["name"] for x in r["recognise"]] == ["Officer One"]
    assert r["to_verify"] == [{"profile_id": w.p.lo2.id, "name": "Officer Two", "figures": 1}]
    assert get(client, w, "bm2").json()["to_verify"] == []


def test_weekly_message_is_counts_only_and_once(client, db, w, monkeypatch):
    sent = []

    class S:
        performance_cron_token = "tok"

    monkeypatch.setattr(ops_router, "get_settings", lambda: S)
    monkeypatch.setattr(ch, "email_configured", lambda: True)
    monkeypatch.setattr(email_sender, "send_email", lambda **kw: sent.append(kw))
    bm1 = db.query(AdminUser).filter_by(username="bm1").one()
    bm1.email = "bm1@example.com"
    db.commit()
    url = "/api/internal/performance/weekly-digest"
    assert client.post(url).status_code == 401
    r = client.post(url, headers={"X-Cron-Secret": "tok"}).json()
    assert r["managers"] >= 1 and r["channels"].get("email_sent") == 1
    msg = sent[0]
    assert msg["to_email"] == "bm1@example.com" and "2 team member" in msg["body_text"] and "1 urgent" in msg["body_text"]
    assert "Officer" not in msg["body_text"] + msg["subject"]
    client.post(url, headers={"X-Cron-Secret": "tok"})
    assert len(sent) == 1


def test_needs_login(client):
    assert client.get(O + "/weekly").status_code in (401, 403)
    assert client.post("/api/internal/performance/weekly-digest").status_code in (401, 404)
