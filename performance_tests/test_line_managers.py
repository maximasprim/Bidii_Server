"""Line managers: a permission can be switched on for managers, for THEIR OWN TEAM only. Nothing is on by default."""

from datetime import date

from app.models.performance import KpiEntry, PerformanceRole, StaffProfile, StaffUnitCoverage
from performance_tests.org import build_world

BASE = "/api/admin/performance"
TEAM_ACTIONS = ("targets.set", "targets.decide", "entries.reopen", "org.coverage", "staff.manage")


def grant(client, w, cap, allowed=True, who="admin1"):
    return client.put(f"{BASE}/permissions", json={"role": "line_manager", "capability": cap, "allowed": allowed}, headers=w.h[who])


def set_target(client, w, who, profile, value=600000):
    body = {"kpi_key": "sales", "effective_from": "2026-01-01", "value": value, "reason": "Level DSE1"}
    return client.put(f"{BASE}/staff/{profile.id}/targets", json=body, headers=w.h[who])


def bulk(client, w, who, **body):
    base = {"mode": "level_defaults", "effective_from": "2026-01-01", "reason": "Annual level defaults", "dry_run": True, "role_id": w.role.id}
    return client.post(f"{BASE}/targets/bulk", json={**base, **body}, headers=w.h[who])


def verified_entry(db, profile, month=1):
    e = KpiEntry(profile_id=profile.id, kpi_key="sales", month=date(2026, month, 1), value=100, status="verified", source="manual", entered_by_username="x")
    db.add(e)
    db.commit()
    return e


def test_nothing_is_on_for_line_managers_until_it_is_ticked(client, db):
    w = build_world(client, db)
    assert set_target(client, w, "bm1", w.p.lo1).status_code == 403
    d = client.get(f"{BASE}/permissions", headers=w.h["admin1"]).json()
    assert d["roles"][0] == "line_manager"
    for cap in d["capabilities"]:
        cell = cap["cells"]["line_manager"]
        assert cell["allowed"] is False and cell["default"] is False
        assert cell["locked"] is (cap["key"] not in TEAM_ACTIONS)  # only the team-sized actions can be given to line managers
    assert client.get(f"{BASE}/permissions/me", headers=w.h["bm1"]).json()["team_only"] == []


def test_organisation_wide_actions_cannot_be_limited_to_a_team(client, db):
    w = build_world(client, db)
    for cap in ("levels.edit", "settings.edit", "retention.anonymise", "audit.view", "imports.manage"):
        assert grant(client, w, cap).status_code == 422
    assert grant(client, w, "targets.set").status_code == 200


def test_set_targets_for_my_team_only(client, db):
    w = build_world(client, db)
    assert grant(client, w, "targets.set").status_code == 200
    assert set_target(client, w, "bm1", w.p.lo1).status_code == 200  # their own team
    assert set_target(client, w, "bm1", w.p.lo2).status_code == 200
    assert set_target(client, w, "bm1", w.p.lo3).status_code == 404  # another branch: not even visible
    assert set_target(client, w, "bm1", w.p.bm1).status_code == 403  # never their own
    assert set_target(client, w, "lo1", w.p.lo2).status_code == 403  # not a line manager: nobody reports to them
    assert set_target(client, w, "hr1", w.p.lo3).status_code == 200  # HR is unaffected
    assert grant(client, w, "targets.set", False).status_code == 200
    assert set_target(client, w, "bm1", w.p.lo1).status_code == 403  # taken away: at once


def test_bulk_targets_only_reach_my_team(client, db):
    w = build_world(client, db)
    grant(client, w, "targets.set")
    mine = {i["profile_id"] for i in bulk(client, w, "bm1").json()["items"]}
    assert mine and mine <= {w.p.lo1.id, w.p.lo2.id}  # nobody from another branch, nobody above, not themselves
    outside = bulk(client, w, "bm1", profile_ids=[w.p.lo3.id, w.p.bm1.id]).json()["items"]
    assert outside == []
    everyone = {i["profile_id"] for i in bulk(client, w, "hr1").json()["items"]}
    assert w.p.lo3.id in everyone and mine < everyone  # HR still reaches the whole organisation


