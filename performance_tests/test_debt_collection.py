"""Debt collection: a unit under every branch, collectors who may cover several branches, and who they report to."""

from app.models.admin_user import AdminUser
from app.models.branch import Branch
from app.models.performance import PerformanceRole, PerformanceUnit, StaffProfile, StaffUnitCoverage, UnitRegionLink
from app.services import performance_units as units_svc
from performance_tests.org import build_world

BASE = "/api/admin/performance"
SETUP = BASE + "/org-setup"


def role(db, key):
    return db.query(PerformanceRole).filter_by(key=key).one()


def region_of(db, unit):
    link = db.query(UnitRegionLink).filter(UnitRegionLink.unit_id == unit.id, UnitRegionLink.effective_to.is_(None)).first()
    return link.region_id if link else None


def add_debt_units(client, w):
    r = client.post(SETUP + "/debt-collection-units", headers=w.h["admin1"])
    assert r.status_code == 200, r.text
    return r.json()


def test_every_branch_gets_its_own_debt_collection_unit_in_its_region(client, db):
    w = build_world(client, db)
    assert add_debt_units(client, w) == {"created": 2, "adopted": 0}
    alpha_dc = db.query(PerformanceUnit).filter_by(name="Alpha Debt Collection").one()
    beta_dc = db.query(PerformanceUnit).filter_by(name="Beta Debt Collection").one()
    assert (alpha_dc.parent_unit_id, alpha_dc.kind) == (w.alpha.id, "team") and beta_dc.parent_unit_id == w.beta.id
    assert region_of(db, alpha_dc) == w.north.id and region_of(db, beta_dc) == w.south.id
    assert add_debt_units(client, w) == {"created": 0, "adopted": 0}  # safe to run again


def test_an_existing_unit_with_that_name_is_adopted_not_duplicated(client, db):
    w = build_world(client, db)
    db.add(PerformanceUnit(name="Alpha Debt Collection", kind="team"))
    db.commit()
    assert add_debt_units(client, w) == {"created": 1, "adopted": 1}
    assert db.query(PerformanceUnit).filter_by(name="Alpha Debt Collection").one().parent_unit_id == w.alpha.id


def test_only_an_admin_can_add_them(client, db):
    w = build_world(client, db)
    for who in ("hr1", "lo1", "bm1"):
        assert client.post(SETUP + "/debt-collection-units", headers=w.h[who]).status_code in (401, 403, 404), who
    assert client.post(SETUP + "/debt-collection-units").status_code in (401, 403)


def test_the_layout_shows_them_under_their_branch_not_as_units_of_their_own(client, db):
    w = build_world(client, db)
    before = client.get(SETUP + "/layout", headers=w.h["admin1"]).json()
    assert before["debt_units_missing"] == 2
    add_debt_units(client, w)
    after = client.get(SETUP + "/layout", headers=w.h["admin1"]).json()
    assert after["debt_units_missing"] == 0
    north = next(r for r in after["regions"] if r["name"] == "North")
    assert [u["name"] for u in north["units"]] == ["Alpha"]
    assert [c["name"] for c in north["units"][0]["children"]] == ["Alpha Debt Collection"]
    assert after["units_without_region"] == []


def test_a_branch_moving_region_takes_its_debt_collection_with_it(client, db):
    w = build_world(client, db)
    add_debt_units(client, w)
    r = client.put(f"{BASE}/units/{w.alpha.id}/region", json={"region_id": w.south.id}, headers=w.h["admin1"])
    assert r.status_code == 200, r.text
    assert region_of(db, db.query(PerformanceUnit).filter_by(name="Alpha Debt Collection").one()) == w.south.id


def test_a_unit_can_be_created_under_a_branch_but_only_one_level_deep(client, db):
    w = build_world(client, db)
    h = w.h["admin1"]
    ok = client.post(f"{BASE}/units", json={"name": "Alpha Recoveries", "kind": "team", "parent_unit_id": w.alpha.id}, headers=h)
    assert ok.status_code == 201 and ok.json()["data"]["parent_unit_id"] == w.alpha.id
    assert region_of(db, db.query(PerformanceUnit).filter_by(name="Alpha Recoveries").one()) == w.north.id
    nested = client.post(f"{BASE}/units", json={"name": "Too Deep", "kind": "team", "parent_unit_id": ok.json()["data"]["id"]}, headers=h)
    missing = client.post(f"{BASE}/units", json={"name": "Orphan", "kind": "team", "parent_unit_id": "nope"}, headers=h)
    assert nested.status_code == 400 and missing.status_code == 400


