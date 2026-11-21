"""Phase 6d - goal cascading: gaps at every level, proposals that add up exactly, scope, and rights."""

from datetime import date
from decimal import Decimal as D

import pytest

import app.routers.admin_performance_cascade as cr
from app.models.performance import CascadeTarget, PerformanceAuditLog, PerformanceTarget
from app.services import performance_cascade as pc
from app.services.performance_targets import set_target
from performance_tests.org import build_world

C = "/api/admin/performance/cascade"
TODAY = date(2026, 11, 18)
M = "2026-11-01"


@pytest.fixture()
def w(client, db, monkeypatch):
    monkeypatch.setattr(cr, "today_nairobi", lambda: TODAY)
    w = build_world(client, db)
    for p, v in ((w.p.lo1, 100), (w.p.lo2, 200), (w.p.bm1, 100), (w.p.lo3, 300), (w.p.bm2, 100)):
        set_target(db, p, "sales", date(2026, 1, 1), v * 1000, "seed", "hr1")
    db.commit()
    return w


def put(client, w, level, value, entity=None, who="hr1", **kw):
    body = {"level": level, "kpi_key": "sales", "month": M, "value": value, "reason": "FY plan", **kw}
    if entity:
        body["entity_id"] = entity
    return client.put(C + "/targets", json=body, headers=w.h[who])


def tree(client, w, who="hr1"):
    return client.get(C + "/tree", params={"kpi": "sales", "month": M}, headers=w.h[who])


@pytest.mark.parametrize("total,weights", [(1000, [1, 2, 1]), (1001, [1, 1, 1]), (7, [0, 0, 0]), (999999, [3, 5, 7, 11]), (10, [1])])
def test_split_adds_up_exactly(total, weights):
    parts = pc.split(D(total), [D(x) for x in weights])
    assert sum(parts) == total and all(p >= 0 and p == int(p) for p in parts)
    if sum(weights) > 0 and len(weights) > 1:
        assert max(abs(float(p) - total * wt / sum(weights)) for p, wt in zip(parts, weights)) < 1


def test_kpis_and_tree_show_the_gap_at_every_level(client, w):
    assert "sales" in [k["key"] for k in client.get(C + "/kpis", headers=w.h["hr1"]).json()["items"]]
    assert put(client, w, "company", 1000000).json() == {"changed": True}
    assert put(client, w, "company", 1000000).json() == {"changed": False}  # same value: nothing written
    put(client, w, "region", 600000, w.north.id)
    put(client, w, "region", 300000, w.south.id)
    put(client, w, "unit", 600000, w.alpha.id)
    put(client, w, "unit", 400000, w.beta.id)
    t = tree(client, w).json()
    assert t["company"] == {"target": 1000000, "regions_total": 900000, "gap": 100000, "state": "unallocated"}
    north = next(r for r in t["regions"] if r["name"] == "North")
    south = next(r for r in t["regions"] if r["name"] == "South")
    assert north["gap"] == 0 and north["state"] == "balanced" and south["gap"] == -100000 and south["state"] == "over"
    alpha = north["units"][0]
    assert alpha["target"] == 600000 and alpha["individuals_total"] == 400000 and alpha["gap"] == 200000 and alpha["state"] == "unallocated"
    assert {p["name"]: p["target"] for p in alpha["people"]} == {"Branch Manager One": 100000, "Officer One": 100000, "Officer Two": 200000}
    # a change at the top shows up straight away, nothing else was touched
    put(client, w, "company", 1200000)
    assert tree(client, w).json()["company"]["gap"] == 300000


