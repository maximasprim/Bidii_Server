"""
Authorization: every route is guarded, nobody sees outside their scope (IDOR),
restricted actions (PIP etc.) reach only HR + direct manager + the person, and
the read-only executive view cannot write.
"""

import json
import re
from datetime import date

import pytest
from fastapi.routing import APIRoute

from app.main import app
from app.models.performance import PerformanceAuditLog, PerformanceUnit, StaffProfile, UnitRegionLink
from app.models.role_permission import RolePermission
from performance_tests.conftest import add_user, login, token_headers
from performance_tests.org import build_org

PREFIX = "/api/admin/performance"
BASE = PREFIX


@pytest.fixture()
def org(client, db):
    return build_org(client, db)


def _ids(resp):
    return {i["id"] for i in resp.json()["items"]}


# ---- every route is guarded ---------------------------------------------------------------


def _guard_names(route: APIRoute) -> set[str]:
    found: set[str] = set()

    def walk(dependant):
        for dep in dependant.dependencies:
            qn = getattr(dep.call, "__qualname__", "")
            if qn.startswith(("require_menu_access.", "require_roles.")):
                found.add(qn.split(".")[0])
            walk(dep)

    walk(route.dependant)
    return found


def _performance_routes():
    return [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith(PREFIX)]


def test_the_enumerator_actually_sees_routes_and_guards():
    routes = _performance_routes()
    assert len(routes) >= 20
    me = next(r for r in routes if r.path.endswith("/me"))
    assert _guard_names(me) == {"require_menu_access"}  # /me is no longer get_current_admin-only


def test_every_performance_route_has_an_explicit_menu_or_role_guard():
    unguarded = [f"{sorted(r.methods)} {r.path}" for r in _performance_routes() if not _guard_names(r)]
    assert unguarded == [], f"Routes with no menu/role dependency: {unguarded}"


def test_every_performance_route_rejects_anonymous_callers(client):
    for r in _performance_routes():
        url = re.sub(r"\{[^}]+\}", "x", r.path)
        for method in r.methods - {"HEAD", "OPTIONS"}:
            resp = client.request(method, url, json={} if method in ("POST", "PUT", "PATCH") else None)
            assert resp.status_code in (401, 403), f"{method} {r.path} -> {resp.status_code}"


# ---- menu gating ------------------------------------------------------------------------------


def test_me_requires_the_my_performance_menu(client, db, org):
    db.add(RolePermission(role="loan_officer", allowed_menus=["/admin/loans"]))
    db.commit()
    assert client.get(f"{BASE}/me", headers=org.h["lo1"]).status_code == 403
    db.query(RolePermission).delete()
    db.commit()
    assert client.get(f"{BASE}/me", headers=org.h["lo1"]).status_code == 200


def test_me_reports_scale_and_scope(client, org):
    me = client.get(f"{BASE}/me", headers=org.h["lo1"]).json()
    assert me["profile"]["full_name"] == "Officer One" and me["team_size"] == 0 and not me["sees_everyone"]
    assert [c["min_score"] for c in me["categories"]] == [120, 90, 60, 0]
    assert client.get(f"{BASE}/me", headers=org.h["bm1"]).json()["team_size"] == 2
    ceo = client.get(f"{BASE}/me", headers=org.h["ceo"]).json()
    assert ceo["sees_everyone"] and ceo["read_only"]
    assert not client.get(f"{BASE}/me", headers=org.h["hr1"]).json()["read_only"]


def test_user_without_a_profile_gets_an_empty_me(client, db, org):
    add_user(db, "nobody", "staff")
    me = client.get(f"{BASE}/me", headers=token_headers(db.query(__import__("app.models.admin_user", fromlist=["AdminUser"]).AdminUser).filter_by(username="nobody").one())).json()
    assert me["profile"] is None and me["team_size"] == 0


# ---- admin-only surfaces -------------------------------------------------------------------------


# The performance settings (config, rating scales, job roles): admin or HR may save them.
SETTINGS_WRITES = [
    ("PUT", "/config", {"config": {}}),
    ("POST", "/scales", {}),
    ("POST", "/scales/preview", {"categories": []}),
    ("POST", "/roles", {"name": "Hacker Role"}),
    ("PATCH", "/roles/x", {"name": "Zed Name"}),
    ("DELETE", "/roles/x", None),
]

