"""Org tree (scope + PIP masking), health checks, first-time setup from the real branch list, level seeds, column migration."""

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.branch import Branch
from app.models.performance import PerformanceRole, PerformanceUnit, Region, StaffProfile, UnitRegionLink
from app.services import performance_org as org
from performance_tests.org import build_world

BASE = "/api/admin/performance"

# The 11 current branches (the public branches table in your repo / the HR induction deck).
REAL_BRANCHES = ["Head Office & Ngong Road", "Nairobi CBD", "Kitengela", "Kawangware", "Embakasi", "Kajiado", "Nakuru", "Machakos", "Ruiru", "Thika", "Karatina"]


@pytest.fixture()
def w(client, db):
    return build_world(client, db)


def add_branches(db, names=REAL_BRANCHES):
    rows = [Branch(name=n, address="x", hours="x", phone="0", lat=0.0, lng=0.0) for n in names]
    db.add_all(rows)
    db.commit()
    return rows


def tree(client, w, who, **params):
    return client.get(f"{BASE}/org-tree", params=params, headers=w.h[who]).json()


def flat(nodes):
    for n in nodes:
        yield n
        yield from flat(n["children"])


# ---- tree --------------------------------------------------------------------------------------------------------


def test_tree_shows_each_person_only_their_own_part(client, w):
    p = w.p
    assert [n["id"] for n in tree(client, w, "lo1")["roots"]] == [p.lo1.id]
    bm = tree(client, w, "bm1")["roots"]
    assert [n["id"] for n in bm] == [p.bm1.id] and {c["id"] for c in bm[0]["children"]} == {p.lo1.id, p.lo2.id}
    assert tree(client, w, "bm1")["total"] == 3
    gm = tree(client, w, "gm")["roots"]
    assert [n["id"] for n in gm] == [p.gm.id] and {c["id"] for c in gm[0]["children"]} == {p.bm1.id, p.bm2.id}
    assert gm[0]["direct_reports"] == 2 and gm[0]["total_reports"] == 5
    for who in ("hr1", "admin1", "ceo"):
        t = tree(client, w, who)
        assert {n["id"] for n in t["roots"]} == {p.ceo.id, p.gm.id} and t["total"] == 7, who


def test_tree_nodes_carry_role_level_and_unit(client, w):
    node = next(n for n in flat(tree(client, w, "hr1")["roots"]) if n["full_name"] == "Officer One")
    assert (node["role_name"], node["level_label"], node["unit_name"]) == ("Loan Officer", "DSE1", "Alpha")


def test_a_pip_never_shows_in_the_tree_to_people_not_entitled_to_it(client, db, w):
    client.post(f"{BASE}/staff/{w.p.lo1.id}/actions", json={"action_type": "pip", "started_on": "2026-05-01"}, headers=w.h["hr1"])
    status = lambda who: next(n for n in flat(tree(client, w, who)["roots"]) if n["id"] == w.p.lo1.id)["status"]  # noqa: E731
    assert status("hr1") == "pip" and status("admin1") == "pip" and status("bm1") == "pip" and status("lo1") == "pip"
    assert status("gm") == "active" and status("ceo") == "active"


def test_inactive_people_are_hidden_unless_asked_for(client, db, w):
    w.p.lo2.is_active = False
    db.commit()
    assert "Officer Two" not in {n["full_name"] for n in flat(tree(client, w, "hr1")["roots"])}
    assert "Officer Two" in {n["full_name"] for n in flat(tree(client, w, "hr1", include_inactive="true")["roots"])}


def test_a_reporting_loop_is_shown_not_hidden(client, db, w):
    w.p.bm1.manager_id = w.p.lo1.id  # lo1 already reports to bm1: a loop that writes would have refused
    db.commit()
    names = {n["full_name"] for n in flat(tree(client, w, "hr1")["roots"])}
    assert {"Branch Manager One", "Officer One"} <= names


