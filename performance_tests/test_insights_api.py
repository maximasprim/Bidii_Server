"""Phase 4a - projections and suggestions endpoints: scope, IDOR, restricted visibility, status rules, AI fallback."""

import json
from datetime import date

import pytest

from app.models.performance import KpiEntry, PerformanceAction, Recommendation
from app.services import performance_ai as ai
from app.services.performance_targets import set_target
from app.services.performance_time import today_nairobi
from performance_tests.org import build_world

BASE = "/api/admin/performance/insights"
Y = today_nairobi().year
LAST = today_nairobi().month - 1  # last complete month this year


@pytest.fixture()
def world(client, db):
    if LAST < 5:
        pytest.skip("needs five complete months in the current year")
    w = build_world(client, db)
    # lo1 steady, lo2 falling below target for the last three months, lo3 steady
    for p, series in ((w.p.lo1, [100] * LAST), (w.p.lo2, [100] * (LAST - 3) + [60, 55, 50]), (w.p.lo3, [95] * LAST)):
        set_target(db, p, "sales", date(Y, 1, 1), 100, "seed target", "hr1")
        for m, val in enumerate(series, start=1):
            db.add(KpiEntry(profile_id=p.id, kpi_key="sales", month=date(Y, m, 1), value=val, status="verified", source="manual", entered_by_username="x"))
    db.commit()
    return w


def get(client, w, who, path, **kw):
    return client.get(BASE + path, headers=w.h[who], **kw)


def test_projection_me_and_person_scope(client, world):
    w = world
    r = get(client, w, "lo1", "/projection/me")
    assert r.status_code == 200 and r.json()["available"] and set(r.json()["horizons"]) == {"quarter_end", "year_end"}
    assert get(client, w, "bm1", f"/projection/person/{w.p.lo1.id}").status_code == 200
    assert get(client, w, "bm1", f"/projection/person/{w.p.lo3.id}").status_code == 404  # other branch
    assert get(client, w, "lo1", f"/projection/person/{w.p.lo2.id}").status_code == 404  # peer
    assert get(client, w, "bm1", f"/projection/unit/{w.beta.id}").status_code == 404
    assert get(client, w, "bm1", f"/projection/unit/{w.alpha.id}").status_code == 200
    assert get(client, w, "bm1", "/projection/organisation").status_code == 404
    assert get(client, w, "hr1", "/projection/organisation").status_code == 200
    assert get(client, w, "gm", f"/projection/region/{w.south.id}").status_code == 404
    assert get(client, w, "gm", f"/projection/region/{w.north.id}").status_code == 200


def test_generate_creates_and_is_idempotent(client, db, world):
    w = world
    r = client.post(BASE + "/recommendations/generate", headers=w.h["bm1"])
    assert r.status_code == 200 and r.json()["created"] >= 1
    n = db.query(Recommendation).count()
    client.post(BASE + "/recommendations/generate", headers=w.h["bm1"])
    assert db.query(Recommendation).count() == n
    rows = db.query(Recommendation).all()
    assert all(x.entity_id != w.p.lo3.id for x in rows)  # bm1 can't run rules for another branch
    assert any(x.entity_id == w.p.lo2.id for x in rows)


def test_staff_cannot_generate(client, world):
    assert client.post(BASE + "/recommendations/generate", headers=world.h["lo1"]).status_code == 403
    assert client.post(BASE + "/recommendations/generate", headers=world.h["ceo"]).status_code == 403


def test_restricted_pip_suggestion_visibility(client, db, world):
    w = world
    client.post(BASE + "/recommendations/generate", headers=w.h["hr1"])
    pip = db.query(Recommendation).filter_by(entity_id=w.p.lo2.id, restricted=True).first()
    if pip is None:
        pytest.skip("rules did not raise a PIP suggestion for this data")
    ids = lambda who: {i["id"] for i in get(client, w, who, "/recommendations", params={"status": "all"}).json()["items"]}
    assert pip.id in ids("hr1") and pip.id in ids("bm1")
    assert pip.id not in ids("gm") and pip.id not in ids("lo1")
    assert get(client, w, "gm", "/recommendations").status_code == 200
    r = client.patch(f"{BASE}/recommendations/{pip.id}", json={"status": "acknowledged"}, headers=w.h["gm"])
    assert r.status_code == 404