def test_decide_target_requests_for_my_team_only(client, db):
    w = build_world(client, db)
    grant(client, w, "targets.decide")
    ask = lambda p: client.post(f"{BASE}/staff/{p.id}/target-requests", json={"kpi_key": "sales", "effective_from": "2026-04-01", "value": 1, "reason": "admin asks"}, headers=w.h["admin1"])  # noqa: E731
    mine = ask(w.p.lo1).json()["data"]["id"]  # asked by an administrator, so the manager is a second pair of eyes
    theirs = ask(w.p.lo3).json()["data"]["id"]
    decide = lambda who, rid: client.post(f"{BASE}/target-requests/{rid}/decision", json={"approve": True, "note": "ok"}, headers=w.h[who])  # noqa: E731
    assert decide("bm1", theirs).status_code == 404  # another branch
    assert decide("bm1", mine).status_code == 200


def test_reopen_figures_for_my_team_only(client, db):
    w = build_world(client, db)
    grant(client, w, "entries.reopen")
    mine, other, own = verified_entry(db, w.p.lo1), verified_entry(db, w.p.lo3), verified_entry(db, w.p.bm1)
    reopen = lambda who, e: client.post(f"{BASE}/entries/{e.id}/reopen", json={"reason": "A figure was wrong"}, headers=w.h[who])  # noqa: E731
    assert reopen("bm1", other).status_code == 404
    assert reopen("bm1", own).status_code == 403  # never their own figures
    assert reopen("bm1", mine).status_code == 200


def test_coverage_for_my_team_only_and_only_units_i_manage(client, db):
    w = build_world(client, db)
    grant(client, w, "org.coverage")
    url = lambda p: f"{BASE}/staff/{p.id}/coverage"  # noqa: E731
    assert client.get(url(w.p.lo1), headers=w.h["bm1"]).status_code == 200
    assert client.get(url(w.p.lo3), headers=w.h["bm1"]).status_code == 404
    assert client.put(url(w.p.lo3), json={"unit_ids": []}, headers=w.h["bm1"]).status_code == 404
    assert client.put(url(w.p.bm1), json={"unit_ids": []}, headers=w.h["bm1"]).status_code == 403  # not their own
    assert client.put(url(w.p.lo1), json={"unit_ids": [w.beta.id]}, headers=w.h["bm1"]).status_code == 403  # a unit they do not manage
    assert client.put(url(w.p.lo1), json={"unit_ids": [w.alpha.id]}, headers=w.h["bm1"]).status_code == 200
    # coverage that HR set for a unit this manager does not manage is left exactly as it is
    assert client.put(url(w.p.lo1), json={"unit_ids": [w.beta.id]}, headers=w.h["hr1"]).status_code == 200
    assert client.put(url(w.p.lo1), json={"unit_ids": []}, headers=w.h["bm1"]).status_code == 200
    assert db.query(StaffUnitCoverage).filter_by(profile_id=w.p.lo1.id, unit_id=w.beta.id).count() == 1


def test_editing_staff_is_limited_to_my_team_and_to_safe_fields(client, db):
    w = build_world(client, db)
    grant(client, w, "staff.manage")
    patch = lambda who, p, **body: client.patch(f"{BASE}/staff/{p.id}", json=body, headers=w.h[who])  # noqa: E731
    assert patch("bm1", w.p.lo1, full_name="Officer One Renamed").status_code == 200
    assert patch("bm1", w.p.lo3, full_name="Not Mine").status_code == 404
    assert patch("bm1", w.p.bm1, full_name="Myself").status_code == 403
    other_role = db.query(PerformanceRole).filter(PerformanceRole.id != w.role.id).first()
    for field, value in (("status", "exited"), ("is_active", False), ("is_executive", True), ("admin_user_id", w.p.lo3.admin_user_id), ("performance_role_id", other_role.id)):
        assert patch("bm1", w.p.lo1, **{field: value}).status_code == 403, field
    # the edit form sends every field every time: unchanged values must not count as a change
    same = {"performance_role_id": w.role.id, "admin_user_id": w.p.lo1.admin_user_id, "status": w.p.lo1.status, "is_active": True, "unit_id": w.alpha.id, "manager_id": w.p.bm1.id}
    assert patch("bm1", w.p.lo1, full_name="Officer One Again", **same).status_code == 200
    assert patch("bm1", w.p.lo1, unit_id=w.beta.id).status_code == 403  # a unit they do not manage
    assert patch("bm1", w.p.lo1, manager_id=w.p.bm2.id).status_code == 403  # a manager outside their team
    assert patch("bm1", w.p.lo1, manager_id=w.p.lo2.id).status_code == 200  # someone inside it
    assert patch("hr1", w.p.lo1, status="probation").status_code == 200  # HR keeps the full set
    assert client.get(f"{BASE}/staff/linkable-users", headers=w.h["bm1"]).status_code == 403  # organisation-wide information