# ---- health -----------------------------------------------------------------------------------------------------------


def codes(client, w):
    return [i["code"] for i in client.get(f"{BASE}/org-health", headers=w.h["hr1"]).json()["issues"]]


@pytest.mark.parametrize("who", ["lo1", "bm1", "gm", "ceo"])
def test_health_is_admin_or_hr_only(client, w, who):
    assert client.get(f"{BASE}/org-health", headers=w.h[who]).status_code == 403


def test_health_flags_structure_problems(client, db, w):
    found = codes(client, w)
    assert "region_no_manager" in found  # South has no regional manager
    assert "no_level" in found  # bm/gm/ceo are Loan Officers without a level, and that role has levels
    assert "no_manager" in found  # the GM (not executive, role isn't ceo) has none
    db.add(StaffProfile(full_name="Orphan", performance_role_id=w.role.id, manager_id=None))
    w.p.lo2.manager_id = None
    db.commit()
    issues = client.get(f"{BASE}/org-health", headers=w.h["hr1"]).json()
    assert any(i["code"] == "no_unit" and "Orphan" in i["message"] for i in issues["issues"])
    assert issues["counts"]["warning"] >= 2


def test_health_reports_loops_and_departed_managers_as_errors(client, db, w):
    w.p.bm1.manager_id = w.p.lo1.id
    w.p.bm2.is_active = False
    db.commit()
    body = client.get(f"{BASE}/org-health", headers=w.h["hr1"]).json()
    errs = {i["code"] for i in body["issues"] if i["severity"] == "error"}
    assert {"reporting_loop", "manager_inactive"} <= errs and body["counts"]["error"] >= 2


def test_health_notes_roles_with_no_kpi_set_and_the_ceo_needs_no_manager(client, db, w):
    ceo_role = db.query(PerformanceRole).filter_by(key="ceo").one()
    hr_role = db.query(PerformanceRole).filter_by(key="hr_officer").one()
    db.add_all([StaffProfile(full_name="The CEO", performance_role_id=ceo_role.id, is_executive=True),
                StaffProfile(full_name="HR Staff", performance_role_id=hr_role.id, manager_id=w.p.gm.id, unit_id=w.alpha.id)])
    db.commit()
    body = client.get(f"{BASE}/org-health", headers=w.h["hr1"]).json()["issues"]
    assert any(i["code"] == "role_unconfigured" and "HR Officer" in i["message"] for i in body)
    assert not any("The CEO" in i["message"] for i in body)


# ---- first-time setup from the real branch list ----------------------------------------------------------------------


@pytest.mark.parametrize("name,expected", [("CBD", "Nairobi CBD"), ("Ngong Road", "Head Office & Ngong Road"), ("Thika", "Thika"), ("Kasarani", None), ("Eldoret", None)])
def test_branch_matching(db, name, expected):
    got = org.match_branch(name, add_branches(db))
    assert (got.name if got else None) == expected


def test_proposal_matches_all_eleven_current_branches_in_4_4_3(client, db, w):
    add_branches(db)
    pre = client.get(f"{BASE}/org-setup/preview", headers=w.h["admin1"]).json()
    assert [(r["name"], len(r["branch_ids"])) for r in pre["regions"]] == [("N. RIFT", 4), ("N. EAST", 4), ("CENTRAL", 3)]
    assert pre["unmatched"] == [] and len(pre["branches"]) == 11
    assert all(b["proposed_region"] for b in pre["branches"])  # no current branch is left out
    assert {e["name"] for e in pre["extra_units"]} == {"Head Office", "Check-off"}


def test_preview_names_what_cannot_be_matched(client, db, w):
    add_branches(db, [n for n in REAL_BRANCHES if n != "Thika"])
    assert client.get(f"{BASE}/org-setup/preview", headers=w.h["admin1"]).json()["unmatched"] == ["Thika"]