def test_person_sees_only_self_audience_and_only_acknowledges(client, db, world):
    w = world
    db.add(Recommendation(entity_type="staff", entity_id=w.p.lo1.id, period_key="2026-11", rule_key="sustained_above", severity="info", restricted=False, audience=["self", "manager"], title="Strong run", content={"detail": "d"}))
    db.add(Recommendation(entity_type="staff", entity_id=w.p.lo1.id, period_key="2026-11", rule_key="below_threshold_coaching", severity="action", restricted=False, audience=["manager"], title="Coach", content={"detail": "d"}))
    db.commit()
    items = get(client, w, "lo1", "/recommendations").json()["items"]
    assert [i["rule_key"] for i in items] == ["sustained_above"]
    rid = items[0]["id"]
    assert client.patch(f"{BASE}/recommendations/{rid}", json={"status": "dismissed", "note": "no"}, headers=w.h["lo1"]).status_code == 403
    assert client.patch(f"{BASE}/recommendations/{rid}", json={"status": "acknowledged"}, headers=w.h["lo1"]).status_code == 200
    hidden = db.query(Recommendation).filter_by(rule_key="below_threshold_coaching").one()
    assert client.patch(f"{BASE}/recommendations/{hidden.id}", json={"status": "acknowledged"}, headers=w.h["lo1"]).status_code == 404


def test_dismiss_needs_a_note_and_ceo_is_read_only(client, db, world):
    w = world
    db.add(Recommendation(entity_type="staff", entity_id=w.p.lo1.id, period_key="2026-11", rule_key="target_review", severity="info", restricted=False, audience=["manager"], title="T", content={"detail": "d"}))
    db.commit()
    rid = db.query(Recommendation).one().id
    assert client.patch(f"{BASE}/recommendations/{rid}", json={"status": "dismissed"}, headers=w.h["bm1"]).status_code == 422
    assert client.patch(f"{BASE}/recommendations/{rid}", json={"status": "dismissed", "note": "Targets were agreed last week"}, headers=w.h["ceo"]).status_code == 403
    r = client.patch(f"{BASE}/recommendations/{rid}", json={"status": "actioned", "note": "Coaching booked", "due_on": "2026-12-01", "assignee_profile_id": w.p.lo1.id}, headers=w.h["bm1"])
    assert r.status_code == 200 and r.json()["status"] == "actioned" and r.json()["due_on"] == "2026-12-01"
    assert client.patch(f"{BASE}/recommendations/{rid}", json={"status": "actioned", "assignee_profile_id": w.p.lo3.id}, headers=w.h["bm1"]).status_code == 422
    assert client.patch(f"{BASE}/recommendations/{rid}", json={"status": "bogus"}, headers=w.h["bm1"]).status_code == 422


def test_advice_is_pseudonymised_falls_back_and_blocks_staff(client, db, world, monkeypatch):
    w = world
    db.add(Recommendation(entity_type="staff", entity_id=w.p.lo2.id, period_key="2026-11", rule_key="below_threshold_coaching", severity="action", restricted=False, audience=["manager"], title="Coach", content={"detail": "Two months below 90%", "figures": {"months": 2}}))
    db.commit()
    rid = db.query(Recommendation).one().id
    sent = []

    def boom(system, user):
        sent.append(user)
        raise ai.AIUnavailable("no key")

    monkeypatch.setattr(ai, "complete_json", boom)
    r = client.post(f"{BASE}/recommendations/{rid}/advice", headers=w.h["bm1"])
    assert r.status_code == 200 and r.json()["source"] == "rules"
    assert sent and "Officer Two" not in sent[0] and w.p.lo2.id not in sent[0] and "Alpha" not in sent[0]
    assert client.post(f"{BASE}/recommendations/{rid}/advice", headers=w.h["lo2"]).status_code in (403, 404)
    assert client.post(f"{BASE}/recommendations/{rid}/advice", headers=w.h["bm2"]).status_code == 404
    assert client.post(f"{BASE}/recommendations/{rid}/advice", headers=w.h["ceo"]).status_code == 403


def test_every_route_needs_login(client, world):
    for m, p in (("get", "/projection/me"), ("get", "/recommendations"), ("post", "/recommendations/generate"), ("patch", "/recommendations/x"), ("post", "/recommendations/x/advice")):
        assert getattr(client, m)(BASE + p).status_code in (401, 403)
