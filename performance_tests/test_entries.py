"""Monthly KPI entries: window, validation, draft/submit/verify flow, four-eyes, scope, achievement from verified figures only."""

import json
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from app.models.admin_user import AdminUser
from app.models.loan_application import LoanApplication, LoanApplicationStatus
from app.models.performance import KpiEntry, PerformanceAuditLog, PerformanceKpi, PerformanceRoleVersion, StaffProfile
from app.services import performance_entries as pe
from performance_tests.conftest import add_user, token_headers
from performance_tests.org import build_world

BASE = "/api/admin/performance"
TODAY = date(2026, 5, 4)  # April's window (10 days after month end) is open until 10 May; March's closed on 10 April


@pytest.fixture()
def w(client, db, monkeypatch):
    import app.routers.admin_performance_entries as router_mod

    monkeypatch.setattr(router_mod, "today_nairobi", lambda: TODAY)
    return build_world(client, db)


def kpi(db, key="sales", role="loan_officer"):
    from app.models.performance import PerformanceRole
    from app.services.performance_roles import latest_version

    r = db.query(PerformanceRole).filter_by(key=role).one()
    return db.query(PerformanceKpi).filter_by(role_version_id=latest_version(db, r.id).id, key=key).one()


def put(client, who, pid, w, key="sales", month="2026-04-01", value=900000, note=None):
    return client.put(f"{BASE}/staff/{pid}/entries", json={"kpi_key": key, "month": month, "value": value, "note": note}, headers=w.h[who])


def submit(client, who, pid, w, month="2026-04-01"):
    return client.post(f"{BASE}/staff/{pid}/entries/submit", json={"month": month}, headers=w.h[who])


def decide(client, who, ids, w, approve=True, reason=None):
    return client.post(f"{BASE}/entries/decision", json={"entry_ids": ids, "approve": approve, "reason": reason}, headers=w.h[who])


def entry_id(db, pid, key="sales", month=date(2026, 4, 1)):
    return db.query(KpiEntry).filter_by(profile_id=pid, kpi_key=key, month=month).one().id


# ---- pure service rules -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw,ok", [(0, True), (900000, True), ("12.5", True), (-1, False), (True, False), (None, False), ("abc", False), (float("nan"), False), (10**13, False)])
def test_kes_values(db, w, raw, ok):
    k = kpi(db)
    if ok:
        assert pe.clean_value(k, raw) >= 0
    else:
        with pytest.raises(pe.EntryError):
            pe.clean_value(k, raw)


def test_counts_must_be_whole_and_percentages_at_most_100(db, w):
    with pytest.raises(pe.EntryError, match="whole number"):
        pe.clean_value(kpi(db, "new_customers"), 2.5)
    assert pe.clean_value(kpi(db, "new_customers"), 3) == 3
    assert pe.clean_value(kpi(db, "par30"), 100) == 100
    with pytest.raises(pe.EntryError, match="more than 100"):
        pe.clean_value(kpi(db, "par30"), 100.5)


@pytest.mark.parametrize(
    "month,today,state",
    [(date(2026, 4, 1), date(2026, 4, 20), "open"), (date(2026, 4, 1), date(2026, 5, 10), "open"), (date(2026, 4, 1), date(2026, 5, 11), "closed"),
     (date(2026, 5, 1), date(2026, 4, 30), "future"), (date(2026, 5, 1), date(2026, 5, 1), "open")],
)
def test_window_boundaries(month, today, state):
    assert pe.window_state(month, 10, today)["state"] == state


def test_window_closing_date_is_reported():
    assert pe.window_state(date(2026, 4, 1), 10, date(2026, 5, 4))["closes_on"] == date(2026, 5, 10)


# ---- who can enter -----------------------------------------------------------------------------------------------


def test_who_may_enter_for_whom(client, db, w):
    p = w.p
    assert put(client, "lo1", p.lo1.id, w).status_code == 200  # yourself
    assert put(client, "bm1", p.lo2.id, w).status_code == 200  # your branch's manager, on behalf
    assert put(client, "hr1", p.lo3.id, w).status_code == 200  # HR, on behalf
    assert put(client, "gm", p.lo1.id, w, key="new_customers", value=3).status_code == 200  # the region manager
    assert put(client, "lo2", p.lo1.id, w).status_code == 404  # a peer can't even see them
    assert put(client, "bm2", p.lo1.id, w).status_code == 404  # another branch's manager
    assert put(client, "ceo", p.lo1.id, w).status_code == 403  # read-only executive