@pytest.mark.parametrize("who", ["lo1", "bm1", "hr1", "ceo"])
def test_setup_is_admin_only(client, w, who):
    assert client.get(f"{BASE}/org-setup/preview", headers=w.h[who]).status_code == 403
    assert client.post(f"{BASE}/org-setup/apply", json={"regions": []}, headers=w.h[who]).status_code == 403


def apply(client, w, regions=None, **extra):
    regions = regions if regions is not None else client.get(f"{BASE}/org-setup/preview", headers=w.h["admin1"]).json()["regions"]
    return client.post(f"{BASE}/org-setup/apply", json={"regions": regions, **extra}, headers=w.h["admin1"])


def test_apply_builds_units_regions_and_links_and_is_idempotent(client, db, w):
    branches = add_branches(db)
    before = sorted((b.id, b.name) for b in db.query(Branch))
    first = apply(client, w).json()
    assert first["units_created"] == 11 + 2 and first["regions_created"] == 3 and first["links_created"] == 11 and first["conflicts"] == []
    assert sorted((b.id, b.name) for b in db.query(Branch)) == before  # the public branches table is only READ
    assert db.query(PerformanceUnit).filter(PerformanceUnit.branch_id.is_not(None)).count() == 11
    assert apply(client, w).json() == {"units_created": 0, "units_linked": 0, "regions_created": 0, "links_created": 0, "conflicts": []}
    ruiru = next(u for u in client.get(f"{BASE}/units", headers=w.h["lo1"]).json()["items"] if u["name"] == "Ruiru")
    assert ruiru["region_name"] == "CENTRAL" and len(branches) == 11


def test_an_existing_unit_with_a_branch_name_is_linked_not_duplicated(client, db, w):
    add_branches(db)
    db.add(PerformanceUnit(name="Kitengela", kind="branch"))
    db.commit()
    first = apply(client, w).json()
    assert first["units_linked"] == 1 and db.query(PerformanceUnit).filter_by(name="Kitengela").count() == 1


def test_a_unit_already_in_another_region_is_reported_not_moved(client, db, w):
    add_branches(db)
    south = db.query(Region).filter_by(name="South").one()
    unit = PerformanceUnit(name="Kitengela", kind="branch")
    db.add(unit)
    db.flush()
    db.add(UnitRegionLink(unit_id=unit.id, region_id=south.id, effective_from=__import__("datetime").date(2020, 1, 1)))
    db.commit()
    res = apply(client, w).json()
    assert any("Kitengela" in c for c in res["conflicts"])
    link = db.query(UnitRegionLink).filter_by(unit_id=unit.id, effective_to=None).one()
    assert link.region_id == south.id


def test_apply_rejects_bad_layouts(client, db, w):
    branches = add_branches(db)
    a, b = branches[0].id, branches[1].id
    twice = [{"name": "R1", "branch_ids": [a]}, {"name": "R2", "branch_ids": [a]}]
    assert apply(client, w, twice).status_code == 400
    assert apply(client, w, [{"name": "R1", "branch_ids": ["nope"]}]).status_code == 400
    assert apply(client, w, [{"name": "  ", "branch_ids": [b]}]).status_code == 400
    assert db.query(Region).filter_by(name="R1").count() == 0


def test_an_admin_can_edit_the_proposal_before_applying(client, db, w):
    branches = add_branches(db)
    custom = [{"name": "ONE REGION", "branch_ids": [b.id for b in branches]}]
    res = apply(client, w, custom).json()
    assert res["regions_created"] == 1 and res["links_created"] == 11


# ---- seeds from the deck ----------------------------------------------------------------------------------------------------


def test_org_structure_roles_are_seeded(w, db):
    keys = {r.key for r in db.query(PerformanceRole)}
    assert {"sales_team_leader", "regional_manager", "customer_service_manager", "general_manager", "ceo", "auto_champion"} <= keys


