"""Role JD / version endpoints: who may read and write, validation over HTTP, versioning, no N+1."""

import json

import pytest
from sqlalchemy import event

from app.models.performance import PerformanceAuditLog, PerformanceRole
from app.services import performance_roles as pr
from performance_tests.org import build_org

BASE = "/api/admin/performance"


@pytest.fixture()
def org(client, db):
    o = build_org(client, db)
    pr.seed_role_templates(db)
    return o


def role_id(db, key):
    return db.query(PerformanceRole).filter_by(key=key).one().id


def save_body(**over):
    body = {
        "jd": {"title": "Loan Officer", "responsibilities": [{"title": "Sales", "weight_pct": 100, "criteria": ["x"]}]},
        "kpis": [{"label": "Sales", "unit": "kes", "direction": "higher", "weight": 100, "source": "kpi_entry", "frequency": "monthly", "jd_section": "Sales"}],
        "signoff_chain": ["staff", "manager", "rm_gm", "hr"],
        "requires_ceo_signoff": False,
        "note": "tweak",
    }
    body.update(over)
    return body


def test_everyone_with_the_menu_can_read_how_a_role_is_measured(client, db, org):
    rid = role_id(db, "credit_risk_analyst")
    for who in ("lo1", "bm1", "gm", "ceo", "hr1", "admin1"):
        r = client.get(f"{BASE}/roles/{rid}/jd", headers=org.h[who])
        assert r.status_code == 200, who
    body = client.get(f"{BASE}/roles/{rid}/jd", headers=org.h["lo1"]).json()
    assert body["version"] == 1 and body["configured"] and body["weights"] == {"responsibilities_total": 100.0, "kpis_total": 100.0}
    assert [k["weight"] for k in body["kpis"]] == [30.0, 25.0, 15.0, 15.0, 15.0]
    assert body["effective_signoff_chain"] == ["staff", "manager", "rm_gm", "hr"]


@pytest.mark.parametrize("who", ["lo1", "bm1", "gm", "ceo"])
def test_only_admins_and_hr_can_save_a_jd(client, db, org, who):
    rid = role_id(db, "loan_officer")
    assert client.put(f"{BASE}/roles/{rid}/jd", json=save_body(), headers=org.h[who]).status_code == 403


def test_hr_can_save_a_jd(client, db, org):
    rid = role_id(db, "loan_officer")
    saved = client.put(f"{BASE}/roles/{rid}/jd", json=save_body(), headers=org.h["hr1"])
    assert saved.status_code == 200, saved.text


def test_jd_options_lists_the_allowed_values(client, org):
    body = client.get(f"{BASE}/jd-options", headers=org.h["lo1"]).json()
    assert "kes" in body["kpi_units"] and body["signoff_steps"] == ["staff", "manager", "csm", "rm_gm", "hr"]
    assert len(body["default_competencies"]) == 6


def test_role_without_a_saved_jd_returns_a_blank_starting_point(client, db, org):
    h = org.h["admin1"]
    rid = client.post(f"{BASE}/roles", json={"name": "Fresh Role"}, headers=h).json()["data"]["id"]
    body = client.get(f"{BASE}/roles/{rid}/jd", headers=h).json()
    assert body["version"] == 0 and body["jd"]["title"] == "Fresh Role" and body["kpis"] == [] and not body["configured"]
    assert len(body["jd"]["competencies"]) == 6
    assert client.get(f"{BASE}/roles/{rid}/versions", headers=h).json()["items"] == []


def test_save_then_read_back_and_unknown_ids(client, db, org):
    h = org.h["admin1"]
    rid = role_id(db, "loan_officer")
    saved = client.put(f"{BASE}/roles/{rid}/jd", json=save_body(), headers=h)
    assert saved.status_code == 200, saved.text
    assert saved.json()["created_new_version"] is False and saved.json()["version"] == 1  # unused -> in place
    assert [k["key"] for k in client.get(f"{BASE}/roles/{rid}/jd", headers=h).json()["kpis"]] == ["sales"]
    assert client.put(f"{BASE}/roles/nope/jd", json=save_body(), headers=h).status_code == 404
    assert client.get(f"{BASE}/roles/nope/jd", headers=h).status_code == 404
    assert client.get(f"{BASE}/roles/{rid}/versions/99", headers=h).status_code == 404


def test_validation_errors_come_back_as_400_with_every_problem(client, db, org):
    rid = role_id(db, "loan_officer")
    body = save_body(signoff_chain=["staff"])
    body["kpis"][0]["weight"] = 60
    r = client.put(f"{BASE}/roles/{rid}/jd", json=body, headers=org.h["admin1"])
    assert r.status_code == 400
    assert "must total 100" in r.json()["detail"] and "sign-off chain" in r.json()["detail"]


def test_a_locked_version_makes_the_next_save_a_new_version_and_history_is_listed(client, db, org):
    h = org.h["admin1"]
    rid = role_id(db, "loan_officer")
    pr.lock_version(db, pr.latest_version(db, rid).id)
    r = client.put(f"{BASE}/roles/{rid}/jd", json=save_body(), headers=h)
    assert r.json()["created_new_version"] is True and r.json()["version"] == 2
    versions = client.get(f"{BASE}/roles/{rid}/versions", headers=h).json()["items"]
    assert [(v["version"], v["is_latest"], v["locked"]) for v in versions] == [(2, True, False), (1, False, True)]
    v1 = client.get(f"{BASE}/roles/{rid}/versions/1", headers=h).json()
    assert v1["is_latest"] is False and v1["locked"] is True and len(v1["kpis"]) == 5  # the old definition is intact


def test_audit_records_the_version_but_never_the_jd_text(client, db, org):
    rid = role_id(db, "loan_officer")
    body = save_body()
    body["jd"]["purpose"] = "CONFIDENTIAL-PURPOSE-TEXT"
    assert client.put(f"{BASE}/roles/{rid}/jd", json=body, headers=org.h["admin1"]).status_code == 200
    entries = db.query(PerformanceAuditLog).filter_by(action="role_version_saved").all()
    assert entries and entries[-1].details == {"version": 1, "created_new": False}
    assert "CONFIDENTIAL" not in json.dumps([e.details for e in db.query(PerformanceAuditLog)])


def test_role_list_reports_version_and_configured_state(client, db, org):
    items = {r["key"]: r for r in client.get(f"{BASE}/roles", headers=org.h["lo1"]).json()["items"]}
    assert items["loan_officer"]["current_version"] == 1 and items["loan_officer"]["configured"] and items["loan_officer"]["kpi_count"] == 5
    assert items["hr_officer"]["current_version"] == 1 and not items["hr_officer"]["configured"] and items["hr_officer"]["kpi_count"] == 0


def test_role_list_query_count_does_not_grow_with_the_number_of_roles(client, db, session_factory, org):
    engine = session_factory.kw["bind"]
    counter = {"n": 0}

    def count(*_a, **_k):
        counter["n"] += 1

    def run():
        counter["n"] = 0
        assert client.get(f"{BASE}/roles", headers=org.h["lo1"]).status_code == 200
        return counter["n"]

    event.listen(engine, "before_cursor_execute", count)
    try:
        few = run()
        for i in range(40):
            db.add(PerformanceRole(key=f"extra_{i}", name=f"Extra {i}"))
        db.commit()
        many = run()
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert many == few  # batched: no N+1
