"""Targets, levels and change requests over HTTP: who may read/set/request/decide, IDOR, four-eyes, audit hygiene."""

import json
from datetime import date

import pytest

from app.models.performance import PerformanceAuditLog, PerformanceLevel, PerformanceRole, PerformanceTarget, StaffProfile
from performance_tests.conftest import add_user, token_headers
from performance_tests.org import build_world

BASE = "/api/admin/performance"


@pytest.fixture()
def w(client, db):
    return build_world(client, db)


def put(client, who, pid, w, key="sales", frm="2026-01-01", value=600000, reason="Level DSE1"):
    return client.put(f"{BASE}/staff/{pid}/targets", json={"kpi_key": key, "effective_from": frm, "value": value, "reason": reason}, headers=w.h[who])


# ---- reading one person's targets -----------------------------------------------------------------------------


def test_targets_listing_shows_only_kpis_that_can_carry_a_target(client, w):
    body = client.get(f"{BASE}/staff/{w.p.lo1.id}/targets", params={"month": "2026-03-10"}, headers=w.h["lo1"]).json()
    assert body["month"] == "2026-03-01"
    assert [i["kpi_key"] for i in body["items"]] == ["sales", "new_customers", "par30", "retention"]  # the rated 'self' KPI is absent
    assert all(i["value"] is None for i in body["items"]) and body["history"] == []


def test_reading_follows_scope_with_the_same_404_for_missing_and_forbidden(client, w):
    ok = client.get(f"{BASE}/staff/{w.p.lo1.id}/targets", headers=w.h["bm1"])
    assert ok.status_code == 200
    for who, other in (("lo1", w.p.lo3), ("lo1", w.p.lo2), ("bm1", w.p.lo3), ("bm2", w.p.lo1)):
        hit = client.get(f"{BASE}/staff/{other.id}/targets", headers=w.h[who])
        miss = client.get(f"{BASE}/staff/nope/targets", headers=w.h[who])
        assert hit.status_code == miss.status_code == 404 and hit.json() == miss.json(), (who, other.full_name)


# ---- setting ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["lo1", "bm1", "gm", "ceo"])
def test_only_admin_or_hr_set_targets_directly(client, w, who):
    assert put(client, who, w.p.lo1.id, w).status_code == 403


@pytest.mark.parametrize("who", ["hr1", "admin1"])
def test_hr_and_admin_set_and_read_back_with_history(client, db, w, who):
    r = put(client, who, w.p.lo1.id, w)
    assert r.status_code == 200 and r.json()["changed"] is True
    put(client, who, w.p.lo1.id, w, frm="2026-06-01", value=700000, reason="Contract change")
    body = client.get(f"{BASE}/staff/{w.p.lo1.id}/targets", params={"month": "2026-05-01"}, headers=w.h["lo1"]).json()
    assert next(i for i in body["items"] if i["kpi_key"] == "sales")["value"] == 600000.0  # May: before the change
    assert [(h["value"], h["reason"]) for h in body["history"]] == [(700000.0, "Contract change"), (600000.0, "Level DSE1")]
    later = client.get(f"{BASE}/staff/{w.p.lo1.id}/targets", params={"month": "2026-06-01"}, headers=w.h["lo1"]).json()
    assert next(i for i in later["items"] if i["kpi_key"] == "sales")["value"] == 700000.0


def test_setting_the_same_value_reports_no_change(client, w):
    put(client, "hr1", w.p.lo1.id, w)
    again = put(client, "hr1", w.p.lo1.id, w).json()
    assert again["changed"] is False and "nothing changed" in again["message"]


@pytest.mark.parametrize("patch", [{"key": "self_character"}, {"key": "bogus"}, {"value": -5}, {"reason": ""}, {"reason": "ab"}, {"value": "lots"}])
def test_bad_target_writes_are_400_or_422_and_write_nothing(client, db, w, patch):
    r = put(client, "hr1", w.p.lo1.id, w, **patch)
    assert r.status_code in (400, 422)
    assert db.query(PerformanceTarget).count() == 0


def test_the_audit_log_records_what_changed_but_never_the_reason_text(client, db, w):
    put(client, "hr1", w.p.lo1.id, w, reason="CONFIDENTIAL-REASON-TEXT")
    entries = db.query(PerformanceAuditLog).filter_by(action="target_set").all()
    assert entries and entries[-1].details == {"kpi_key": "sales", "effective_from": "2026-01-01"}
    assert "CONFIDENTIAL" not in json.dumps([e.details for e in db.query(PerformanceAuditLog)])


