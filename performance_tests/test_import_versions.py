"""Import version control: numbering, unpost rules, restore to any version, compare, locked months."""

from datetime import date

from app.models.performance import KpiEntry, PeriodLock
from performance_tests.org import build_world
from performance_tests.test_import import ALPHA, BASE, YEAR, counts, make_workbook, upload

V = BASE + "/versions"


def post(client, h, alpha, name="staff.xlsx"):
    imp = upload(client, h, make_workbook({"Alpha": alpha}), name).json()["id"]
    r = client.post(f"{BASE}/{imp}/commit", json={"confirm": True}, headers=h)
    assert r.status_code == 200, r.text
    return imp


def three(client, db):
    w = build_world(client, db)
    h = w.h["hr1"]
    a = post(client, h, ALPHA)
    b = post(client, h, [dict(ALPHA[0], jan=1200000)] + ALPHA[1:])
    c = post(client, h, [dict(ALPHA[0], jan=1300000)] + ALPHA[1:])
    return w, h, a, b, c


def jan(db):
    db.expire_all()
    return float(db.query(KpiEntry).filter(KpiEntry.kpi_key == "sales", KpiEntry.month == date(YEAR, 1, 1)).order_by(KpiEntry.value.desc()).first().value)


def test_versions_are_numbered_per_year_in_order(client, db):
    w, h, a, b, c = three(client, db)
    items = client.get(V, headers=h).json()["items"]
    assert [i["version"] for i in items] == [3, 2, 1] and items[0]["is_current"] and not items[1]["is_current"]
    assert all(i["status"] == "posted" for i in items) and items[0]["series"] == str(YEAR)
    assert client.put(f"{BASE}/{a}/label", json={"label": "Original tracker"}, headers=h).status_code == 200
    assert client.get(V, headers=h).json()["items"][2]["label"] == "Original tracker"
    assert [i["same_file_as_version"] for i in client.get(V, headers=h).json()["items"]] == [None, None, None]


def test_unpost_needs_a_reason_and_the_latest_version_unless_cascading(client, db):
    w, h, a, b, c = three(client, db)
    assert client.post(f"{BASE}/{c}/unpost", json={"reason": "no", "confirm": True}, headers=h).status_code == 422
    assert client.post(f"{BASE}/{c}/unpost", json={"reason": "wrong file", "confirm": False}, headers=h).status_code == 422
    r = client.post(f"{BASE}/{b}/unpost", json={"reason": "wrong file", "confirm": True}, headers=h)
    assert r.status_code == 409 and "v3" in r.json()["detail"]
    assert client.post(f"{BASE}/{b}/undo", json={"confirm": True}, headers=h).status_code == 400  # the old route obeys the same rule
    assert jan(db) == 1300000
    r = client.post(f"{BASE}/{c}/unpost", json={"reason": "wrong file", "confirm": True}, headers=h)
    assert r.status_code == 200 and jan(db) == 1200000
    items = {i["version"]: i for i in client.get(V, headers=h).json()["items"]}
    assert items[3]["status"] == "unposted" and items[3]["unposted_reason"] == "wrong file" and items[2]["is_current"]
    r = client.post(f"{BASE}/{a}/unpost", json={"reason": "start over", "confirm": True, "cascade": True}, headers=h)
    assert r.status_code == 200 and [d["version"] for d in r.json()["unposted"]] == [2, 1]
    assert counts(db)["entries"] == 0


def test_restore_an_older_version_unposts_the_newer_ones(client, db):
    w, h, a, b, c = three(client, db)
    plan = client.get(f"{BASE}/{b}/restore-plan", headers=h).json()
    assert plan["target_posted"] and [u["version"] for u in plan["unpost"]] == [3] and not plan["repost_as_new_version"]
    r = client.post(f"{BASE}/{b}/restore", json={"reason": "v3 had a typo", "confirm": True}, headers=h)
    assert r.status_code == 200 and r.json()["new_version"] is None and jan(db) == 1200000
    assert client.post(f"{BASE}/{b}/restore", json={"reason": "again please", "confirm": True}, headers=h).status_code == 409  # already current


def test_restoring_an_unposted_version_posts_it_again_as_a_new_version(client, db):
    w, h, a, b, c = three(client, db)
    assert client.post(f"{BASE}/{a}/unpost", json={"reason": "start over", "confirm": True, "cascade": True}, headers=h).status_code == 200
    plan = client.get(f"{BASE}/{b}/restore-plan", headers=h).json()
    assert plan["repost_as_new_version"] and plan["unpost"] == []
    r = client.post(f"{BASE}/{b}/restore", json={"reason": "go back to v2", "confirm": True}, headers=h)
    assert r.status_code == 200 and r.json()["new_version"] == 4 and jan(db) == 1200000
    items = client.get(V, headers=h).json()["items"]
    assert items[0]["version"] == 4 and items[0]["label"] == "Restore of v2" and items[0]["restored_from"] == 2 and items[0]["is_current"]
    assert [i["status"] for i in items[1:]] == ["unposted"] * 3


def test_restore_is_all_or_nothing(client, db, monkeypatch):
    w, h, a, b, c = three(client, db)
    client.post(f"{BASE}/{a}/unpost", json={"reason": "start over", "confirm": True, "cascade": True}, headers=h)
    d = post(client, h, [dict(ALPHA[0], jan=1400000)] + ALPHA[1:])  # v4 posted; v1-3 unposted
    from app.services import performance_import as svc

    monkeypatch.setattr(svc, "execute", lambda *a, **k: (_ for _ in ()).throw(svc.ImportProblem("boom")))
    before = jan(db)
    r = client.post(f"{BASE}/{b}/restore", json={"reason": "go back to v2", "confirm": True}, headers=h)
    assert r.status_code == 409 and jan(db) == before == 1400000  # v4 was NOT unposted by the failed restore
    assert next(i for i in client.get(V, headers=h).json()["items"] if i["version"] == 4)["status"] == "posted"


def test_a_locked_month_blocks_unposting_and_restoring(client, db):
    w, h, a, b, c = three(client, db)
    db.add(PeriodLock(month=date(YEAR, 1, 1), status="locked", locked_by_username="hr1"))
    db.commit()
    r = client.post(f"{BASE}/{c}/unpost", json={"reason": "wrong file", "confirm": True}, headers=h)
    assert r.status_code == 409 and f"{YEAR}-01" in r.json()["detail"]
    assert client.get(f"{BASE}/{b}/restore-plan", headers=h).json()["blocked_by_locked_months"] == [f"{YEAR}-01"]
    assert client.post(f"{BASE}/{b}/restore", json={"reason": "go back to v2", "confirm": True}, headers=h).status_code == 409
    assert jan(db) == 1300000


def test_compare_two_versions(client, db):
    w, h, a, b, c = three(client, db)
    r = client.get(V + "/compare", params={"a": a, "b": c}, headers=h).json()
    assert r["from_version"] == 1 and r["to_version"] == 3 and r["counts"]["changed"] == 1 and not r["same_file"]
    ch = r["changed"][0]
    assert ch["month"] == f"{YEAR}-01" and ch["from"] == 1100000 and ch["to"] == 1300000
    assert client.get(V + "/compare", params={"a": a, "b": "nope"}, headers=h).status_code == 404


def test_only_admin_or_hr_and_staff_cannot_see_versions(client, db):
    w = build_world(client, db)
    for who in ("lo1", "bm1", "ceo"):
        assert client.get(V, headers=w.h[who]).status_code in (401, 403, 404)
    assert client.get(V).status_code in (401, 403)
    assert client.get(V, headers=w.h["admin1"]).status_code == 200