def test_idor_same_404_for_missing_and_out_of_scope(client, w):
    hit = client.get(f"{BASE}/staff/{w.p.lo3.id}/entries", headers=w.h["lo1"])
    miss = client.get(f"{BASE}/staff/nope/entries", headers=w.h["lo1"])
    assert hit.status_code == miss.status_code == 404 and hit.json() == miss.json()


def test_only_kpis_entered_monthly_are_accepted(client, w):
    assert put(client, "lo1", w.p.lo1.id, w, key="self_character", value=4).status_code == 400  # a rated KPI
    assert put(client, "lo1", w.p.lo1.id, w, key="bogus").status_code == 400


# ---- window ---------------------------------------------------------------------------------------------------------


def test_window_blocks_staff_but_not_hr_and_future_is_never_allowed(client, db, w):
    late = put(client, "lo1", w.p.lo1.id, w, month="2026-03-01")
    assert late.status_code == 400 and "closed on 10 Apr 2026" in late.json()["detail"]
    assert put(client, "bm1", w.p.lo1.id, w, month="2026-03-01").status_code == 400  # managers are bound by it too
    assert put(client, "hr1", w.p.lo1.id, w, month="2026-03-01").status_code == 200  # HR/admin may enter late
    assert put(client, "admin1", w.p.lo2.id, w, month="2026-03-01").status_code == 200
    for who in ("hr1", "lo1"):
        assert put(client, who, w.p.lo1.id, w, month="2026-06-01").status_code == 400  # not started yet


def test_month_view_reports_the_window(client, w):
    body = client.get(f"{BASE}/staff/{w.p.lo1.id}/entries", params={"month": "2026-04-15"}, headers=w.h["lo1"]).json()
    assert body["month"] == "2026-04-01" and body["window"] == {"state": "open", "closes_on": "2026-05-10"}
    assert body["can_enter"] is True and body["can_verify"] is False


# ---- draft -> submitted -> verified ------------------------------------------------------------------------------------


def test_full_flow_and_the_first_entry_locks_the_job_role_version(client, db, w):
    pid = w.p.lo1.id
    assert db.query(PerformanceRoleVersion).filter_by(role_id=w.role.id).one().locked_at is None
    put(client, "lo1", pid, w)
    assert db.query(PerformanceRoleVersion).filter_by(role_id=w.role.id).one().locked_at is not None  # scored data now depends on it
    assert put(client, "lo1", pid, w, value=950000).json()["data"]["rows"][0]["value"] == 950000.0  # drafts are editable
    assert submit(client, "lo1", pid, w).json()["data"]["rows"][0]["status"] == "submitted"
    assert put(client, "lo1", pid, w, value=1).status_code == 400  # no edits once submitted
    assert submit(client, "lo1", pid, w).status_code == 400  # nothing left to submit
    eid = entry_id(db, pid)
    ok = decide(client, "bm1", [eid], w).json()
    assert ok == {"updated": 1, "failed": []}
    row = client.get(f"{BASE}/staff/{pid}/entries", params={"month": "2026-04-01"}, headers=w.h["lo1"]).json()["rows"][0]
    assert row["status"] == "verified" and row["verified_value"] == 950000.0
    assert put(client, "lo1", pid, w, value=1).status_code == 400  # verified: locked until HR reopens


def test_submit_with_nothing_entered_is_a_friendly_400(client, w):
    r = submit(client, "lo1", w.p.lo1.id, w)
    assert r.status_code == 400 and "enter at least one figure" in r.json()["detail"]


def test_only_verified_figures_count_towards_achievement(client, db, w):
    pid = w.p.lo1.id
    client.put(f"{BASE}/staff/{pid}/targets", json={"kpi_key": "sales", "effective_from": "2026-01-01", "value": 600000, "reason": "Level DSE1"}, headers=w.h["hr1"])
    get = lambda: client.get(f"{BASE}/staff/{pid}/entries", params={"month": "2026-04-01"}, headers=w.h["lo1"]).json()["rows"][0]  # noqa: E731
    put(client, "lo1", pid, w, value=900000)
    assert (get()["target"], get()["achievement_pct"], get()["pending_verification"]) == (600000.0, None, True)  # draft: pending
    submit(client, "lo1", pid, w)
    assert get()["achievement_pct"] is None and get()["pending_verification"] is True  # submitted: still pending
    decide(client, "bm1", [entry_id(db, pid)], w)
    assert (get()["achievement_pct"], get()["pending_verification"]) == (150.0, False)