def test_a_branch_with_a_staffed_debt_unit_cannot_be_removed_but_an_empty_one_goes_with_its_debt_unit(client, db):
    w = build_world(client, db)
    add_debt_units(client, w)
    gamma = PerformanceUnit(name="Gamma", kind="branch")
    db.add(gamma)
    db.commit()
    add_debt_units(client, w)
    gamma_dc = db.query(PerformanceUnit).filter_by(name="Gamma Debt Collection").one()
    assert units_svc.is_empty(db, gamma.id)
    units_svc.delete_if_empty(db, gamma)
    db.commit()
    assert db.query(PerformanceUnit).filter(PerformanceUnit.id.in_([gamma.id, gamma_dc.id])).count() == 0

    delta = PerformanceUnit(name="Delta", kind="branch")
    db.add(delta)
    db.commit()
    add_debt_units(client, w)
    delta_dc = db.query(PerformanceUnit).filter_by(name="Delta Debt Collection").one()
    db.add(StaffProfile(full_name="Dee Collector", performance_role_id=role(db, "debt_recovery").id, unit_id=delta_dc.id))
    db.commit()
    assert not units_svc.is_empty(db, delta.id)
    try:
        units_svc.delete_if_empty(db, delta)
        raise AssertionError("should have refused")
    except units_svc.UnitError:
        pass


def test_closing_a_branch_closes_its_debt_unit_and_moves_collectors_to_the_other_branchs(client, db):
    w = build_world(client, db)
    add_debt_units(client, w)
    alpha_dc = db.query(PerformanceUnit).filter_by(name="Alpha Debt Collection").one()
    beta_dc = db.query(PerformanceUnit).filter_by(name="Beta Debt Collection").one()
    collector = StaffProfile(full_name="Carl Collector", performance_role_id=role(db, "debt_recovery").id, unit_id=alpha_dc.id)
    db.add(collector)
    db.commit()
    units_svc.close_unit(db, db.get(PerformanceUnit, w.alpha.id), w.beta.id, "admin1")
    db.refresh(collector)
    db.refresh(alpha_dc)
    assert alpha_dc.is_active is False and collector.unit_id == beta_dc.id


# ---- placement: who the collectors and their manager report to ----------------------------------------------------------


def make_placement_world(client, db):
    w = build_world(client, db)
    add_debt_units(client, w)
    dcm_role = PerformanceRole(key="debt_collection_manager", name="Debt Collection Manager", kind="appraisal")
    db.add(dcm_role)
    db.add(PerformanceUnit(name="Head Office", kind="head_office"))
    db.flush()
    gm = StaffProfile(full_name="Gina General", performance_role_id=role(db, "general_manager").id)
    dcm = StaffProfile(full_name="Dan Manager", performance_role_id=dcm_role.id)
    branch = db.get(Branch, w.alpha.branch_id) if w.alpha.branch_id else None
    if branch is None:
        branch = Branch(name="Alpha", address="x", hours="x", phone="0", lat=0.0, lng=0.0)
        db.add(branch)
        db.flush()
        w.alpha.branch_id = branch.id
    user = AdminUser(username="debt.alpha", password_hash="x", role="staff", branch_id=branch.id)
    db.add_all([gm, dcm, user])
    db.flush()
    collector = StaffProfile(full_name="Cora Collector", performance_role_id=role(db, "debt_recovery").id, admin_user_id=user.id)
    db.add(collector)
    db.commit()
    return w, gm, dcm, collector


def test_a_collector_goes_to_their_branchs_debt_unit_and_reports_to_the_debt_collection_manager(client, db):
    w, gm, dcm, collector = make_placement_world(client, db)
    rows = {x["name"]: x for x in client.get(SETUP + "/placement", headers=w.h["hr1"]).json()["proposals"]}
    c = rows["Cora Collector"]
    assert c["unit_name"] == "Alpha Debt Collection" and c["manager_id"] == dcm.id
    assert "debt collector" in c["unit_reason"] and c["manager_reason"] == "the Debt Collection Manager"


def test_the_debt_collection_manager_sits_in_head_office_under_the_general_manager(client, db):
    w, gm, dcm, collector = make_placement_world(client, db)
    rows = {x["name"]: x for x in client.get(SETUP + "/placement", headers=w.h["hr1"]).json()["proposals"]}
    d = rows["Dan Manager"]
    assert d["unit_name"] == "Head Office" and d["manager_id"] == gm.id and d["manager_reason"] == "the General Manager"


