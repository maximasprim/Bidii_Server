"""Phase 6c - 360 feedback: lifecycle, anonymity, who sees what, and that it never touches scores."""

import pytest

from app.models.internal_notification import InternalNotification
from app.models.performance import Feedback360Rater, PerformanceAuditLog
from app.services import performance_360 as f
from performance_tests.org import build_world

B = "/api/admin/performance/feedback360"
ANSWERS = {q["key"]: 4 for q in f.QUESTIONS}


@pytest.fixture()
def w(client, db):
    return build_world(client, db)


def cycle(client, w, min_group=3):
    return client.post(B + "/cycles", json={"name": "Managers 2026", "min_group": min_group}, headers=w.h["hr1"]).json()["id"]


def setup_open(client, db, w, min_group=2):
    cid = cycle(client, w, min_group)
    r = client.post(f"{B}/cycles/{cid}/subjects", json={"profile_id": w.p.bm1.id}, headers=w.h["hr1"])
    assert r.status_code == 201, r.text
    assert client.post(f"{B}/cycles/{cid}/status", json={"to": "open"}, headers=w.h["hr1"]).status_code == 200
    return cid, r.json()["subject_id"]


def rid(db, w, who, subject_id):
    return db.query(Feedback360Rater).filter_by(subject_id=subject_id, rater_profile_id=getattr(w.p, who).id).one().id


def answer(client, w, who, rid_, mark=4, strengths=None, development=None):
    return client.put(f"{B}/requests/{rid_}", json={"answers": {k: mark for k in ANSWERS}, "strengths": strengths, "development": development}, headers=w.h[who])


def test_hr_only_management_and_suggestion(client, w):
    assert client.post(B + "/cycles", json={"name": "x"}, headers=w.h["bm1"]).status_code in (403, 422)
    assert client.post(B + "/cycles", json={"name": "Managers"}, headers=w.h["bm1"]).status_code == 403
    s = client.get(f"{B}/suggest/{w.p.bm1.id}", headers=w.h["hr1"]).json()["raters"]
    assert {(r["name"], r["relationship"]) for r in s} == {("Branch Manager One", "self"), ("General Manager", "manager"), ("Officer One", "direct_report"), ("Officer Two", "direct_report"), ("Branch Manager Two", "peer")}
    assert client.get(f"{B}/suggest/{w.p.bm1.id}", headers=w.h["lo1"]).status_code == 403


def test_needs_enough_anonymous_raters_and_logins(client, db, w):
    cid = cycle(client, w, 3)
    thin = [{"profile_id": w.p.bm1.id, "relationship": "self"}, {"profile_id": w.p.gm.id, "relationship": "manager"}, {"profile_id": w.p.lo1.id, "relationship": "direct_report"}]
    assert client.post(f"{B}/cycles/{cid}/subjects", json={"profile_id": w.p.bm1.id, "raters": thin}, headers=w.h["hr1"]).status_code == 422
    bad_self = [{"profile_id": w.p.lo1.id, "relationship": "self"}]
    assert client.post(f"{B}/cycles/{cid}/subjects", json={"profile_id": w.p.bm1.id, "raters": bad_self}, headers=w.h["hr1"]).status_code == 422
    w.p.lo2.admin_user_id = None  # no login
    db.commit()
    assert client.post(f"{B}/cycles/{cid}/subjects", json={"profile_id": w.p.bm1.id}, headers=w.h["hr1"]).status_code == 422


def test_lifecycle_rules_and_notifications(client, db, w):
    cid = cycle(client, w)
    assert client.post(f"{B}/cycles/{cid}/status", json={"to": "open"}, headers=w.h["hr1"]).status_code == 422  # nobody added
    client.post(f"{B}/cycles/{cid}/subjects", json={"profile_id": w.p.bm1.id}, headers=w.h["hr1"])
    assert client.post(f"{B}/cycles/{cid}/status", json={"to": "released"}, headers=w.h["hr1"]).status_code == 422  # can't skip
    client.post(f"{B}/cycles/{cid}/status", json={"to": "open"}, headers=w.h["hr1"])
    assert db.query(InternalNotification).count() >= 4
    assert all("anonymous" in n.message and "bm1" not in n.message.lower() for n in db.query(InternalNotification))
    assert client.post(f"{B}/cycles/{cid}/status", json={"to": "bogus"}, headers=w.h["hr1"]).status_code == 422


def test_raters_see_only_their_own_requests(client, db, w):
    cid, sid = setup_open(client, db, w)
    mine = client.get(B + "/my-requests", headers=w.h["lo1"]).json()["items"]
    assert [(i["subject"], i["relationship"]) for i in mine] == [("Branch Manager One", "direct_report")]
    own = client.get(B + "/my-requests", headers=w.h["bm1"]).json()["items"]
    assert [i["subject"] for i in own] == ["Yourself"]
    assert client.get(B + "/my-requests", headers=w.h["lo3"]).json()["items"] == []
    r1 = rid(db, w, "lo1", sid)
    assert client.get(f"{B}/requests/{r1}", headers=w.h["lo2"]).status_code == 404
    assert client.get(f"{B}/requests/{r1}", headers=w.h["hr1"]).status_code == 404  # even HR can't open someone else's form
    form = client.get(f"{B}/requests/{r1}", headers=w.h["lo1"]).json()
    assert len(form["questions"]) == 6 and "others" in form["anonymity"] or "group" in form["anonymity"]