ADMIN_ONLY = [
    ("POST", "/units", {"name": "Evil Unit"}),
    ("PATCH", "/units/x", {"name": "Evil Unit"}),
    ("PUT", "/units/x/region", {}),
    ("POST", "/regions", {"name": "Evil Region"}),
    ("PATCH", "/regions/x", {"name": "Evil Region"}),
    ("GET", "/audit", None),
]


@pytest.mark.parametrize("method,path,body", ADMIN_ONLY)
@pytest.mark.parametrize("who", ["lo1", "bm1", "gm", "ceo", "hr1"])
def test_admin_only_routes_reject_everyone_else(client, org, who, method, path, body):
    resp = client.request(method, BASE + path, json=body, headers=org.h[who])
    assert resp.status_code == 403, f"{who} {method} {path} -> {resp.status_code}"


@pytest.mark.parametrize("method,path,body", SETTINGS_WRITES)
@pytest.mark.parametrize("who", ["lo1", "bm1", "gm", "ceo"])
def test_settings_writes_reject_everyone_but_admin_and_hr(client, org, who, method, path, body):
    resp = client.request(method, BASE + path, json=body, headers=org.h[who])
    assert resp.status_code == 403, f"{who} {method} {path} -> {resp.status_code}"


@pytest.mark.parametrize("method,path,body", SETTINGS_WRITES)
def test_hr_is_let_through_to_the_settings_writes(client, org, method, path, body):
    resp = client.request(method, BASE + path, json=body, headers=org.h["hr1"])
    assert resp.status_code not in (401, 403), f"hr {method} {path} -> {resp.status_code}"  # may still be 400/404/422 for these junk bodies


def test_hr_can_add_edit_and_remove_a_job_role_and_save_the_config(client, org):
    h = org.h["hr1"]
    created = client.post(f"{BASE}/roles", json={"name": "Debt Collection Manager", "department": "Commercial"}, headers=h)
    assert created.status_code == 201, created.text
    rid = created.json()["data"]["id"]
    assert created.json()["data"]["department"] == "Commercial"
    renamed = client.patch(f"{BASE}/roles/{rid}", json={"name": "Debt Collections Manager"}, headers=h)
    assert renamed.status_code == 200 and renamed.json()["data"]["name"] == "Debt Collections Manager"
    assert client.delete(f"{BASE}/roles/{rid}", headers=h).status_code == 200
    cfg = client.get(f"{BASE}/config", headers=h).json()["config"]
    assert client.put(f"{BASE}/config", json={"config": cfg}, headers=h).status_code == 200


@pytest.mark.parametrize("who", ["lo1", "bm1", "gm", "ceo"])
def test_staff_writes_and_linkable_users_are_admin_or_hr_only(client, org, who):
    assert client.get(f"{BASE}/staff/linkable-users", headers=org.h[who]).status_code == 403
    assert client.post(f"{BASE}/staff", json={"full_name": "New Person", "performance_role_id": org.role.id}, headers=org.h[who]).status_code == 403
    assert client.patch(f"{BASE}/staff/{org.p.lo1.id}", json={"full_name": "Renamed X"}, headers=org.h[who]).status_code == 403


# ---- scope / IDOR ---------------------------------------------------------------------------------


def test_staff_list_scope_per_viewer(client, org):
    p = org.p
    expect = {
        "lo1": {p.lo1.id},
        "lo3": {p.lo3.id},
        "bm1": {p.bm1.id, p.lo1.id, p.lo2.id},
        "bm2": {p.bm2.id, p.lo3.id},
        # GM manages region North (unit Alpha) AND is line manager of bm1/bm2 -> sees all of their teams.
        "gm": {p.gm.id, p.bm1.id, p.bm2.id, p.lo1.id, p.lo2.id, p.lo3.id},
    }
    for who, ids in expect.items():
        r = client.get(f"{BASE}/staff", headers=org.h[who])
        assert r.status_code == 200 and _ids(r) == ids, who
    everyone = {x.id for x in vars(p).values()}
    for who in ("admin1", "hr1", "ceo"):
        assert _ids(client.get(f"{BASE}/staff", headers=org.h[who])) == everyone, who