def test_lower_is_better_kpis_invert_achievement(client, db, w):
    pid = w.p.lo1.id
    client.put(f"{BASE}/staff/{pid}/targets", json={"kpi_key": "par30", "effective_from": "2026-01-01", "value": 5, "reason": "Level DSE1"}, headers=w.h["hr1"])
    put(client, "lo1", pid, w, key="par30", value=10)
    submit(client, "lo1", pid, w)
    decide(client, "bm1", [entry_id(db, pid, "par30")], w)
    row = next(r for r in client.get(f"{BASE}/staff/{pid}/entries", params={"month": "2026-04-01"}, headers=w.h["lo1"]).json()["rows"] if r["kpi_key"] == "par30")
    assert row["achievement_pct"] == 50.0  # PAR 10% against a 5% target


def test_new_hire_targets_are_pro_rated_by_the_ramp_setting(client, db, w):
    pid = w.p.lo1.id
    w.p.lo1.hire_date = date(2026, 4, 2)
    db.commit()
    client.put(f"{BASE}/config", json={"config": {"ramp_up_months": 3}}, headers=w.h["admin1"])
    client.put(f"{BASE}/staff/{pid}/targets", json={"kpi_key": "sales", "effective_from": "2026-01-01", "value": 800000, "reason": "Level SNR"}, headers=w.h["hr1"])
    row = client.get(f"{BASE}/staff/{pid}/entries", params={"month": "2026-04-01"}, headers=w.h["lo1"]).json()["rows"][0]
    assert row["target"] == 200000.0  # hire month = 1/4 of full


# ---- verification: four eyes and who -----------------------------------------------------------------------------------


def test_nobody_verifies_their_own_figures_or_ones_they_entered(client, db, w):
    pid = w.p.lo1.id
    put(client, "lo1", pid, w)
    submit(client, "lo1", pid, w)
    eid = entry_id(db, pid)
    own = decide(client, "lo1", [eid], w).json()
    assert own["updated"] == 0 and own["failed"][0]["reason"] == "Not found."  # lo1 isn't anyone's verifier, least of all their own
    # bm1 enters on lo1's behalf, so bm1 may not verify it
    put(client, "bm1", pid, w, key="new_customers", value=4)
    submit(client, "bm1", pid, w)
    nid = entry_id(db, pid, "new_customers")
    mine = decide(client, "bm1", [nid, eid], w).json()
    assert mine["updated"] == 1 and mine["failed"] == [{"entry_id": nid, "reason": "Someone else must verify a figure you entered."}]
    assert decide(client, "hr1", [nid], w).json()["updated"] == 1  # a second person can


def test_who_may_verify_at_all(client, db, w):
    pid = w.p.lo1.id
    put(client, "lo1", pid, w)
    submit(client, "lo1", pid, w)
    eid = entry_id(db, pid)
    for who in ("lo2", "bm2", "ceo"):  # a peer, another branch's manager, the read-only executive
        r = decide(client, who, [eid], w).json()
        assert r["updated"] == 0 and r["failed"][0]["reason"] == "Not found.", who
    assert decide(client, "gm", [eid], w).json()["updated"] == 1  # the region manager


def test_a_branch_office_admin_cannot_verify(client, db, w):
    from app.models.branch import Branch

    b = Branch(name="B1", address="x", hours="x", phone="0", lat=0.0, lng=0.0)
    db.add(b)
    db.commit()
    w.alpha.branch_id = b.id
    boa = add_user(db, "boa", "branch_office_admin", branch_id=b.id)
    db.commit()
    h = token_headers(boa)
    pid = w.p.lo1.id
    put(client, "lo1", pid, w)
    submit(client, "lo1", pid, w)
    assert client.get(f"{BASE}/entries/queue", headers=h).json()["items"] == []  # they can see the branch, but clerks don't verify
    r = client.post(f"{BASE}/entries/decision", json={"entry_ids": [entry_id(db, pid)], "approve": True}, headers=h).json()
    assert r["updated"] == 0


def test_rejection_needs_a_reason_and_the_person_can_fix_and_resubmit(client, db, w):
    pid = w.p.lo1.id
    put(client, "lo1", pid, w, value=100)
    submit(client, "lo1", pid, w)
    eid = entry_id(db, pid)
    assert decide(client, "bm1", [eid], w, approve=False).json()["failed"][0]["reason"] == "Say why you're sending it back."
    assert decide(client, "bm1", [eid], w, approve=False, reason="Missing the Kawangware group").json()["updated"] == 1
    row = client.get(f"{BASE}/staff/{pid}/entries", params={"month": "2026-04-01"}, headers=w.h["lo1"]).json()["rows"][0]
    assert (row["status"], row["rejection_reason"]) == ("rejected", "Missing the Kawangware group")
    fixed = put(client, "lo1", pid, w, value=250).json()["data"]["rows"][0]
    assert (fixed["status"], fixed["rejection_reason"], fixed["value"]) == ("draft", None, 250.0)
    submit(client, "lo1", pid, w)
    assert decide(client, "bm1", [eid], w).json()["updated"] == 1


