"""Linking imported units to website branches and cleaning up the empty copies that setup created."""

from app.models.branch import Branch
from app.models.performance import PerformanceRole, PerformanceUnit, StaffProfile
from performance_tests.org import build_world

B = "/api/admin/performance/org-setup"


def mk(db, name, kind="branch", branch=None):
    u = PerformanceUnit(name=name, kind=kind, branch_id=branch.id if branch else None)
    db.add(u)
    db.flush()
    return u


def world_with_twins(client, db):
    w = build_world(client, db)
    role = db.query(PerformanceRole).filter_by(key="loan_officer").one()
    cbd = Branch(name="Nairobi CBD", address="x", hours="x", phone="0", lat=0.0, lng=0.0)
    ngong = Branch(name="Head Office & Ngong Road", address="x", hours="x", phone="0", lat=0.0, lng=0.0)
    db.add_all([cbd, ngong])
    db.flush()
    imported = {"Cbd": mk(db, "Cbd"), "Ngong Rd": mk(db, "Ngong Rd"), "Check off": mk(db, "Check off", "team")}
    twins = {"cbd": mk(db, "Nairobi CBD", "branch", cbd), "ngong": mk(db, "Head Office & Ngong Road", "branch", ngong), "co": mk(db, "Check-off", "team")}
    for i, u in enumerate(imported.values()):
        db.add(StaffProfile(full_name=f"Person {i}", performance_role_id=role.id, unit_id=u.id))
    db.commit()
    return w, imported, twins, cbd, ngong


def test_duplicates_are_found_and_only_empty_twins_are_offered(client, db):
    w, imported, twins, cbd, ngong = world_with_twins(client, db)
    pairs = client.get(B + "/duplicates", headers=w.h["admin1"]).json()["pairs"]
    got = {p["keep_name"]: (p["remove_name"], p["link_branch_name"]) for p in pairs}
    assert got == {"Cbd": ("Nairobi CBD", "Nairobi CBD"), "Ngong Rd": ("Head Office & Ngong Road", "Head Office & Ngong Road"), "Check off": ("Check-off", None)}
    assert all(p["keep_staff"] == 1 for p in pairs)
    assert client.get(B + "/duplicates", headers=w.h["hr1"]).status_code in (403, 404)  # structure edits are admin-only


def test_clean_up_removes_the_empty_copy_links_and_renames(client, db):
    w, imported, twins, cbd, ngong = world_with_twins(client, db)
    h = w.h["admin1"]
    twin_cbd_id, cbd_id, co_id = twins["cbd"].id, imported["Cbd"].id, imported["Check off"].id
    items = [{"keep_id": imported["Cbd"].id, "remove_id": twins["cbd"].id, "link": True, "use_name": True}, {"keep_id": imported["Check off"].id, "remove_id": twins["co"].id, "use_name": True}]
    assert client.post(B + "/duplicates/clean-up", json={"items": items, "confirm": False}, headers=h).status_code == 422
    r = client.post(B + "/duplicates/clean-up", json={"items": items, "confirm": True}, headers=h)
    assert r.status_code == 200 and r.json() == {"removed": 2, "linked": 1, "renamed": 2}
    db.expire_all()
    c = db.get(PerformanceUnit, cbd_id)
    assert c.name == "Nairobi CBD" and c.branch_id == cbd.id and db.get(PerformanceUnit, twin_cbd_id) is None
    assert db.get(PerformanceUnit, co_id).name == "Check-off"
    assert db.query(StaffProfile).filter(StaffProfile.unit_id == c.id).count() == 1  # people stayed with the kept unit


def test_a_unit_with_people_is_never_removed_and_a_failed_clean_up_changes_nothing(client, db):
    w, imported, twins, cbd, ngong = world_with_twins(client, db)
    h = w.h["admin1"]
    twin_cbd_id, cbd_id = twins["cbd"].id, imported["Cbd"].id
    bad = client.post(B + "/duplicates/clean-up", json={"items": [{"keep_id": imported["Cbd"].id, "remove_id": twins["cbd"].id, "link": True}, {"keep_id": twins["co"].id, "remove_id": imported["Check off"].id}], "confirm": True}, headers=h)
    assert bad.status_code == 400
    db.expire_all()
    assert db.get(PerformanceUnit, twin_cbd_id) is not None and db.get(PerformanceUnit, cbd_id).branch_id is None  # first item rolled back too
    assert client.delete(f"{B}/units/{imported['Ngong Rd'].id}", headers=h).status_code == 409
    assert client.delete(f"{B}/units/{twins['ngong'].id}", headers=h).status_code == 200