def test_out_of_scope_id_is_indistinguishable_from_a_missing_one(client, org):
    mine = client.get(f"{BASE}/staff/{org.p.lo3.id}", headers=org.h["lo1"])  # exists, not theirs
    missing = client.get(f"{BASE}/staff/does-not-exist", headers=org.h["lo1"])
    assert mine.status_code == missing.status_code == 404
    assert mine.json() == missing.json()


@pytest.mark.parametrize("suffix", ["", "/history", "/actions"])
def test_no_resource_under_a_staff_id_leaks_across_scope(client, org, suffix):
    for who, other in (("lo1", org.p.lo3), ("lo1", org.p.lo2), ("lo1", org.p.bm1), ("bm1", org.p.lo3), ("bm2", org.p.lo1)):
        a = client.get(f"{BASE}/staff/{other.id}{suffix}", headers=org.h[who])
        b = client.get(f"{BASE}/staff/nope{suffix}", headers=org.h[who])
        assert a.status_code == b.status_code == 404 and a.json() == b.json(), (who, other.full_name, suffix)


def test_cannot_record_an_action_on_someone_out_of_scope(client, org):
    resp = client.post(f"{BASE}/staff/{org.p.lo3.id}/actions", json={"action_type": "talk", "started_on": "2026-05-01"}, headers=org.h["bm1"])
    assert resp.status_code == 404
    resp = client.post(f"{BASE}/staff/{org.p.lo2.id}/actions", json={"action_type": "talk", "started_on": "2026-05-01"}, headers=org.h["lo1"])
    assert resp.status_code == 404  # peer: not even visible


def test_filtering_by_another_branch_cannot_widen_scope(client, org):
    r = client.get(f"{BASE}/staff", params={"unit_id": org.beta.id}, headers=org.h["bm1"])
    assert _ids(r) == set()
    r = client.get(f"{BASE}/staff", params={"manager_id": org.p.bm2.id}, headers=org.h["lo1"])
    assert _ids(r) == set()


def test_region_manager_scope_follows_effective_dated_links(client, db, org):
    # Beta joins North only from the future: must not widen the region manager's view yet.
    db.add(UnitRegionLink(unit_id=org.beta.id, region_id=org.north.id, effective_from=date(2999, 1, 1)))
    db.commit()
    org.p.gm.manager_id = None
    org.p.bm1.manager_id = None
    org.p.bm2.manager_id = None
    db.commit()
    r = client.get(f"{BASE}/staff", headers=org.h["gm"])
    assert _ids(r) == {org.p.gm.id, org.p.bm1.id, org.p.lo1.id, org.p.lo2.id}  # only Alpha (North today)


def _public_branch(db, name):
    from app.models.branch import Branch

    branch = Branch(name=name, address="x", hours="x", phone="0", lat=0.0, lng=0.0)
    db.add(branch)
    db.commit()
    return branch


def test_branch_office_admin_sees_only_the_units_linked_to_their_branch(client, db, org):
    mine, other = _public_branch(db, "Linked Branch"), _public_branch(db, "Other Branch")
    org.alpha.branch_id = mine.id  # Alpha is the unit tied to the account's branch
    org.beta.branch_id = other.id
    boa = add_user(db, "boa", "branch_office_admin", branch_id=mine.id)
    db.commit()
    seen = _ids(client.get(f"{BASE}/staff", headers=token_headers(boa)))
    assert seen == {org.p.bm1.id, org.p.lo1.id, org.p.lo2.id}  # everyone in Alpha, nobody in Beta
    assert client.get(f"{BASE}/staff/{org.p.lo3.id}", headers=token_headers(boa)).status_code == 404
    # Seeing a branch is not authority over PIPs, and it is not an HR/admin write permission.
    assert client.patch(f"{BASE}/staff/{org.p.lo1.id}", json={"full_name": "Renamed X"}, headers=token_headers(boa)).status_code == 403


def test_branch_office_admin_managed_branch_ids_extend_the_view(client, db, org):
    a, b = _public_branch(db, "Branch A"), _public_branch(db, "Branch B")
    org.alpha.branch_id, org.beta.branch_id = a.id, b.id
    boa = add_user(db, "boa2", "branch_office_admin", branch_id=a.id, managed_branch_ids=[b.id])
    db.commit()
    seen = _ids(client.get(f"{BASE}/staff", headers=token_headers(boa)))
    assert {org.p.lo1.id, org.p.lo3.id} <= seen and org.p.gm.id not in seen


