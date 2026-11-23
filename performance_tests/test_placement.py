"""Where everyone sits: layout view, proposals from facts already in the system, accepting them, and safety."""

from app.models.admin_user import AdminUser
from app.models.branch import Branch
from app.models.performance import PerformanceRole, PerformanceUnit, Region, StaffAssignmentHistory, StaffProfile, UnitRegionLink
from performance_tests.org import build_world

B = "/api/admin/performance/org-setup"


def role(db, key):
    return db.query(PerformanceRole).filter_by(key=key).one()


def test_layout_lists_regions_units_and_gaps(client, db):
    w = build_world(client, db)
    db.add(Branch(name="Embu", address="x", hours="x", phone="0", lat=0.0, lng=0.0))
    db.commit()
    r = client.get(B + "/layout", headers=w.h["admin1"]).json()
    assert {x["name"] for x in r["regions"]} == {"North", "South"}
    north = next(x for x in r["regions"] if x["name"] == "North")
    assert {u["name"] for u in north["units"]} == {"Alpha"} and north["units"][0]["staff"] == 3
    assert [b["name"] for b in r["branches_without_unit"]] == ["Embu"]
    for who in ("lo1", "bm1", "gm", "ceo"):
        assert client.get(B + "/layout", headers=w.h[who]).status_code in (401, 403, 404), who
    assert client.get(B + "/layout").status_code in (401, 403)


def test_proposals_use_login_branch_role_and_unit_manager(client, db):
    w = build_world(client, db)
    branch = Branch(name="Embu", address="x", hours="x", phone="0", lat=0.0, lng=0.0)
    db.add(branch)
    db.flush()
    embu = PerformanceUnit(name="Embu", kind="branch", branch_id=branch.id)
    ho = PerformanceUnit(name="Head Office", kind="head_office")
    db.add_all([embu, ho])
    db.flush()
    u = AdminUser(username="new.officer", password_hash="x", role="staff", branch_id=branch.id)
    db.add(u)
    db.flush()
    lo_role = role(db, "loan_officer")
    new = StaffProfile(full_name="New Officer", performance_role_id=lo_role.id, admin_user_id=u.id)
    hr_person = StaffProfile(full_name="Hira Rao", performance_role_id=role(db, "hr_officer").id)
    nobody = StaffProfile(full_name="Wandering One", performance_role_id=lo_role.id)
    db.add_all([new, hr_person, nobody])
    db.flush()
    embu.manager_profile_id = w.p.bm2.id
    db.commit()
    r = client.get(B + "/placement", headers=w.h["hr1"]).json()
    rows = {x["name"]: x for x in r["proposals"]}
    assert rows["New Officer"]["unit_name"] == "Embu" and rows["New Officer"]["manager_id"] == w.p.bm2.id
    assert rows["Hira Rao"]["unit_name"] == "Head Office"
    assert "Wandering One" not in rows and "Wandering One" in [x["name"] for x in r["unplaced"]]
    assert "Officer One" not in rows  # already placed with a manager: never touched


def test_accepting_writes_history_and_is_all_or_nothing(client, db):
    w = build_world(client, db)
    lo_role = role(db, "loan_officer")
    p = StaffProfile(full_name="Loose Person", performance_role_id=lo_role.id)
    db.add(p)
    db.commit()
    unit = db.query(PerformanceUnit).filter_by(name="Beta").one()
    h = w.h["admin1"]
    bad = client.post(B + "/placement/apply", json={"items": [{"profile_id": p.id, "unit_id": unit.id}, {"profile_id": p.id, "manager_id": p.id}], "confirm": True}, headers=h)
    assert bad.status_code == 400
    db.expire_all()
    assert db.get(StaffProfile, p.id).unit_id is None  # the first item was not kept
    assert client.post(B + "/placement/apply", json={"items": [{"profile_id": p.id, "unit_id": unit.id, "manager_id": w.p.bm2.id}], "confirm": False}, headers=h).status_code == 422
    ok = client.post(B + "/placement/apply", json={"items": [{"profile_id": p.id, "unit_id": unit.id, "manager_id": w.p.bm2.id}], "confirm": True}, headers=h)
    assert ok.status_code == 200 and ok.json() == {"units_set": 1, "managers_set": 1}
    db.expire_all()
    q = db.get(StaffProfile, p.id)
    assert q.unit_id == unit.id and q.manager_id == w.p.bm2.id
    hist = db.query(StaffAssignmentHistory).filter_by(profile_id=p.id).all()
    assert len(hist) == 1 and hist[0].unit_id == unit.id and hist[0].reason == "Placed in the organisation structure"
    assert client.post(B + "/placement/apply", json={"items": [{"profile_id": p.id, "unit_id": "nope"}], "confirm": True}, headers=h).status_code == 400
    assert client.post(B + "/placement/apply", json={"items": [{"profile_id": p.id, "x": 1}], "confirm": True}, headers=h).status_code == 422
    for who in ("lo1", "bm1", "gm"):
        assert client.post(B + "/placement/apply", json={"items": [{"profile_id": p.id, "unit_id": unit.id}], "confirm": True}, headers=w.h[who]).status_code in (401, 403, 404), who


def test_a_loop_is_refused(client, db):
    w = build_world(client, db)
    # lo1 reports to bm1; making bm1 report to lo1 would loop
    r = client.post(B + "/placement/apply", json={"items": [{"profile_id": w.p.bm1.id, "manager_id": w.p.lo1.id}], "confirm": True}, headers=w.h["admin1"])
    assert r.status_code == 400 and "loop" in r.json()["detail"]


def test_a_collector_in_a_branch_with_no_debt_unit_yet_goes_to_the_branch_never_to_one_global_debt_collection_team(client, db):
    """There is no single Debt Collection team: each branch has its own unit (see test_debt_collection). Where a branch has none yet, a collector
    goes to the branch itself, and even an old global 'Debt Collection' unit is never used."""
    w = build_world(client, db)
    branch = Branch(name="Embu", address="x", hours="x", phone="0", lat=0.0, lng=0.0)
    db.add(branch)
    db.flush()
    embu = PerformanceUnit(name="Embu", kind="branch", branch_id=branch.id)
    legacy = PerformanceUnit(name="Debt Collection", kind="team")
    db.add_all([embu, legacy])
    db.flush()
    user = AdminUser(username="debt.embu", password_hash="x", role="staff", branch_id=branch.id)
    db.add(user)
    db.flush()
    dr = role(db, "debt_recovery")
    with_login = StaffProfile(full_name="Debt Collection Embu", performance_role_id=dr.id, admin_user_id=user.id)
    no_branch = StaffProfile(full_name="Debt Collection Nowhere", performance_role_id=dr.id)
    db.add_all([with_login, no_branch])
    db.commit()
    r = client.get(B + "/placement", headers=w.h["hr1"]).json()
    rows = {x["name"]: x for x in r["proposals"]}
    assert rows["Debt Collection Embu"]["unit_name"] == "Embu"
    assert "Debt Collection Nowhere" not in rows and "Debt Collection Nowhere" in [x["name"] for x in r["unplaced"]]
    assert all(x["unit_name"] != "Debt Collection" for x in r["proposals"])