# ---- grid ---------------------------------------------------------------------------------------------------------


def test_grid_is_scope_filtered(client, w):
    put(client, "hr1", w.p.lo1.id, w)
    names = lambda who: {r["full_name"] for r in client.get(f"{BASE}/targets/grid", params={"kpi_key": "sales", "month": "2026-02-01"}, headers=w.h[who]).json()["items"]}  # noqa: E731
    assert names("lo1") == {"Officer One"}
    assert names("bm1") == {"Branch Manager One", "Officer One", "Officer Two"}
    assert len(names("hr1")) == 7
    rows = client.get(f"{BASE}/targets/grid", params={"kpi_key": "sales", "month": "2026-02-01", "level_id": w.levels["dse1"]}, headers=w.h["hr1"]).json()["items"]
    assert [(r["full_name"], r["value"], r["level_label"]) for r in rows] == [("Officer One", 600000.0, "DSE1")]


def test_grid_flags_people_whose_role_has_no_such_kpi(client, db, w):
    other = db.query(PerformanceRole).filter_by(key="hr_officer").one()
    db.add(StaffProfile(full_name="HR Person", performance_role_id=other.id))
    db.commit()
    rows = client.get(f"{BASE}/targets/grid", params={"kpi_key": "sales"}, headers=w.h["hr1"]).json()["items"]
    assert next(r for r in rows if r["full_name"] == "HR Person")["has_kpi"] is False


def test_grid_query_count_does_not_grow_with_people(client, db, session_factory, w):
    from sqlalchemy import event

    engine, counter = session_factory.kw["bind"], {"n": 0}
    bump = lambda *a, **k: counter.__setitem__("n", counter["n"] + 1)  # noqa: E731

    def run():
        counter["n"] = 0
        assert client.get(f"{BASE}/targets/grid", params={"kpi_key": "sales"}, headers=w.h["hr1"]).status_code == 200
        return counter["n"]

    event.listen(engine, "before_cursor_execute", bump)
    try:
        few = run()
        for i in range(60):
            db.add(StaffProfile(full_name=f"Extra {i}", performance_role_id=w.role.id, level_id=w.levels["dse1"]))
        db.commit()
        many = run()
    finally:
        event.remove(engine, "before_cursor_execute", bump)
    assert many == few  # no N+1


# ---- bulk ------------------------------------------------------------------------------------------------------------


def bulk(client, who, w, **body):
    base = {"mode": "level_defaults", "effective_from": "2026-01-01", "reason": "Annual level defaults", "dry_run": True}
    return client.post(f"{BASE}/targets/bulk", json={**base, **body}, headers=w.h[who])


@pytest.mark.parametrize("who", ["lo1", "bm1", "gm", "ceo"])
def test_bulk_is_admin_or_hr_only(client, w, who):
    assert bulk(client, who, w).status_code == 403


def test_bulk_is_a_dry_run_by_default_then_applies_in_one_go(client, db, w):
    plan = bulk(client, "hr1", w, role_id=w.role.id).json()
    assert plan["dry_run"] is True and plan["applied"] == 0 and db.query(PerformanceTarget).count() == 0
    assert plan["summary"]["create"] == 6  # lo1/lo2/lo3 x (sales, par30)
    assert plan["summary"]["skip"] == 4  # gm, ceo, bm1, bm2 have no level
    done = bulk(client, "hr1", w, role_id=w.role.id, dry_run=False).json()
    assert done["applied"] == 6 and db.query(PerformanceTarget).count() == 6
    assert {t.source for t in db.query(PerformanceTarget)} == {"level_default"}
    assert bulk(client, "hr1", w, role_id=w.role.id, dry_run=False).json()["applied"] == 0  # idempotent
    sales = {r.profile_id: r.value for r in db.query(PerformanceTarget).filter_by(kpi_key="sales")}
    assert sales == {w.p.lo1.id: 600000, w.p.lo2.id: 700000, w.p.lo3.id: 800000}  # the deck's DSE1/DSE2/SNR DSE


def test_bulk_requires_a_reason_even_for_a_dry_run(client, w):
    assert bulk(client, "hr1", w, reason="").status_code in (400, 422)