def test_branch_office_admin_without_a_branch_sees_nothing(client, db, org):
    boa = add_user(db, "boa3", "branch_office_admin")
    assert _ids(client.get(f"{BASE}/staff", headers=token_headers(boa))) == set()


# ---- read-only executive ------------------------------------------------------------------------------


def test_executive_sees_everything_but_cannot_change_anything(client, org):
    h = org.h["ceo"]
    assert client.get(f"{BASE}/staff/{org.p.lo3.id}", headers=h).status_code == 200
    assert client.post(f"{BASE}/staff", json={"full_name": "Ghost Hire", "performance_role_id": org.role.id}, headers=h).status_code == 403
    assert client.patch(f"{BASE}/staff/{org.p.lo1.id}", json={"full_name": "Renamed X"}, headers=h).status_code == 403
    assert client.post(f"{BASE}/staff/{org.p.lo1.id}/actions", json={"action_type": "talk", "started_on": "2026-05-01"}, headers=h).status_code == 403
    assert client.post(f"{BASE}/staff/{org.p.lo1.id}/actions", json={"action_type": "recognition", "started_on": "2026-05-01"}, headers=h).status_code == 403


def test_only_an_admin_can_grant_the_executive_view(client, org):
    assert client.patch(f"{BASE}/staff/{org.p.lo1.id}", json={"is_executive": True}, headers=org.h["hr1"]).status_code == 403
    assert client.post(f"{BASE}/staff", json={"full_name": "Sneaky Exec", "performance_role_id": org.role.id, "is_executive": True}, headers=org.h["hr1"]).status_code == 403
    assert client.patch(f"{BASE}/staff/{org.p.lo1.id}", json={"is_executive": True}, headers=org.h["admin1"]).status_code == 200


# ---- PIP / restricted actions -------------------------------------------------------------------------


def _pip(client, org, who="hr1", target=None):
    target = target or org.p.lo1
    return client.post(f"{BASE}/staff/{target.id}/actions", json={"action_type": "pip", "started_on": "2026-05-01", "note": "SECRET-NOTE-TEXT"}, headers=org.h[who])


def _status_as(client, org, who, target):
    return client.get(f"{BASE}/staff/{target.id}", headers=org.h[who]).json()["status"]


def test_pip_visible_only_to_hr_direct_manager_and_the_person(client, org):
    assert _pip(client, org).status_code == 201
    lo1 = org.p.lo1
    audience = {"hr1": True, "admin1": True, "bm1": True, "lo1": True}  # HR, administrator (does everything HR can), direct manager, the person
    outsiders = ["gm", "ceo"]  # skip-level (and region) manager, executive
    for who, _ in audience.items():
        assert _status_as(client, org, who, lo1) == "pip", who
        items = client.get(f"{BASE}/staff/{lo1.id}/actions", headers=org.h[who]).json()["items"]
        assert [i["action_type"] for i in items] == ["pip"], who
    for who in outsiders:
        assert _status_as(client, org, who, lo1) == "active", who  # the PIP is not even hinted at
        assert client.get(f"{BASE}/staff/{lo1.id}/actions", headers=org.h[who]).json()["items"] == [], who


def test_peers_and_other_branches_cannot_see_a_pip_at_all(client, org):
    _pip(client, org)
    for who in ("lo2", "lo3", "bm2"):
        assert client.get(f"{BASE}/staff/{org.p.lo1.id}", headers=org.h[who]).status_code == 404


def test_status_filter_cannot_be_used_to_discover_a_pip(client, org):
    _pip(client, org)
    assert _ids(client.get(f"{BASE}/staff", params={"status": "pip"}, headers=org.h["gm"])) == set()
    assert _ids(client.get(f"{BASE}/staff", params={"status": "pip"}, headers=org.h["admin1"])) == {org.p.lo1.id}  # an administrator is entitled like HR
    assert _ids(client.get(f"{BASE}/staff", params={"status": "pip"}, headers=org.h["hr1"])) == {org.p.lo1.id}
    assert _ids(client.get(f"{BASE}/staff", params={"status": "pip"}, headers=org.h["bm1"])) == {org.p.lo1.id}
    # ...and a PIP profile shows up under "active" for those who must not know.
    assert org.p.lo1.id in _ids(client.get(f"{BASE}/staff", params={"status": "active"}, headers=org.h["gm"]))
    assert org.p.lo1.id not in _ids(client.get(f"{BASE}/staff", params={"status": "active"}, headers=org.h["hr1"]))