def test_without_a_debt_collection_manager_a_collector_falls_back_to_the_normal_rule(client, db):
    w, gm, dcm, collector = make_placement_world(client, db)
    dcm.is_active = False
    db.commit()
    alpha_dc = db.query(PerformanceUnit).filter_by(name="Alpha Debt Collection").one()
    alpha_dc.manager_profile_id = w.p.bm1.id
    db.commit()
    rows = {x["name"]: x for x in client.get(SETUP + "/placement", headers=w.h["hr1"]).json()["proposals"]}
    assert rows["Cora Collector"]["manager_id"] == w.p.bm1.id  # the unit's own manager


# ---- coverage: one collector, several branches --------------------------------------------------------------------------


def test_a_collector_can_cover_more_than_one_branch(client, db):
    w = build_world(client, db)
    add_debt_units(client, w)
    alpha_dc = db.query(PerformanceUnit).filter_by(name="Alpha Debt Collection").one()
    beta_dc = db.query(PerformanceUnit).filter_by(name="Beta Debt Collection").one()
    collector = StaffProfile(full_name="Cora Collector", performance_role_id=role(db, "debt_recovery").id, unit_id=alpha_dc.id)
    db.add(collector)
    db.commit()
    h = w.h["hr1"]
    url = f"{BASE}/staff/{collector.id}/coverage"
    assert client.get(url, headers=h).json()["units"] == []
    put = client.put(url, json={"unit_ids": [beta_dc.id, alpha_dc.id, beta_dc.id]}, headers=h)  # main unit and repeats are ignored
    assert put.status_code == 200 and [u["name"] for u in put.json()["units"]] == ["Beta Debt Collection"]
    assert put.json()["main_unit_id"] == alpha_dc.id
    assert client.put(url, json={"unit_ids": []}, headers=h).json()["units"] == []  # replace, so an empty list clears it
    assert db.query(StaffUnitCoverage).filter_by(profile_id=collector.id).count() == 0


def test_coverage_rejects_bad_units_unknown_people_and_the_wrong_caller(client, db):
    w = build_world(client, db)
    add_debt_units(client, w)
    beta_dc = db.query(PerformanceUnit).filter_by(name="Beta Debt Collection").one()
    p = w.p.lo1
    url = f"{BASE}/staff/{p.id}/coverage"
    assert client.put(url, json={"unit_ids": ["nope"]}, headers=w.h["hr1"]).status_code == 400
    assert client.put(f"{BASE}/staff/missing/coverage", json={"unit_ids": []}, headers=w.h["hr1"]).status_code == 404
    for who in ("lo1", "bm1", "gm"):
        assert client.put(url, json={"unit_ids": [beta_dc.id]}, headers=w.h[who]).status_code in (401, 403, 404), who
    beta_dc.is_active = False
    db.commit()
    assert client.put(url, json={"unit_ids": [beta_dc.id]}, headers=w.h["admin1"]).status_code == 400  # closed


def test_the_structure_data_carries_the_parent_link_and_who_covers_what(client, db):
    w = build_world(client, db)
    add_debt_units(client, w)
    alpha_dc = db.query(PerformanceUnit).filter_by(name="Alpha Debt Collection").one()
    beta_dc = db.query(PerformanceUnit).filter_by(name="Beta Debt Collection").one()
    collector = StaffProfile(full_name="Cora Collector", performance_role_id=role(db, "debt_recovery").id, unit_id=alpha_dc.id)
    db.add(collector)
    db.commit()
    client.put(f"{BASE}/staff/{collector.id}/coverage", json={"unit_ids": [beta_dc.id]}, headers=w.h["admin1"])
    s = client.get(f"{BASE}/org-structure", headers=w.h["admin1"]).json()
    units = {u["name"]: u for u in s["units"]}
    assert units["Alpha Debt Collection"]["parent_unit_id"] == w.alpha.id and units["Alpha"]["parent_unit_id"] is None
    assert next(x for x in s["people"] if x["id"] == collector.id)["covers"] == [beta_dc.id]
    assert next(x for x in s["people"] if x["id"] == w.p.lo1.id)["covers"] == []


def test_closing_a_unit_ends_the_coverage_of_it(client, db):
    w = build_world(client, db)
    add_debt_units(client, w)
    alpha_dc = db.query(PerformanceUnit).filter_by(name="Alpha Debt Collection").one()
    beta_dc = db.query(PerformanceUnit).filter_by(name="Beta Debt Collection").one()
    collector = StaffProfile(full_name="Cora Collector", performance_role_id=role(db, "debt_recovery").id, unit_id=alpha_dc.id)
    db.add(collector)
    db.commit()
    client.put(f"{BASE}/staff/{collector.id}/coverage", json={"unit_ids": [beta_dc.id]}, headers=w.h["admin1"])
    units_svc.close_unit(db, beta_dc, None, "admin1")
    assert db.query(StaffUnitCoverage).filter_by(profile_id=collector.id).count() == 0