def test_bulk_set_value_and_copy_forward(client, db, w):
    r = bulk(client, "admin1", w, mode="set_value", kpi_key="sales", value=900000, unit_id=w.alpha.id, dry_run=False).json()
    assert r["applied"] == 3  # bm1, lo1 and lo2 are the people in Alpha (all on the Loan Officer role)
    cf = bulk(client, "admin1", w, mode="copy_forward", effective_from="2027-01-01", source_month="2026-12-01", uplift_pct=10, kpi_key="sales", unit_id=w.alpha.id, dry_run=False).json()
    assert cf["applied"] == 3 and {t.value for t in db.query(PerformanceTarget).filter_by(effective_from=date(2027, 1, 1))} == {990000}


def test_bulk_validation_errors(client, w):
    assert bulk(client, "hr1", w, mode="set_value").status_code == 400  # no KPI/value
    assert bulk(client, "hr1", w, mode="copy_forward").status_code == 400  # no source month
    assert bulk(client, "hr1", w, mode="copy_forward", source_month="2026-01-01", uplift_pct=-100).status_code == 400
    assert bulk(client, "hr1", w, mode="nonsense").status_code == 422


# ---- requests ----------------------------------------------------------------------------------------------------------


def request(client, who, pid, w, value=750000, reason="Promoted to DSE2"):
    return client.post(f"{BASE}/staff/{pid}/target-requests", json={"kpi_key": "sales", "effective_from": "2026-04-01", "value": value, "reason": reason}, headers=w.h[who])


def test_who_may_request_a_change(client, w):
    assert request(client, "bm1", w.p.lo1.id, w).status_code == 201  # a manager, for their report
    assert request(client, "lo1", w.p.lo1.id, w).status_code == 403  # never for yourself
    assert request(client, "lo1", w.p.lo2.id, w).status_code == 404  # a peer is out of scope
    assert request(client, "bm1", w.p.lo3.id, w).status_code == 404  # another branch
    assert request(client, "ceo", w.p.lo1.id, w).status_code == 403  # read-only executive
    assert request(client, "bm1", w.p.lo1.id, w).status_code == 400  # duplicate pending
    assert request(client, "bm2", w.p.lo3.id, w, reason="").status_code in (400, 422)


def test_request_changes_nothing_until_approved(client, db, w):
    req = request(client, "bm1", w.p.lo1.id, w).json()["data"]
    assert req["status"] == "pending" and db.query(PerformanceTarget).count() == 0
    decided = client.post(f"{BASE}/target-requests/{req['id']}/decision", json={"approve": True, "note": "ok"}, headers=w.h["hr1"])
    assert decided.status_code == 200 and decided.json()["data"]["status"] == "approved"
    t = db.query(PerformanceTarget).one()
    assert (t.kpi_key, float(t.value), t.source, t.set_by_username) == ("sales", 750000.0, "request", "hr1")
    assert client.post(f"{BASE}/target-requests/{req['id']}/decision", json={"approve": False}, headers=w.h["hr1"]).status_code == 400  # already decided


def test_rejecting_writes_no_target(client, db, w):
    rid = request(client, "bm1", w.p.lo1.id, w).json()["data"]["id"]
    assert client.post(f"{BASE}/target-requests/{rid}/decision", json={"approve": False, "note": "No"}, headers=w.h["admin1"]).json()["data"]["status"] == "rejected"
    assert db.query(PerformanceTarget).count() == 0


@pytest.mark.parametrize("who", ["lo1", "bm1", "gm", "ceo"])
def test_only_admin_or_hr_decide(client, w, who):
    rid = request(client, "bm1", w.p.lo1.id, w).json()["data"]["id"]
    assert client.post(f"{BASE}/target-requests/{rid}/decision", json={"approve": True}, headers=w.h[who]).status_code == 403


def test_four_eyes_nobody_decides_their_own_request_or_their_own_target(client, db, w):
    own = client.post(f"{BASE}/staff/{w.p.lo1.id}/target-requests", json={"kpi_key": "sales", "effective_from": "2026-04-01", "value": 1, "reason": "admin asks"}, headers=w.h["admin1"]).json()["data"]["id"]
    assert client.post(f"{BASE}/target-requests/{own}/decision", json={"approve": True}, headers=w.h["admin1"]).status_code == 403  # the requester
    assert client.post(f"{BASE}/target-requests/{own}/decision", json={"approve": True}, headers=w.h["hr1"]).status_code == 200  # someone else
    # HR as the SUBJECT of a request cannot approve a change to their own target
    from app.models.admin_user import AdminUser

    hr_user = db.query(AdminUser).filter_by(username="hr1").one()
    hr_profile = StaffProfile(full_name="HR Person", performance_role_id=w.role.id, admin_user_id=hr_user.id)
    db.add(hr_profile)
    db.commit()
    rid = client.post(f"{BASE}/staff/{hr_profile.id}/target-requests", json={"kpi_key": "sales", "effective_from": "2026-04-01", "value": 5, "reason": "raise it"}, headers=w.h["admin1"]).json()["data"]["id"]
    assert client.post(f"{BASE}/target-requests/{rid}/decision", json={"approve": True}, headers=w.h["hr1"]).status_code == 403