def test_answer_validation_and_edit_until_closed(client, db, w):
    cid, sid = setup_open(client, db, w)
    r1 = rid(db, w, "lo1", sid)
    bad = lambda body: client.put(f"{B}/requests/{r1}", json=body, headers=w.h["lo1"]).status_code  # noqa: E731
    assert bad({"answers": {"nope": 3}}) == 422
    assert bad({"answers": {k: 6 for k in ANSWERS}}) == 422
    assert bad({"answers": {k: 3 for k in list(ANSWERS)[:2]}}) == 422  # must rate everything
    assert bad({"answers": ANSWERS, "extra": 1}) == 422
    assert answer(client, w, "lo1", r1, 4).status_code == 200
    assert answer(client, w, "lo1", r1, 2).status_code == 200  # may change their mind while open
    client.post(f"{B}/cycles/{cid}/status", json={"to": "closed"}, headers=w.h["hr1"])
    assert answer(client, w, "lo1", r1, 5).status_code == 422  # closed: no more changes
    assert client.get(f"{B}/requests/{r1}", headers=w.h["lo1"]).status_code == 404


def test_results_hidden_until_released_and_anonymised(client, db, w):
    cid, sid = setup_open(client, db, w, min_group=2)
    answer(client, w, "bm1", rid(db, w, "bm1", sid), 5, "I coach well", "I delegate poorly")
    answer(client, w, "gm", rid(db, w, "gm", sid), 4, "Solid manager", "Needs delegation")
    answer(client, w, "lo1", rid(db, w, "lo1", sid), 2, "Fair to everyone", "Too many meetings")
    answer(client, w, "lo2", rid(db, w, "lo2", sid), 3, "Supports us", "Slow to decide")
    answer(client, w, "bm2", rid(db, w, "bm2", sid), 5, "Great peer", "")
    prog = client.get(f"{B}/cycles/{cid}", headers=w.h["hr1"]).json()["progress"][0]
    assert prog["submitted"] == 5 and prog["asked"] == 5 and "Fair" not in str(prog)
    for who in ("bm1", "hr1"):
        assert client.get(f"{B}/results/{sid}", headers=w.h[who]).status_code == 404  # not released
    client.post(f"{B}/cycles/{cid}/status", json={"to": "closed"}, headers=w.h["hr1"])
    assert client.get(f"{B}/results/{sid}", headers=w.h["hr1"]).status_code == 404
    client.post(f"{B}/cycles/{cid}/status", json={"to": "released"}, headers=w.h["hr1"])
    r = client.get(f"{B}/results/{sid}", headers=w.h["bm1"]).json()
    g = r["groups"]
    assert g["direct_report"]["shown"] and g["direct_report"]["responses"] == 2 and g["direct_report"]["averages"]["delivering_results"] == 2.5
    assert g["peer"]["shown"] is False and "Great peer" not in str(r)  # a single peer is not shown separately
    assert g["self"]["averages"]["delivering_results"] == 5 and g["manager"]["strengths"] == ["Solid manager"]
    assert r["overall_others"]["delivering_results"] == round((2 + 3 + 5) / 3, 2) and r["self_vs_others"]["delivering_results"] == round(5 - 10 / 3, 2)
    assert sorted(g["direct_report"]["development"]) == ["Slow to decide", "Too many meetings"]
    assert "rater" not in str(r).lower() and "lo1" not in str(r) and "Officer" not in str(r)
    # who may read
    assert client.get(f"{B}/results/{sid}", headers=w.h["gm"]).status_code == 200  # the subject's direct manager
    assert client.get(f"{B}/results/{sid}", headers=w.h["hr1"]).status_code == 200
    for who in ("lo1", "lo2", "bm2", "ceo", "lo3"):
        assert client.get(f"{B}/results/{sid}", headers=w.h[who]).status_code == 404, who
    assert len(client.get(B + "/my-results", headers=w.h["bm1"]).json()["items"]) == 1
    assert [i["subject"] for i in client.get(B + "/team-results", headers=w.h["gm"]).json()["items"]] == ["Branch Manager One"]
    assert client.get(B + "/team-results", headers=w.h["bm2"]).json()["items"] == [] and client.get(B + "/team-results", headers=w.h["lo1"]).json()["items"] == []
    assert client.get(B + "/my-results", headers=w.h["lo1"]).json()["items"] == []
    audited = {a for (a,) in db.query(PerformanceAuditLog.action)}
    assert "f360.results_viewed" in audited and "f360.submitted" in audited
    assert "Fair to everyone" not in str(list(db.query(PerformanceAuditLog.details)))


def test_decline_and_pending_visible_to_hr_as_names_only(client, db, w):
    cid, sid = setup_open(client, db, w)
    r2 = rid(db, w, "lo2", sid)
    assert client.post(f"{B}/requests/{r2}/decline", headers=w.h["lo2"]).status_code == 200
    assert answer(client, w, "lo2", r2).status_code == 409
    prog = client.get(f"{B}/cycles/{cid}", headers=w.h["hr1"]).json()["progress"][0]
    assert prog["declined"] == 1 and "Officer One" in prog["pending_raters"]


def test_never_touches_scores_and_needs_login(client, w):
    import app.main as m

    assert not [r for r in m.app.routes if getattr(r, "path", "").startswith("/api/admin/performance/feedback360") and "score" in r.path]
    for meth, p in (("get", "/cycles"), ("post", "/cycles"), ("get", "/my-requests"), ("get", "/my-results"), ("get", "/team-results"), ("get", "/results/x"), ("put", "/requests/x"), ("get", "/suggest/x")):
        assert getattr(client, meth)(B + p).status_code in (401, 403), p