def test_proposal_and_apply_write_ordinary_targets(client, db, w):
    put(client, w, "unit", 600000, w.alpha.id)
    p = client.get(C + "/proposal", params={"level": "unit", "entity_id": w.alpha.id, "kpi": "sales", "month": M}, headers=w.h["hr1"]).json()
    assert {r["name"]: r["proposed"] for r in p["rows"]} == {"Branch Manager One": 150000, "Officer One": 150000, "Officer Two": 300000} and p["child_level"] == "person"
    before = db.query(PerformanceTarget).count()
    body = {"level": "unit", "entity_id": w.alpha.id, "kpi_key": "sales", "month": M}
    assert client.post(C + "/apply", json={**body, "confirm": False}, headers=w.h["hr1"]).status_code == 422
    assert db.query(PerformanceTarget).count() == before
    r = client.post(C + "/apply", json={**body, "confirm": True}, headers=w.h["hr1"])
    assert r.json() == {"changed": 3, "children": 3}
    rows = db.query(PerformanceTarget).filter_by(source="cascade").all()
    assert len(rows) == 3 and all("Cascade from" in x.reason and x.effective_from == date(2026, 11, 1) for x in rows)
    assert tree(client, w).json()["regions"][0]["units"][0]["state"] == "balanced"
    assert client.post(C + "/apply", json={**body, "confirm": True}, headers=w.h["hr1"]).json()["changed"] == 0  # idempotent
    # January's targets are untouched (effective-dated, append-only)
    from app.services.performance_targets import in_force_map

    assert Decimal_eq(in_force_map(db, [w.p.lo1.id], date(2026, 6, 1), ["sales"])[(w.p.lo1.id, "sales")].value, 100000)
    assert db.query(PerformanceAuditLog).filter_by(action="cascade.apply").count() == 2


def Decimal_eq(a, b):
    return D(a) == D(b)


def test_region_and_company_proposals_with_equal_start(client, db, w):
    put(client, w, "company", 1000000)
    p = client.get(C + "/proposal", params={"level": "company", "kpi": "sales", "month": M}, headers=w.h["hr1"]).json()
    assert [r["proposed"] for r in p["rows"]] == [500000, 500000] and p["child_level"] == "region"
    assert client.post(C + "/apply", json={"level": "company", "kpi_key": "sales", "month": M, "confirm": True}, headers=w.h["hr1"]).json()["changed"] == 2
    assert db.query(CascadeTarget).filter_by(level="region", source="cascade").count() == 2
    r = client.get(C + "/proposal", params={"level": "region", "entity_id": w.north.id, "kpi": "sales", "month": M}, headers=w.h["hr1"])
    assert r.status_code == 200 and r.json()["rows"][0]["name"] == "Alpha"


def test_errors_are_clear(client, w):
    g = lambda **q: client.get(C + "/proposal", params={"kpi": "sales", "month": M, **q}, headers=w.h["hr1"])  # noqa: E731
    assert g(level="unit", entity_id=w.alpha.id).status_code == 422  # no unit target yet
    assert g(level="unit", entity_id="nope").status_code == 422
    assert g(level="unit", entity_id=w.alpha.id, kpi="bogus").status_code == 422
    assert put(client, w, "company", -5).status_code == 422
    assert put(client, w, "company", 100, reason="x").status_code == 422
    assert put(client, w, "region", 100, "nope").status_code == 422
    assert put(client, w, "company", 100, extra=1).status_code == 422
    assert client.get(C + "/tree", params={"kpi": "bogus"}, headers=w.h["hr1"]).status_code == 422


def test_scope_and_rights(client, db, w):
    put(client, w, "company", 1000000)
    put(client, w, "region", 600000, w.north.id)
    put(client, w, "unit", 600000, w.alpha.id)
    put(client, w, "unit", 400000, w.beta.id)
    bm1 = tree(client, w, "bm1").json()
    assert bm1["company"] is None and [r["name"] for r in bm1["regions"]] == ["North"] and [u["name"] for u in bm1["regions"][0]["units"]] == ["Alpha"]
    gm = tree(client, w, "gm").json()
    assert gm["company"] is None and [r["name"] for r in gm["regions"]] == ["North"]
    bm2 = tree(client, w, "bm2").json()
    assert [u["name"] for r in bm2["regions"] for u in r["units"]] == ["Beta"] and "Officer One" not in str(bm2)
    assert tree(client, w, "ceo").json()["company"]["target"] == 1000000  # sees everything
    assert tree(client, w, "lo1").status_code == 404
    for who in ("bm1", "gm", "ceo", "lo1"):
        assert put(client, w, "company", 5, who=who).status_code == 403, who
        assert client.post(C + "/apply", json={"level": "company", "kpi_key": "sales", "month": M, "confirm": True}, headers=w.h[who]).status_code == 403
        assert client.get(C + "/proposal", params={"level": "company", "kpi": "sales"}, headers=w.h[who]).status_code == 403
    assert put(client, w, "company", 5, who="admin1").status_code == 200


def test_needs_login(client):
    for m, p in (("get", "/kpis"), ("get", "/tree"), ("put", "/targets"), ("get", "/proposal"), ("post", "/apply")):
        assert getattr(client, m)(C + p).status_code in (401, 403), p