def test_adding_staff_is_limited_to_my_unit_or_my_team(client, db):
    w = build_world(client, db)
    grant(client, w, "staff.manage")
    add = lambda **body: client.post(f"{BASE}/staff", json={"full_name": "New Hire", "performance_role_id": w.role.id, **body}, headers=w.h["bm1"])  # noqa: E731
    assert add(unit_id=w.alpha.id).status_code == 201
    assert add(manager_id=w.p.bm1.id).status_code == 201
    assert add(unit_id=w.beta.id).status_code == 403
    assert add(manager_id=w.p.lo3.id).status_code == 403
    assert add().status_code == 403  # somewhere that is not theirs
    assert add(unit_id=w.alpha.id, admin_user_id=w.p.lo3.admin_user_id).status_code == 403
    assert add(unit_id=w.alpha.id, status="exited").status_code == 403
    assert add(unit_id=w.alpha.id, status="pip").status_code == 403
    assert add(unit_id=w.alpha.id, login={"username": "newhire", "password": "Str0ng!Pass1", "role": "loan_officer"}).status_code == 403
    assert db.query(StaffProfile).filter_by(full_name="New Hire").count() == 2


def test_my_permissions_show_what_i_hold_as_a_manager(client, db):
    w = build_world(client, db)
    grant(client, w, "targets.set")
    grant(client, w, "org.coverage")
    me = client.get(f"{BASE}/permissions/me", headers=w.h["bm1"]).json()
    assert me["team_only"] == ["org.coverage", "targets.set"] and set(me["team_only"]) <= set(me["capabilities"])
    assert client.get(f"{BASE}/permissions/me", headers=w.h["lo1"]).json()["team_only"] == []  # not a manager
    assert client.get(f"{BASE}/permissions/me", headers=w.h["hr1"]).json()["team_only"] == []  # HR already works organisation-wide


def test_a_role_tick_wins_over_the_team_limit_and_hr_cannot_be_a_line_manager(client, db):
    w = build_world(client, db)
    grant(client, w, "targets.set")
    assert client.put(f"{BASE}/permissions", json={"role": "loan_officer", "capability": "targets.set", "allowed": True}, headers=w.h["admin1"]).status_code == 200
    assert set_target(client, w, "bm1", w.p.lo3).status_code == 200  # the whole organisation, as the role is allowed
    client.post(f"{BASE}/permissions/reset", json={}, headers=w.h["admin1"])
    grant(client, w, "targets.set")
    client.put(f"{BASE}/permissions", json={"role": "hr", "capability": "targets.set", "allowed": False}, headers=w.h["admin1"])
    assert set_target(client, w, "hr1", w.p.lo1).status_code == 403  # an administrator turned it off for HR; HR is not a "line manager"
    assert set_target(client, w, "admin1", w.p.lo3).status_code == 200


def test_a_manager_of_managers_can_act_for_everyone_beneath_them(client, db):
    w = build_world(client, db)
    grant(client, w, "targets.set")
    assert set_target(client, w, "gm", w.p.bm1).status_code == 200  # a manager they manage
    assert set_target(client, w, "gm", w.p.lo3).status_code == 200  # and the people below that manager
    assert set_target(client, w, "gm", w.p.gm).status_code == 403  # never themselves
    assert set_target(client, w, "ceo", w.p.lo1).status_code == 403  # the executive view never changes anything