def test_request_lists_and_idor(client, w):
    rid = request(client, "bm1", w.p.lo1.id, w).json()["data"]["id"]
    ids = lambda who: {r["id"] for r in client.get(f"{BASE}/target-requests", headers=w.h[who]).json()["items"]}  # noqa: E731
    assert ids("bm1") == {rid} and ids("hr1") == {rid} and ids("admin1") == {rid}
    assert ids("lo1") == set() and ids("bm2") == set() and ids("gm") == set()  # only the requester (and admin/HR)
    # an id someone else can't see is the same 404 as a missing id
    for who in ("lo1", "bm2"):
        hit = client.post(f"{BASE}/target-requests/{rid}/withdraw", headers=w.h[who])
        miss = client.post(f"{BASE}/target-requests/nope/withdraw", headers=w.h[who])
        assert hit.status_code == miss.status_code == 404 and hit.json() == miss.json()
    assert client.get(f"{BASE}/target-requests", params={"status": "bogus"}, headers=w.h["hr1"]).status_code == 422


def test_withdraw_is_for_the_requester_only(client, w):
    rid = request(client, "bm1", w.p.lo1.id, w).json()["data"]["id"]
    assert client.post(f"{BASE}/target-requests/{rid}/withdraw", headers=w.h["hr1"]).status_code == 403
    assert client.post(f"{BASE}/target-requests/{rid}/withdraw", headers=w.h["bm1"]).json()["data"]["status"] == "withdrawn"
    assert client.post(f"{BASE}/target-requests/{rid}/withdraw", headers=w.h["bm1"]).status_code == 400


def test_a_request_shows_the_current_target_next_to_the_proposal(client, w):
    put(client, "hr1", w.p.lo1.id, w, frm="2026-01-01", value=600000)
    data = request(client, "bm1", w.p.lo1.id, w).json()["data"]
    assert data["current_value"] == 600000.0 and data["proposed_value"] == 750000.0 and data["profile_name"] == "Officer One"


# ---- levels ---------------------------------------------------------------------------------------------------------------


def test_seeded_levels_carry_the_numbers_from_the_hr_induction_deck(client, db, w):
    items = client.get(f"{BASE}/levels", headers=w.h["lo1"]).json()["items"]
    by_label = {i["label"]: i for i in items}
    assert by_label["DSE1"]["default_targets"] == {"sales": 600000.0, "par30": 5.0}
    assert by_label["DSE2"]["default_targets"]["sales"] == 700000.0 and by_label["SNR DSE"]["default_targets"]["sales"] == 800000.0
    assert [by_label[n]["default_targets"]["sales"] for n in ("Auto Champion Level 1", "Auto Champion Level 2", "Senior Auto Champion", "BDO Auto Champion")] == [1000000.0, 1500000.0, 2000000.0, 3000000.0]
    assert by_label["Auto Champion Level 1"]["default_targets"]["par30"] == 10.0
    assert by_label["BDO"]["default_targets"] == {"sales": 1000000.0, "par30": 5.0}
    assert [by_label[n]["default_targets"]["disbursement"] for n in ("Check-off Officer Level 1", "Check-off Officer Level 2", "Senior Check-off Agent")] == [1000000.0, 1500000.0, 2000000.0]
    assert by_label["DSE1"]["staff_count"] == 1


@pytest.mark.parametrize("who", ["lo1", "bm1", "hr1", "ceo"])
def test_level_writes_are_admin_only(client, w, who):
    assert client.post(f"{BASE}/levels", json={"role_id": w.role.id, "label": "X"}, headers=w.h[who]).status_code == 403
    assert client.patch(f"{BASE}/levels/{w.levels['dse1']}", json={"label": "X"}, headers=w.h[who]).status_code == 403


