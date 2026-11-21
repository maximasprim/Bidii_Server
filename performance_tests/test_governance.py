"""Phase 4b-1 - period locks, data-use notice, retention/anonymise, access log."""

from datetime import date, timedelta

import pytest

from app.models.performance import AppraisalDispute, KpiEntry, PerformanceAction, PerformanceAuditLog, PeriodLock
from app.services import performance_entries as pe
from app.services import performance_governance as gov
from performance_tests.org import build_world

G = "/api/admin/performance/governance"
TODAY = date(2026, 5, 4)


@pytest.fixture()
def w(client, db, monkeypatch):
    import app.routers.admin_performance_entries as em
    import app.routers.admin_performance_governance as gm

    monkeypatch.setattr(em, "today_nairobi", lambda: TODAY)
    monkeypatch.setattr(gm, "today_nairobi", lambda: TODAY)
    return build_world(client, db)


def test_lock_rules_and_enforcement(client, db, w):
    assert client.post(G + "/locks", json={"period": "2026-03"}, headers=w.h["bm1"]).status_code == 403
    assert client.post(G + "/locks", json={"period": "2026-05"}, headers=w.h["hr1"]).status_code == 422  # not finished
    assert client.post(G + "/locks", json={"period": "nonsense"}, headers=w.h["hr1"]).status_code == 422
    r = client.post(G + "/locks", json={"period": "2026-Q1"}, headers=w.h["hr1"])
    assert r.status_code == 200 and r.json()["locked"] == ["2026-01", "2026-02", "2026-03"]
    body = {"kpi_key": "sales", "month": "2026-03-01", "value": 100}
    for who in ("hr1", "admin1", "lo1"):
        r = client.put(f"/api/admin/performance/staff/{w.p.lo1.id}/entries", json=body, headers=w.h[who])
        assert r.status_code == 400 and "locked" in r.json()["detail"], who
    ok = client.put(f"/api/admin/performance/staff/{w.p.lo1.id}/entries", json={**body, "month": "2026-04-01"}, headers=w.h["lo1"])
    assert ok.status_code == 200
    # reopening needs a reason; then writes work again
    assert client.post(G + "/locks/reopen", json={"period": "2026-03", "reason": "x"}, headers=w.h["hr1"]).status_code == 422
    r = client.post(G + "/locks/reopen", json={"period": "2026-03", "reason": "Late correction approved by CFO"}, headers=w.h["hr1"])
    assert r.json()["reopened"] == ["2026-03"]
    assert client.put(f"/api/admin/performance/staff/{w.p.lo1.id}/entries", json=body, headers=w.h["hr1"]).status_code == 200  # HR may amend after the window
    items = {i["month"]: i for i in client.get(G + "/locks", headers=w.h["hr1"]).json()["items"]}
    assert items["2026-03"]["status"] == "open" and items["2026-03"]["reopen_reason"] and items["2026-02"]["status"] == "locked"
    actions = {a for (a,) in db.query(PerformanceAuditLog.action)}
    assert {"period.lock", "period.reopen"} <= actions


def test_verify_and_reopen_blocked_in_locked_month(client, db, w):
    e = KpiEntry(profile_id=w.p.lo1.id, kpi_key="sales", month=date(2026, 3, 1), value=100, status="submitted", source="manual", entered_by_username="lo1")
    db.add(e)
    db.commit()
    gov.lock(db, "2026-03", "hr1", TODAY)
    with pytest.raises(pe.EntryError):
        pe.decide(db, e, True, "bm1", None)
    e.status = "verified"
    db.commit()
    with pytest.raises(pe.EntryError):
        pe.reopen(db, e, "mistake", "hr1")
    with pytest.raises(pe.EntryError):
        pe.submit_month(db, w.p.lo1, date(2026, 3, 15), "lo1")


def test_notice_flow(client, db, w):
    r = client.get(G + "/notice", headers=w.h["lo1"]).json()
    assert r["acknowledged"] is False and r["version"] == gov.NOTICE_VERSION and "AI" in r["text"]
    out = {i["name"] for i in client.get(G + "/notice/outstanding", headers=w.h["hr1"]).json()["items"]}
    assert "Officer One" in out
    assert client.get(G + "/notice/outstanding", headers=w.h["lo1"]).status_code == 403
    assert client.post(G + "/notice/acknowledge", headers=w.h["lo1"]).json()["acknowledged"] is True
    assert client.post(G + "/notice/acknowledge", headers=w.h["lo1"]).status_code == 200  # idempotent
    out = {i["name"] for i in client.get(G + "/notice/outstanding", headers=w.h["hr1"]).json()["items"]}
    assert "Officer One" not in out and "Officer Two" in out


