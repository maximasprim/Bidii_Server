"""Phase 6e - the monthly one-page board pack."""

import io
from datetime import date

import pytest
from pypdf import PdfReader

import app.routers.admin_performance_pack as pk
from app.models.performance import BoardPack, KpiEntry
from app.services import performance_storage as storage
from app.services.performance_targets import set_target
from performance_tests.org import build_world
from performance_tests.test_files import FakeBucket

P = "/api/admin/performance/pack"
TODAY = date(2026, 11, 3)


@pytest.fixture()
def w(client, db, monkeypatch):
    b = FakeBucket()
    monkeypatch.setattr(storage, "_client", lambda: b)
    monkeypatch.setattr(pk, "today_nairobi", lambda: TODAY)
    w = build_world(client, db)
    w.bucket = b
    for p, v in ((w.p.lo1, 120), (w.p.lo2, 80), (w.p.lo3, 60)):
        set_target(db, p, "sales", date(2026, 1, 1), 100, "seed", "hr1")
        for m in range(1, 11):
            db.add(KpiEntry(profile_id=p.id, kpi_key="sales", month=date(2026, m, 1), value=v, status="verified", source="manual", entered_by_username="x"))
    db.commit()
    return w


def test_only_admin_hr_executive(client, w):
    for who in ("bm1", "lo1", "gm"):
        assert client.get(P + "/preview", headers=w.h[who]).status_code in (403, 404)
        assert client.post(P + "/generate", json={}, headers=w.h[who]).status_code in (403, 404)
    assert client.get(P + "/preview").status_code in (401, 403)
    assert client.get(P + "/preview", headers=w.h["hr1"]).status_code == 200
    assert client.post(P + "/generate", json={}, headers=w.h["ceo"]).status_code in (403, 404)


def test_generate_is_one_page_with_no_names_and_replaces_not_duplicates(client, db, w):
    r = client.post(P + "/generate", json={"month": "2026-10"}, headers=w.h["hr1"])
    assert r.status_code == 200 and r.json()["month"] == "2026-10"
    pdf = client.get(f"{P}/download/{r.json()['id']}", headers=w.h["ceo"])
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    rd = PdfReader(io.BytesIO(pdf.content))
    assert len(rd.pages) == 1
    text = rd.pages[0].extract_text()
    assert "October 2026" in text and "Officer One" not in text and "Officer Two" not in text
    again = client.post(P + "/generate", json={"month": "2026-10"}, headers=w.h["admin1"])
    assert again.json()["id"] == r.json()["id"] and db.query(BoardPack).count() == 1 and len(w.bucket.files) == 1
    assert len(client.get(P + "/list", headers=w.h["hr1"]).json()) == 1


def test_validation(client, w):
    assert client.post(P + "/generate", json={"month": "2026-13"}, headers=w.h["hr1"]).status_code == 422
    assert client.post(P + "/generate", json={"month": "2027-01"}, headers=w.h["hr1"]).status_code == 422
    assert client.post(P + "/generate", json={"x": 1}, headers=w.h["hr1"]).status_code == 422
    assert client.get(P + "/download/nope", headers=w.h["hr1"]).status_code == 404


def test_cron_needs_secret_and_is_idempotent(client, db, w, monkeypatch):
    class S:
        performance_cron_token = "tok"

    url = "/api/internal/performance/monthly-pack"
    monkeypatch.setattr(pk, "get_settings", lambda: S)
    assert client.post(url).status_code == 401 and client.post(url, headers={"X-Cron-Secret": "bad"}).status_code == 401
    r = client.post(url, headers={"X-Cron-Secret": "tok"}).json()
    assert r["generated"] is True and r["month"] == "2026-10" and r["notified"] >= 2
    assert client.post(url, headers={"X-Cron-Secret": "tok"}).json()["generated"] is False
    assert db.query(BoardPack).count() == 1
    S.performance_cron_token = None
    assert client.post(url, headers={"X-Cron-Secret": "tok"}).status_code == 404