def test_level_create_update_and_validation(client, db, w):
    h = w.h["admin1"]
    ok = client.post(f"{BASE}/levels", json={"role_id": w.role.id, "label": "Trainee", "rank": 1, "default_targets": {"sales": 300000}}, headers=h)
    assert ok.status_code == 201 and ok.json()["data"]["key"] == "trainee"
    assert client.post(f"{BASE}/levels", json={"role_id": w.role.id, "label": "Trainee"}, headers=h).status_code == 409
    assert client.post(f"{BASE}/levels", json={"role_id": w.role.id, "label": "Bad", "default_targets": {"self_character": 3}}, headers=h).status_code == 400  # rated KPI
    assert client.post(f"{BASE}/levels", json={"role_id": w.role.id, "label": "Bad2", "default_targets": {"nope": 3}}, headers=h).status_code == 400
    assert client.post(f"{BASE}/levels", json={"role_id": "x", "label": "Bad3"}, headers=h).status_code == 400
    assert client.patch(f"{BASE}/levels/{w.levels['dse1']}", json={"default_targets": {"sales": -1}}, headers=h).status_code == 400
    assert client.patch(f"{BASE}/levels/nope", json={"label": "Y"}, headers=h).status_code == 404


def test_editing_a_level_leaves_existing_targets_alone(client, db, w):
    bulk(client, "hr1", w, role_id=w.role.id, dry_run=False)
    r = client.patch(f"{BASE}/levels/{w.levels['dse1']}", json={"default_targets": {"sales": 999999}}, headers=w.h["admin1"])
    assert r.status_code == 200 and r.json()["data"]["default_targets"] == {"sales": 999999.0}
    assert float(db.query(PerformanceTarget).filter_by(profile_id=w.p.lo1.id, kpi_key="sales").one().value) == 600000.0


# ---- staff <-> level ---------------------------------------------------------------------------------------------------------


def test_staff_level_must_belong_to_the_staff_members_role(client, db, w):
    ac = db.query(PerformanceLevel).filter_by(key="auto_champion").one()
    h = w.h["hr1"]
    assert client.patch(f"{BASE}/staff/{w.p.lo1.id}", json={"level_id": ac.id}, headers=h).status_code == 400
    ok = client.patch(f"{BASE}/staff/{w.p.lo1.id}", json={"level_id": w.levels["dse2"]}, headers=h)
    assert ok.status_code == 200 and ok.json()["data"]["level_label"] == "DSE2"
    assert client.patch(f"{BASE}/staff/{w.p.lo1.id}", json={"level_id": "nope"}, headers=h).status_code == 400
    # changing the role clears a level that belongs to the old one
    auto_role = db.query(PerformanceRole).filter_by(key="auto_champion").one()
    moved = client.patch(f"{BASE}/staff/{w.p.lo1.id}", json={"performance_role_id": auto_role.id}, headers=h).json()["data"]
    assert moved["level_id"] is None and moved["performance_role_name"] == "Auto Champion"
    assert client.patch(f"{BASE}/staff/{w.p.lo1.id}", json={"level_id": ac.id}, headers=h).json()["data"]["level_label"] == "Auto Champion Level 1"
    assert client.patch(f"{BASE}/staff/{w.p.lo1.id}", json={"level_id": None}, headers=h).json()["data"]["level_id"] is None


def test_staff_can_be_created_with_a_level(client, w):
    r = client.post(f"{BASE}/staff", json={"full_name": "New DSE", "performance_role_id": w.role.id, "level_id": w.levels["dse1"]}, headers=w.h["hr1"])
    assert r.status_code == 201 and r.json()["data"]["level_label"] == "DSE1"
    bad = client.post(f"{BASE}/staff", json={"full_name": "Wrong", "performance_role_id": w.role.id, "level_id": "zzz"}, headers=w.h["hr1"])
    assert bad.status_code == 400


def test_the_server_tells_each_viewer_whether_a_request_is_theirs_and_whether_they_may_decide_it(client, db, w):
    rid = request(client, "bm1", w.p.lo1.id, w).json()["data"]["id"]
    flags = lambda who: next(r for r in client.get(f"{BASE}/target-requests", headers=w.h[who]).json()["items"] if r["id"] == rid)  # noqa: E731
    assert (flags("bm1")["is_mine"], flags("bm1")["can_decide"]) == (True, False)  # the requester withdraws, never decides
    assert (flags("hr1")["is_mine"], flags("hr1")["can_decide"]) == (False, True)
    assert (flags("admin1")["is_mine"], flags("admin1")["can_decide"]) == (False, True)
    client.post(f"{BASE}/target-requests/{rid}/decision", json={"approve": True}, headers=w.h["hr1"])
    done = client.get(f"{BASE}/target-requests", params={"status": "approved"}, headers=w.h["hr1"]).json()["items"][0]
    assert done["can_decide"] is False  # nothing left to decide