def test_auto_champion_shares_the_loan_officer_kpi_keys_so_level_targets_apply(db, w):
    from app.services.performance_roles import targetable_kpis

    lo = db.query(PerformanceRole).filter_by(key="loan_officer").one()
    ac = db.query(PerformanceRole).filter_by(key="auto_champion").one()
    assert set(targetable_kpis(db, lo.id)) == set(targetable_kpis(db, ac.id)) == {"sales", "new_customers", "par30", "retention"}


def test_level_seeding_is_idempotent_and_never_overwrites_an_edit(db, w):
    from app.models.performance import PerformanceLevel
    from app.services.performance_levels import seed_levels

    dse1 = db.query(PerformanceLevel).filter_by(key="dse1").one()
    dse1.label, dse1.default_targets = "DSE 1 (edited)", {"sales": 111}
    db.commit()
    assert seed_levels(db) == 0
    db.refresh(dse1)
    assert (dse1.label, dse1.default_targets) == ("DSE 1 (edited)", {"sales": 111})
    assert db.query(PerformanceLevel).count() == 11


def test_disciplinary_ladder_is_restricted_and_the_old_generic_warning_is_gone(client, db, w):
    from app.services.performance_config import RESTRICTED_ACTION_TYPES

    assert {"verbal_warning", "first_written_warning", "final_warning", "demotion", "dismissal", "pip"} <= RESTRICTED_ACTION_TYPES
    assert "talk" not in RESTRICTED_ACTION_TYPES
    for kind in ("verbal_warning", "first_written_warning", "final_warning"):
        r = client.post(f"{BASE}/staff/{w.p.lo2.id}/actions", json={"action_type": kind, "started_on": "2026-05-01"}, headers=w.h["bm1"])
        assert r.status_code == 201 and r.json()["data"]["restricted"] is True, kind
    seen = lambda who: len(client.get(f"{BASE}/staff/{w.p.lo2.id}/actions", headers=w.h[who]).json()["items"])  # noqa: E731
    assert seen("bm1") == 3 and seen("hr1") == 3 and seen("lo2") == 3
    assert seen("gm") == 0 and seen("ceo") == 0 and seen("admin1") == 3  # an administrator is entitled like HR
    assert client.post(f"{BASE}/staff/{w.p.lo2.id}/actions", json={"action_type": "warning", "started_on": "2026-05-01"}, headers=w.h["hr1"]).status_code == 422


# ---- additive column migration ------------------------------------------------------------------------------------------------