def _leaver(db, w, years_ago):
    p = w.p.lo3
    p.exit_date, p.status, p.employee_no = TODAY.replace(year=TODAY.year - years_ago), "exited", "E-42"
    db.add(PerformanceAction(profile_id=p.id, action_type="warning", restricted=True, started_on=TODAY, note="Sensitive text", outcome="Sensitive outcome"))
    db.add(KpiEntry(profile_id=p.id, kpi_key="sales", month=date(2019, 1, 1), value=90, status="verified", source="manual", note="a note", evidence_path="performance/evidence/x/" + "a" * 32 + ".pdf", evidence_name="proof.pdf"))
    db.commit()
    return p


def test_retention_candidates_and_anonymise(client, db, w, monkeypatch):
    removed = []
    monkeypatch.setattr(gov.storage, "remove", lambda path: removed.append(path))
    p = _leaver(db, w, 8)
    w.p.lo2.exit_date = TODAY.replace(year=TODAY.year - 2)  # inside retention
    db.commit()
    c = client.get(G + "/retention/candidates", headers=w.h["hr1"]).json()
    assert [i["profile_id"] for i in c["items"]] == [p.id] and c["retention_years"] == 7
    assert client.get(G + "/retention/candidates", headers=w.h["bm1"]).status_code == 403
    url = f"{G}/retention/{p.id}/anonymise"
    assert client.post(url, json={"confirm": "anonymise"}, headers=w.h["admin1"]).status_code == 422
    assert client.post(url, json={"confirm": "ANONYMISE"}, headers=w.h["hr1"]).status_code == 403  # admin only
    assert client.post(f"{G}/retention/{w.p.lo2.id}/anonymise", json={"confirm": "ANONYMISE"}, headers=w.h["admin1"]).status_code == 422
    r = client.post(url, json={"confirm": "ANONYMISE"}, headers=w.h["admin1"])
    assert r.status_code == 200 and r.json()["files"] == 1
    db.refresh(p)
    assert p.full_name.startswith("Former staff #") and p.employee_no is None and p.admin_user_id is None
    assert db.query(PerformanceAction).filter_by(profile_id=p.id).one().note is None
    e = db.query(KpiEntry).filter_by(profile_id=p.id).one()
    assert e.value == 90 and e.evidence_path is None and e.note is None  # the number stays
    assert removed and client.post(url, json={"confirm": "ANONYMISE"}, headers=w.h["admin1"]).status_code == 422  # already done
    assert client.get(G + "/retention/candidates", headers=w.h["hr1"]).json()["items"] == []
    assert db.query(PerformanceAuditLog).filter_by(action="retention.anonymise").count() == 1
    assert client.post(f"{G}/retention/nope/anonymise", json={"confirm": "ANONYMISE"}, headers=w.h["admin1"]).status_code == 404


def test_access_log_visibility(client, db, w):
    client.get(f"/api/admin/performance/reports/person/{w.p.lo1.id}", params={"period": "2026-03"}, headers=w.h["bm1"])
    items = client.get(f"{G}/access-log/{w.p.lo1.id}", headers=w.h["hr1"]).json()["items"]
    assert any(i["viewer"] == "bm1" and i["action"] == "report.view" for i in items)
    assert client.get(f"{G}/access-log/{w.p.lo1.id}", headers=w.h["lo1"]).status_code == 200  # the person themselves
    assert client.get(f"{G}/access-log/{w.p.lo1.id}", headers=w.h["lo2"]).status_code == 404
    assert client.get(f"{G}/access-log/{w.p.lo1.id}", headers=w.h["bm1"]).status_code == 404  # managers don't see the viewer list
    assert client.get(f"{G}/access-log/nope", headers=w.h["hr1"]).status_code == 404


def test_all_routes_need_login(client):
    for m, p in (("get", "/notice"), ("post", "/notice/acknowledge"), ("get", "/locks"), ("post", "/locks"), ("post", "/locks/reopen"),
                 ("get", "/retention/candidates"), ("post", "/retention/x/anonymise"), ("get", "/access-log/x")):
        assert getattr(client, m)(G + p).status_code in (401, 403), p