def test_link_a_unit_to_a_branch(client, db):
    w, imported, twins, cbd, ngong = world_with_twins(client, db)
    h = w.h["admin1"]
    r = client.post(f"{B}/units/{imported['Ngong Rd'].id}/link-branch", json={"branch_id": ngong.id, "use_branch_name": False, "confirm": True}, headers=h)
    assert r.status_code == 200 and r.json()["removed_duplicate"] == "Head Office & Ngong Road" and r.json()["name"] == "Ngong Rd"
    db.expire_all()
    assert db.get(PerformanceUnit, imported["Ngong Rd"].id).branch_id == ngong.id
    r = client.post(f"{B}/units/{imported['Check off'].id}/link-branch", json={"branch_id": cbd.id, "confirm": True}, headers=h)
    assert r.status_code == 400  # a team can't be a branch
    # a branch whose current unit has people is refused
    other = client.post(f"{B}/units/{imported['Cbd'].id}/link-branch", json={"branch_id": ngong.id, "confirm": True}, headers=h)
    assert other.status_code == 400 and "has people" in other.json()["detail"]
    assert client.post(f"{B}/units/{imported['Cbd'].id}/link-branch", json={"branch_id": None, "confirm": True}, headers=h).status_code == 200
    assert client.post(f"{B}/units/{imported['Cbd'].id}/link-branch", json={"branch_id": "nope", "confirm": True}, headers=h).status_code == 400


def test_layout_suggests_the_imported_unit_for_an_unlinked_branch(client, db):
    w = build_world(client, db)
    role = db.query(PerformanceRole).filter_by(key="loan_officer").one()
    br = Branch(name="Nairobi CBD", address="x", hours="x", phone="0", lat=0.0, lng=0.0)
    db.add(br)
    u = mk(db, "Cbd")
    db.add(StaffProfile(full_name="Someone", performance_role_id=role.id, unit_id=u.id))
    db.commit()
    r = client.get(B + "/layout", headers=w.h["admin1"]).json()
    row = next(b for b in r["branches_without_unit"] if b["name"] == "Nairobi CBD")
    assert row["suggested_unit_name"] == "Cbd" and row["suggested_unit_staff"] == 1
    ok = client.post(f"{B}/units/{row['suggested_unit_id']}/link-branch", json={"branch_id": row["id"], "confirm": True}, headers=w.h["admin1"])
    assert ok.status_code == 200
    r = client.get(B + "/layout", headers=w.h["admin1"]).json()
    assert [b["name"] for b in r["branches_without_unit"]] == []


def test_closing_a_unit_moves_people_keeps_history_and_hides_it(client, db):
    from app.models.performance import StaffAssignmentHistory, UnitRegionLink

    w, imported, twins, cbd, ngong = world_with_twins(client, db)
    h = w.h["admin1"]
    kas = mk(db, "Kasarani")
    role = db.query(PerformanceRole).filter_by(key="loan_officer").one()
    p = StaffProfile(full_name="Kas Person", performance_role_id=role.id, unit_id=kas.id)
    db.add(p)
    db.flush()
    db.add(UnitRegionLink(unit_id=kas.id, region_id=w.region_ids["North"] if hasattr(w, "region_ids") else db.query(__import__("app.models.performance", fromlist=["Region"]).Region).first().id, effective_from=__import__("datetime").date(2026, 1, 1)))
    db.commit()
    kas_id, pid, dest_id = kas.id, p.id, imported["Cbd"].id
    assert client.post(f"{B}/units/{kas_id}/close", json={"move_people_to": dest_id, "confirm": False}, headers=h).status_code == 422
    assert client.post(f"{B}/units/{kas_id}/close", json={"move_people_to": kas_id, "confirm": True}, headers=h).status_code == 400
    r = client.post(f"{B}/units/{kas_id}/close", json={"move_people_to": dest_id, "confirm": True}, headers=h)
    assert r.status_code == 200 and r.json() == {"people_moved": 1}
    db.expire_all()
    assert db.get(StaffProfile, pid).unit_id == dest_id and db.get(PerformanceUnit, kas_id).is_active is False
    assert db.query(StaffAssignmentHistory).filter_by(profile_id=pid, reason="Kasarani closed").count() == 1
    assert db.query(UnitRegionLink).filter_by(unit_id=kas_id, effective_to=None).count() == 0
    names = [u["name"] for x in client.get(B + "/layout", headers=h).json()["regions"] for u in x["units"]] + [u["name"] for u in client.get(B + "/layout", headers=h).json()["units_without_region"]]
    assert "Kasarani" not in names
    assert client.post(f"{B}/units/{kas_id}/close", json={"confirm": True}, headers=h).status_code == 400  # already closed
    for who in ("hr1", "bm1", "lo1"):
        assert client.post(f"{B}/units/{dest_id}/close", json={"confirm": True}, headers=w.h[who]).status_code in (403, 404)