def _old_shape_db():
    """A database that ran Phase 1b: staff_profiles exists WITHOUT level_id."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE performance_levels (id VARCHAR(36) PRIMARY KEY)"))
        c.execute(text("CREATE TABLE staff_profiles (id VARCHAR(36) PRIMARY KEY, full_name VARCHAR(150))"))
        c.execute(text("INSERT INTO staff_profiles (id, full_name) VALUES ('p1', 'Existing Person')"))
    return engine


def test_level_column_is_added_to_an_existing_table_once_and_keeps_data():
    from app.services.performance_bootstrap import ensure_additive_columns

    engine = _old_shape_db()
    db = sessionmaker(bind=engine)()
    assert ensure_additive_columns(db) == ["staff_profiles.level_id"]
    assert ensure_additive_columns(db) == []  # idempotent
    assert "level_id" in {c["name"] for c in inspect(engine).get_columns("staff_profiles")}
    assert db.execute(text("SELECT full_name, level_id FROM staff_profiles")).one() == ("Existing Person", None)


def test_nothing_happens_when_the_table_does_not_exist_yet():
    from app.services.performance_bootstrap import ensure_additive_columns

    db = sessionmaker(bind=create_engine("sqlite://", poolclass=StaticPool))()
    assert ensure_additive_columns(db) == []


# ---- structure (flat facts for the chain-of-command views) -----------------------------------------------------------


def structure(client, w, who, **params):
    return client.get(f"{BASE}/org-structure", params=params, headers=w.h[who]).json()


def test_structure_gives_everyone_their_manager_unit_role_and_region(client, w):
    s = structure(client, w, "admin1")
    p = w.p
    by_id = {x["id"]: x for x in s["people"]}
    assert set(by_id) == {p.ceo.id, p.gm.id, p.bm1.id, p.bm2.id, p.lo1.id, p.lo2.id, p.lo3.id}
    lo1 = by_id[p.lo1.id]
    assert (lo1["manager_id"], lo1["unit_id"], lo1["role_key"], lo1["role_name"]) == (p.bm1.id, w.alpha.id, "loan_officer", "Loan Officer")
    assert by_id[p.ceo.id]["is_executive"] is True and by_id[p.ceo.id]["manager_id"] is None
    units = {u["name"]: u for u in s["units"]}
    assert (units["Alpha"]["region_id"], units["Alpha"]["manager_profile_id"], units["Alpha"]["kind"]) == (w.north.id, p.bm1.id, "branch")
    assert units["Beta"]["region_id"] == w.south.id
    regions = {r["name"]: r for r in s["regions"]}
    assert regions["North"]["manager_profile_id"] == p.gm.id and regions["South"]["manager_profile_id"] is None


def test_structure_shows_a_manager_only_their_own_part(client, w):
    p = w.p
    s = structure(client, w, "bm1")
    assert {x["id"] for x in s["people"]} == {p.bm1.id, p.lo1.id, p.lo2.id}
    assert {u["name"] for u in s["units"]} == {"Alpha"} and {r["name"] for r in s["regions"]} == {"North"}
    bm = next(x for x in s["people"] if x["id"] == p.bm1.id)
    assert bm["manager_id"] is None  # their own manager is outside what they may see
    region = s["regions"][0]
    assert region["manager_profile_id"] is None  # the regional manager is not visible to them either


def test_structure_hides_a_pip_like_the_tree_does(client, w):
    client.post(f"{BASE}/staff/{w.p.lo1.id}/actions", json={"action_type": "pip", "started_on": "2026-05-01"}, headers=w.h["hr1"])
    status = lambda who: next(x for x in structure(client, w, who)["people"] if x["id"] == w.p.lo1.id)["status"]  # noqa: E731
    assert status("hr1") == "pip" and status("bm1") == "pip" and status("gm") == "active"


def test_structure_leaves_inactive_people_out_unless_asked_for(client, db, w):
    w.p.lo3.is_active = False
    db.commit()
    ids = lambda **kw: {x["id"] for x in structure(client, w, "hr1", **kw)["people"]}  # noqa: E731
    assert w.p.lo3.id not in ids() and w.p.lo3.id in ids(include_inactive="true")


def test_preview_follows_the_regions_you_have_actually_set_up(client, db, w):
    """After branches are placed, the preview shows YOUR grouping (not the built-in starting proposal), incl. new regions and moves."""
    add_branches(db)
    apply(client, w)
    west = Region(name="WEST")
    db.add(west)
    db.flush()
    thika = db.query(PerformanceUnit).filter_by(name="Thika").one()
    link = db.query(UnitRegionLink).filter_by(unit_id=thika.id, effective_to=None).one()
    link.region_id = west.id
    db.commit()

    preview = client.get(f"{BASE}/org-setup/preview", headers=w.h["admin1"]).json()
    by_region = {r["name"]: {b["name"] for b in preview["branches"] if b["branch_id"] in r["branch_ids"]} for r in preview["regions"]}
    assert by_region["WEST"] == {"Thika"}
    assert "Thika" not in by_region["CENTRAL"] and "Ruiru" in by_region["CENTRAL"]
    assert preview["unmatched"] == []
    # applying exactly what it shows changes nothing and reports no conflict
    again = client.post(f"{BASE}/org-setup/apply", json={"regions": [{"name": r["name"], "branch_ids": r["branch_ids"]} for r in preview["regions"]]}, headers=w.h["admin1"]).json()
    assert again["links_created"] == 0 and again["conflicts"] == []