def test_deciding_the_same_figure_twice_reports_it(client, db, w):
    pid = w.p.lo1.id
    put(client, "lo1", pid, w)
    submit(client, "lo1", pid, w)
    eid = entry_id(db, pid)
    decide(client, "bm1", [eid], w)
    again = decide(client, "hr1", [eid, eid], w).json()  # duplicates in the request are collapsed
    assert again["updated"] == 0 and "not waiting for verification" in again["failed"][0]["reason"]


def test_decision_payload_limits(client, w):
    assert decide(client, "hr1", [], w).status_code == 422
    assert decide(client, "hr1", [f"id{i}" for i in range(201)], w).status_code == 422


# ---- queue -----------------------------------------------------------------------------------------------------------------


def test_queue_shows_each_verifier_only_their_own_people(client, db, w):
    for who, pid in (("lo1", w.p.lo1.id), ("lo3", w.p.lo3.id)):
        put(client, who, pid, w)
        submit(client, who, pid, w)
    names = lambda who: {i["profile_name"] for i in client.get(f"{BASE}/entries/queue", headers=w.h[who]).json()["items"]}  # noqa: E731
    assert names("bm1") == {"Officer One"} and names("bm2") == {"Officer Three"}
    assert names("hr1") == names("admin1") == {"Officer One", "Officer Three"}
    assert names("gm") == {"Officer One"}  # the region manager verifies their own region's units (North = Alpha), not Beta
    assert names("lo1") == set() and names("lo2") == set() and names("ceo") == set()


def test_queue_hides_what_you_entered_and_filters_by_month(client, db, w):
    put(client, "bm1", w.p.lo1.id, w)
    submit(client, "bm1", w.p.lo1.id, w)
    assert client.get(f"{BASE}/entries/queue", headers=w.h["bm1"]).json()["items"] == []
    assert len(client.get(f"{BASE}/entries/queue", headers=w.h["hr1"]).json()["items"]) == 1
    assert client.get(f"{BASE}/entries/queue", params={"month": "2026-03-01"}, headers=w.h["hr1"]).json()["items"] == []


def test_queue_query_count_does_not_grow_with_entries(client, db, session_factory, w):
    from sqlalchemy import event

    engine, counter = session_factory.kw["bind"], {"n": 0}
    bump = lambda *a, **k: counter.__setitem__("n", counter["n"] + 1)  # noqa: E731

    def run():
        counter["n"] = 0
        assert client.get(f"{BASE}/entries/queue", headers=w.h["hr1"]).status_code == 200
        return counter["n"]

    put(client, "lo1", w.p.lo1.id, w)
    submit(client, "lo1", w.p.lo1.id, w)
    event.listen(engine, "before_cursor_execute", bump)
    try:
        few = run()
        for i in range(40):
            p = StaffProfile(full_name=f"Extra {i}", performance_role_id=w.role.id, unit_id=w.alpha.id)
            db.add(p)
            db.flush()
            db.add(KpiEntry(profile_id=p.id, kpi_key="sales", month=date(2026, 4, 1), value=D(1), status="submitted", entered_by_username="x"))
        db.commit()
        many = run()
    finally:
        event.remove(engine, "before_cursor_execute", bump)
    assert many == few


# ---- reopen & audit -------------------------------------------------------------------------------------------------------------


def test_hr_can_reopen_a_verified_figure_with_a_reason(client, db, w):
    pid = w.p.lo1.id
    put(client, "lo1", pid, w)
    submit(client, "lo1", pid, w)
    eid = entry_id(db, pid)
    decide(client, "bm1", [eid], w)
    for who in ("lo1", "bm1", "gm", "ceo"):
        assert client.post(f"{BASE}/entries/{eid}/reopen", json={"reason": "Wrong month"}, headers=w.h[who]).status_code == 403
    assert client.post(f"{BASE}/entries/{eid}/reopen", json={"reason": ""}, headers=w.h["hr1"]).status_code == 400
    r = client.post(f"{BASE}/entries/{eid}/reopen", json={"reason": "Wrong month"}, headers=w.h["hr1"])
    assert r.status_code == 200 and r.json()["data"]["rows"][0]["status"] == "draft"
    assert client.post(f"{BASE}/entries/{eid}/reopen", json={"reason": "again"}, headers=w.h["hr1"]).status_code == 400  # already open
    assert client.post(f"{BASE}/entries/nope/reopen", json={"reason": "x y z"}, headers=w.h["hr1"]).status_code == 404