def test_who_may_create_restricted_actions(client, org):
    assert _pip(client, org, "admin1").status_code == 201  # an administrator can do what HR can
    assert _pip(client, org, "gm").status_code == 403  # skip-level
    assert _pip(client, org, "ceo").status_code == 403
    assert _pip(client, org, "bm1").status_code == 201  # direct manager
    assert _pip(client, org, "bm1", org.p.lo3).status_code == 404  # not their report / out of scope
    assert _pip(client, org, "hr1", org.p.lo3).status_code == 201


def test_non_restricted_actions_are_visible_to_the_wider_scope(client, org):
    assert client.post(f"{BASE}/staff/{org.p.lo1.id}/actions", json={"action_type": "talk", "started_on": "2026-05-01"}, headers=org.h["admin1"]).status_code == 201
    items = client.get(f"{BASE}/staff/{org.p.lo1.id}/actions", headers=org.h["gm"]).json()["items"]
    assert [i["action_type"] for i in items] == ["talk"]


def test_pip_status_cannot_be_flipped_through_a_profile_patch_by_outsiders(client, org):
    _pip(client, org)
    for who in ("gm", "ceo"):
        assert client.patch(f"{BASE}/staff/{org.p.lo1.id}", json={"status": "active"}, headers=org.h[who]).status_code in (403, 404), who
    assert client.patch(f"{BASE}/staff/{org.p.lo1.id}", json={"status": "active"}, headers=org.h["admin1"]).status_code == 200
    assert client.patch(f"{BASE}/staff/{org.p.lo2.id}", json={"status": "pip"}, headers=org.h["admin1"]).status_code == 200
    assert client.patch(f"{BASE}/staff/{org.p.lo1.id}", json={"status": "active"}, headers=org.h["hr1"]).status_code == 200


def test_pip_lifted_restores_active_status(client, org):
    _pip(client, org)
    r = client.post(f"{BASE}/staff/{org.p.lo1.id}/actions", json={"action_type": "pip_lifted", "started_on": "2026-07-01"}, headers=org.h["hr1"])
    assert r.status_code == 201
    assert _status_as(client, org, "hr1", org.p.lo1) == "active"


def test_audit_log_records_ids_never_note_text(client, db, org):
    _pip(client, org)
    dump = json.dumps([r.details for r in db.query(PerformanceAuditLog).all()])
    assert "SECRET-NOTE-TEXT" not in dump
    assert any(r.action == "action_created" for r in db.query(PerformanceAuditLog))


# ---- staff create/update, history, loops ---------------------------------------------------------------


def test_assignment_history_tracks_transfers(client, org):
    h = org.h["hr1"]
    created = client.post(f"{BASE}/staff", json={"full_name": "Transfer Person", "performance_role_id": org.role.id, "unit_id": org.alpha.id, "manager_id": org.p.bm1.id, "hire_date": "2026-01-05"}, headers=h)
    assert created.status_code == 201, created.text
    pid = created.json()["data"]["id"]
    first = client.get(f"{BASE}/staff/{pid}/history", headers=h).json()["items"]
    assert len(first) == 1 and first[0]["effective_from"] == "2026-01-05" and first[0]["effective_to"] is None
    moved = client.patch(f"{BASE}/staff/{pid}", json={"unit_id": org.beta.id, "manager_id": org.p.bm2.id, "change_reason": "Transfer"}, headers=h)
    assert moved.status_code == 200
    rows = client.get(f"{BASE}/staff/{pid}/history", headers=h).json()["items"]
    assert len(rows) == 2
    current = next(r for r in rows if r["effective_to"] is None)
    closed = next(r for r in rows if r["effective_to"] is not None)
    assert current["unit_id"] == org.beta.id and current["reason"] == "Transfer"
    assert closed["unit_id"] == org.alpha.id and closed["effective_to"] < current["effective_from"]
    # An unrelated edit adds no history row.
    client.patch(f"{BASE}/staff/{pid}", json={"employee_no": "E1"}, headers=h)
    assert len(client.get(f"{BASE}/staff/{pid}/history", headers=h).json()["items"]) == 2


