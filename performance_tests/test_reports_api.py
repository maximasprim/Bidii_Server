"""Phase 3 - report endpoints: scope, IDOR, restricted info, exports, snapshots."""

import io
from datetime import date

import pytest
from openpyxl import load_workbook

from app.models.performance import KpiEntry, PerformanceAction, ScoreSnapshot
from app.services.performance_targets import set_target
from app.services.performance_time import today_nairobi
from performance_tests.org import build_world

BASE = "/api/admin/performance/reports"
YEAR = today_nairobi().year - 1


@pytest.fixture()
def world(client, db):
    w = build_world(client, db)
    for p, val in ((w.p.lo1, 120), (w.p.lo2, 60), (w.p.lo3, 100)):
        set_target(db, p, "sales", date(YEAR, 1, 1), 100, "seed target", "hr1")
        for m in (1, 2, 3):
            db.add(KpiEntry(profile_id=p.id, kpi_key="sales", month=date(YEAR, m, 1), value=val, status="verified", source="manual", entered_by_username="x"))
    db.commit()
    return w


def get(client, w, who, path, **params):
    params.setdefault("period", f"{YEAR}-Q1")
    return client.get(BASE + path, params=params, headers=w.h[who])


def test_my_report_and_unlinked_user(client, world):
    r = get(client, world, "lo1", "/me")
    assert r.status_code == 200
    body = r.json()
    assert body["person"]["headline"]["pct"] == 120 and body["person"]["category"]["key"]
    assert body["explanation"]
    assert get(client, world, "ceo", "/me").status_code in (200, 404)


def test_person_scope_and_idor(client, world):
    p = world.p
    assert get(client, world, "bm1", f"/person/{p.lo1.id}").status_code == 200
    assert get(client, world, "lo1", f"/person/{p.lo2.id}").status_code == 404
    assert get(client, world, "lo1", f"/person/{p.lo3.id}").status_code == 404
    assert get(client, world, "bm1", f"/person/{p.lo3.id}").status_code == 404
    assert get(client, world, "lo1", "/person/does-not-exist").status_code == get(client, world, "lo1", f"/person/{p.lo3.id}").status_code


def test_team_unit_region_org_scope(client, world):
    p = world
    assert get(client, p, "bm1", "/team").status_code == 200
    assert get(client, p, "lo1", "/team").json()["people"] == []
    assert get(client, p, "bm1", f"/unit/{p.alpha.id}").status_code == 200
    assert get(client, p, "bm1", f"/unit/{p.beta.id}").status_code == 404
    assert get(client, p, "lo1", f"/unit/{p.beta.id}").status_code == 404
    assert get(client, p, "gm", f"/region/{p.north.id}").status_code == 200
    assert get(client, p, "gm", f"/region/{p.south.id}").status_code == 404
    assert get(client, p, "bm1", f"/region/{p.north.id}").status_code == 404
    for who in ("lo1", "bm1", "gm"):
        assert get(client, p, who, "/organisation").status_code == 404
    for who in ("admin1", "hr1", "ceo"):
        assert get(client, p, who, "/organisation").status_code == 200


def test_org_report_numbers(client, world):
    d = get(client, world, "admin1", "/organisation").json()
    assert d["kes"]["actual"] == 3 * (120 + 60 + 100) and d["kes"]["target"] == 900
    assert len(d["by_month"]) >= 1 and d["summary"]["scored_people"] == 3


def test_unit_report_small_unit_caution_and_heatmap(client, world):
    d = get(client, world, "bm1", f"/unit/{world.alpha.id}").json()
    assert d["small_unit"] and d["small_unit_note"]
    assert {r["name"] for r in d["heatmap"]["rows"]} >= {"Officer One", "Officer Two"}
    assert d["summary"]["achievement_pct"] == 90  # 540/600


def test_pip_status_hidden_from_non_audience(client, db, world):
    world.p.lo1.status = "pip"
    db.commit()
    team = get(client, world, "gm", f"/unit/{world.alpha.id}").json()
    row = next(r for r in team["people"] if r["name"] == "Officer One")
    assert row["status"] == "active" and not any("improvement plan" in a for a in row["attention"])
    mine = get(client, world, "lo1", "/me").json()
    assert mine["person"]["status"] == "pip"
    boss = get(client, world, "bm1", "/team").json()
    assert next(r for r in boss["people"] if r["name"] == "Officer One")["status"] == "pip"


def test_peer_rank_hidden_from_self_unless_enabled(client, world):
    assert get(client, world, "lo1", "/me").json()["person"]["peer_rank"] is None


def test_compare_and_bad_periods(client, world):
    r = get(client, world, "admin1", "/compare", a=f"{YEAR}-01", b=f"{YEAR}-Q1")
    assert r.status_code == 200 and r.json()["a"]["achievement_pct"] is not None
    assert get(client, world, "admin1", "/organisation", period="nonsense").status_code == 422
    assert get(client, world, "admin1", "/organisation", period="2020-01..2026-12").status_code == 422
    assert get(client, world, "lo1", "/compare", a=f"{YEAR}-01", b=f"{YEAR}-02", profile_id=world.p.lo2.id).status_code == 404


def test_export_xlsx_pdf_and_formula_injection(client, db, world):
    world.p.lo1.full_name = "=HYPERLINK(\"http://evil\")"
    db.commit()
    r = get(client, world, "admin1", "/export", kind="unit", id=world.alpha.id, format="xlsx")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    ws = load_workbook(io.BytesIO(r.content)).active
    texts = [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]
    assert not any(t.startswith("=") for t in texts) and any(t.startswith("'=") for t in texts)
    pdf = get(client, world, "admin1", "/export", kind="org", format="pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    assert get(client, world, "lo1", "/export", kind="unit", id=world.beta.id).status_code == 404
    assert get(client, world, "lo1", "/export", kind="org").status_code == 404


def test_snapshots_recompute_is_admin_hr_and_idempotent(client, db, world):
    assert client.post(BASE + "/snapshots/recompute", json={"period": f"{YEAR}-Q1"}, headers=world.h["bm1"]).status_code == 403
    for _ in range(2):
        assert client.post(BASE + "/snapshots/recompute", json={"period": f"{YEAR}-Q1"}, headers=world.h["hr1"]).status_code == 200
    assert db.query(ScoreSnapshot).filter_by(profile_id=world.p.lo1.id).count() == 1
    assert get(client, world, "lo1", "/snapshots").json()["snapshots"][0]["score"] is not None
    assert get(client, world, "lo1", "/snapshots", profile_id=world.p.lo2.id).status_code == 404
    assert client.post(BASE + "/snapshots/recompute", json={"period": "x", "extra": 1}, headers=world.h["hr1"]).status_code == 422


def test_viewing_someone_else_is_audited(client, db, world):
    from app.models.performance import PerformanceAuditLog
    get(client, world, "bm1", f"/person/{world.p.lo1.id}")
    assert db.query(PerformanceAuditLog).filter_by(action="report.view").count() == 1
    get(client, world, "lo1", "/me")
    assert db.query(PerformanceAuditLog).filter_by(action="report.view").count() == 1