def test_the_audit_log_never_carries_a_figure_or_a_note(client, db, w):
    pid = w.p.lo1.id
    put(client, "lo1", pid, w, value=123456789, note="PRIVATE-NOTE-TEXT")
    submit(client, "lo1", pid, w)
    decide(client, "bm1", [entry_id(db, pid)], w)
    blob = json.dumps([e.details for e in db.query(PerformanceAuditLog)])
    assert "123456789" not in blob and "PRIVATE-NOTE" not in blob
    assert {e.action for e in db.query(PerformanceAuditLog)} >= {"entry_saved", "entries_submitted", "entries_verified"}


# ---- optional suggestion from approved applications -----------------------------------------------------------------------------------


def app_row(officer_id, amount, status=LoanApplicationStatus.approved, created=datetime(2026, 4, 10, 9, tzinfo=timezone.utc)):
    return LoanApplication(product_slug="p", product_name="P", tier_id="t", tier_label="T", amount=amount, term_value=6, term_unit="months",
                           estimated_installment=1.0, full_name="Applicant Name", id_number="12345678", phone="0700000000", email="a@b.co",
                           monthly_income="10000", assigned_loan_officer_id=officer_id, status=status, created_at=created)


def test_suggestion_only_counts_this_officers_approved_applications_that_month(client, db, w):
    officer = db.query(AdminUser).filter_by(username="lo1").one()
    other = db.query(AdminUser).filter_by(username="lo2").one()
    db.add_all([
        app_row(officer.id, 100000), app_row(officer.id, 50000),
        app_row(officer.id, 70000, status=LoanApplicationStatus.pending),  # not approved
        app_row(other.id, 999999),  # someone else's
        app_row(officer.id, 888888, created=datetime(2026, 3, 10, tzinfo=timezone.utc)),  # another month
    ])
    db.commit()
    rows = client.get(f"{BASE}/staff/{w.p.lo1.id}/entries", params={"month": "2026-04-01"}, headers=w.h["lo1"]).json()["rows"]
    sales = next(r for r in rows if r["kpi_key"] == "sales")
    assert sales["suggestion"]["count"] == 2 and sales["suggestion"]["total_amount"] == 150000.0
    assert "not disbursement" in sales["suggestion"]["note"]
    assert next(r for r in rows if r["kpi_key"] == "par30")["suggestion"] is None  # only the disbursement-type figure
    assert "Applicant Name" not in json.dumps(rows) and "12345678" not in json.dumps(rows)  # never any applicant detail
    assert db.query(KpiEntry).count() == 0  # a suggestion writes nothing
    mine = client.get(f"{BASE}/staff/{w.p.lo2.id}/entries", params={"month": "2026-04-01"}, headers=w.h["lo2"]).json()["rows"][0]
    assert (mine["suggestion"]["count"], mine["suggestion"]["total_amount"]) == (1, 999999.0)  # their own, never lo1's


def test_staff_without_a_login_get_no_suggestion_and_can_still_be_recorded(client, db, w):
    p = StaffProfile(full_name="No Login", performance_role_id=w.role.id, unit_id=w.alpha.id, manager_id=w.p.bm1.id)
    db.add(p)
    db.commit()
    assert put(client, "bm1", p.id, w).status_code == 200
    assert client.get(f"{BASE}/staff/{p.id}/entries", params={"month": "2026-04-01"}, headers=w.h["bm1"]).json()["rows"][0]["suggestion"] is None


def test_a_manager_cannot_verify_their_own_figures_even_when_someone_else_entered_them(client, db, w):
    """bm1 manages Alpha AND works in Alpha, so only the 'never your own figures' rule stops them; HR enters on their behalf so the
    entered-by rule doesn't apply."""
    pid = w.p.bm1.id
    assert put(client, "hr1", pid, w).status_code == 200
    assert submit(client, "hr1", pid, w).status_code == 200
    eid = entry_id(db, pid)
    own = decide(client, "bm1", [eid], w).json()
    assert own["updated"] == 0 and own["failed"][0]["reason"] == "Not found."
    assert client.get(f"{BASE}/entries/queue", headers=w.h["bm1"]).json()["items"] == []  # not even listed for them
    assert decide(client, "gm", [eid], w).json()["updated"] == 1  # their own manager can