def test_reporting_loops_and_duplicate_logins_rejected(client, db, org):
    h = org.h["hr1"]
    assert client.patch(f"{BASE}/staff/{org.p.gm.id}", json={"manager_id": org.p.lo1.id}, headers=h).status_code == 400
    assert client.patch(f"{BASE}/staff/{org.p.lo1.id}", json={"manager_id": org.p.lo1.id}, headers=h).status_code == 400
    other = add_user(db, "dup", "staff")
    assert client.patch(f"{BASE}/staff/{org.p.lo2.id}", json={"admin_user_id": other.id}, headers=h).status_code == 200
    assert client.patch(f"{BASE}/staff/{org.p.lo1.id}", json={"admin_user_id": other.id}, headers=h).status_code == 409


def test_staff_without_a_login_can_exist_and_be_linked_later(client, db, org):
    h = org.h["hr1"]
    pid = client.post(f"{BASE}/staff", json={"full_name": "No Login Yet", "performance_role_id": org.role.id, "unit_id": org.alpha.id}, headers=h).json()["data"]["id"]
    assert client.get(f"{BASE}/staff/{pid}", headers=h).json()["admin_user_id"] is None
    user = add_user(db, "later", "loan_officer")
    assert client.patch(f"{BASE}/staff/{pid}", json={"admin_user_id": user.id}, headers=h).status_code == 200
    assert db.query(StaffProfile).filter_by(id=pid).first().admin_user_id == user.id


# ---- units, regions, scales (admin happy paths) ----------------------------------------------------------------


def test_region_assignment_is_effective_dated(client, org):
    h = org.h["admin1"]
    r = client.put(f"{BASE}/units/{org.alpha.id}/region", json={"region_id": org.south.id, "effective_from": "2999-01-01"}, headers=h)
    assert r.status_code == 200
    unit = next(u for u in client.get(f"{BASE}/units", headers=h).json()["items"] if u["id"] == org.alpha.id)
    assert unit["region_name"] == "North"  # the move hasn't happened yet
    early = client.put(f"{BASE}/units/{org.alpha.id}/region", json={"region_id": org.south.id, "effective_from": "2000-01-01"}, headers=h)
    assert early.status_code == 400


def test_unit_and_region_name_uniqueness(client, org):
    h = org.h["admin1"]
    assert client.post(f"{BASE}/units", json={"name": "Alpha"}, headers=h).status_code == 409
    assert client.post(f"{BASE}/regions", json={"name": "North"}, headers=h).status_code == 409
    assert client.post(f"{BASE}/units", json={"name": "Kasarani", "kind": "branch"}, headers=h).status_code == 201


@pytest.mark.parametrize("who", ["admin1", "hr1"])
def test_scales_endpoints_admin_and_hr_flow(client, org, who):
    h = org.h[who]
    cats = [{"key": k, "label": k, "min_score": m} for k, m in zip(
        ["above_expectation", "meeting_expectation", "approaching_expectation", "below_expectation"], [125, 95, 65, 0])]
    bands = [{"rating": r, "label": str(r), "min_pct": m} for r, m in zip([5, 4, 3, 2, 1], [120, 100, 80, 60, 0])]
    pi = {"5": 125, "4": 110, "3": 90, "2": 70, "1": 50}
    preview = client.post(f"{BASE}/scales/preview", json={"categories": cats, "scores": [121, 100, 61]}, headers=h)
    assert preview.status_code == 200 and preview.json()["moved"] == 2  # 121 and 61 move; 100 stays Meeting
    payload = {"effective_from": "2999-01-01", "categories": cats, "rating_bands": bands, "rating_to_pi": pi}
    created = client.post(f"{BASE}/scales", json=payload, headers=h)
    assert created.status_code == 201 and created.json()["data"]["is_current"] is False  # future-dated
    assert client.post(f"{BASE}/scales", json={**payload, "effective_from": "2998-01-01"}, headers=h).status_code == 400
    listing = client.get(f"{BASE}/scales", headers=org.h["lo1"]).json()["items"]
    assert [s["effective_from"] for s in listing] == ["2999-01-01", "2000-01-01"]
    assert [s["is_current"] for s in listing] == [False, True]
    bad = client.post(f"{BASE}/scales/preview", json={"categories": cats[:2]}, headers=h)
    assert bad.status_code == 400
