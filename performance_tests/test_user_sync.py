"""Every login is also a staff member, from either side."""

from app.models.admin_user import AdminUser
from app.models.performance import PerformanceRole, StaffProfile
from app.services import performance_sync as sync
from performance_tests.org import build_world

S = "/api/admin/performance/staff"


def test_user_created_from_admin_users_gets_a_profile(client, db):
    w = build_world(client, db)
    r = client.post("/api/admin/users", json={"username": "jane.doe", "password": "Passw0rd!x", "role": "loan_officer"}, headers=w.h["admin1"])
    assert r.status_code == 201, r.text
    u = db.query(AdminUser).filter_by(username="jane.doe").one()
    p = db.query(StaffProfile).filter_by(admin_user_id=u.id).one()
    assert p.full_name == "Jane Doe" and p.is_active and db.get(PerformanceRole, p.performance_role_id).key == "general_staff"
    r = client.post("/api/admin/users", json={"username": "jd2", "password": "Passw0rd!x", "role": "staff", "full_name": "Real Name"}, headers=w.h["admin1"])
    assert db.query(StaffProfile).filter_by(full_name="Real Name").count() == 1


def test_backfill_creates_links_and_is_idempotent(client, db):
    w = build_world(client, db)
    sync.backfill(db)  # baseline: everyone from the test world has a profile
    from app.services.auth import hash_password

    db.add_all([AdminUser(username="old.timer", password_hash=hash_password("Passw0rd!x"), role="hr"), AdminUser(username="amos.k", password_hash=hash_password("Passw0rd!x"), role="staff")])
    db.add(StaffProfile(full_name="Amos K", performance_role_id=w.p.lo1.performance_role_id))  # imported earlier, no login yet
    db.commit()
    before = db.query(StaffProfile).count()
    out = sync.backfill(db)
    assert out == {"created": 1, "linked": 1}  # old.timer is new; amos.k matches "Amos K" exactly and is linked, not duplicated
    assert db.query(StaffProfile).count() == before + 1
    assert sync.backfill(db) == {"created": 0, "linked": 0}


def test_ambiguous_names_are_never_guessed(client, db):
    w = build_world(client, db)
    sync.backfill(db)
    rid = w.p.lo1.performance_role_id
    db.add_all([StaffProfile(full_name="Sam Roe", performance_role_id=rid), StaffProfile(full_name="sam roe", performance_role_id=rid)])
    db.add(AdminUser(username="sam.roe", password_hash="x", role="staff"))
    db.commit()
    out = sync.backfill(db)
    assert out["created"] == 1 and out["linked"] == 0


def test_deactivating_a_login_mirrors_on_the_profile(client, db):
    w = build_world(client, db)
    u = db.query(AdminUser).filter_by(username="lo3").one()
    r = client.patch(f"/api/admin/users/{u.id}", json={"is_active": False}, headers=w.h["admin1"])
    assert r.status_code == 200
    assert db.query(StaffProfile).filter_by(admin_user_id=u.id).one().is_active is False


def test_staff_created_with_a_login_creates_both(client, db):
    w = build_world(client, db)
    role = w.p.lo1.performance_role_id
    body = {"full_name": "New Person", "performance_role_id": role, "login": {"username": "new.person", "password": "Passw0rd!x", "role": "staff"}}
    assert client.post(S, json=body, headers=w.h["hr1"]).status_code == 403  # only an administrator creates logins
    r = client.post(S, json=body, headers=w.h["admin1"])
    assert r.status_code == 201, r.text
    u = db.query(AdminUser).filter_by(username="new.person").one()
    assert db.query(StaffProfile).filter_by(admin_user_id=u.id, full_name="New Person").count() == 1
    assert client.post(S, json=body, headers=w.h["admin1"]).status_code == 409
    for bad in ({"role": "admin"}, {"role": "nonsense"}):
        r = client.post(S, json={**body, "login": {**body["login"], "username": "x.y.z", **bad}}, headers=w.h["admin1"])
        assert r.status_code == 422
    spare = AdminUser(username="spare.user", password_hash="x", role="staff")
    db.add(spare)
    db.commit()
    r = client.post(S, json={**body, "full_name": "Both", "admin_user_id": spare.id, "login": {**body["login"], "username": "other.one"}}, headers=w.h["admin1"])
    assert r.status_code == 422
    assert client.post(S, json={**body, "login": {**body["login"], "x": 1}}, headers=w.h["admin1"]).status_code == 422
