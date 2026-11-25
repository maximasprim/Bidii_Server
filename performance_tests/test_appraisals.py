"""Appraisals: creation snapshot, the sign-off chain (actors resolved by role), ratings, returns, CEO step, disputes,
acknowledgement, reopening, scope/IDOR, PDF, audit hygiene."""

import json
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from app.models.performance import (
    Appraisal,
    AppraisalItem,
    KpiEntry,
    PerformanceAuditLog,
    PerformanceRole,
    PerformanceRoleVersion,
    StaffProfile,
)
from performance_tests.conftest import add_user, token_headers
from performance_tests.org import build_world

BASE = "/api/admin/performance"
PERIOD = "2026-Q1"


@pytest.fixture()
def w(client, db):
    o = build_world(client, db)
    role = lambda key: db.query(PerformanceRole).filter_by(key=key).one()  # noqa: E731
    o.p.gm.performance_role_id = role("general_manager").id
    o.p.ceo.performance_role_id = role("ceo").id
    db.commit()
    o.role_of = role
    return o


def person(db, w, username, name, role_key, access="staff", unit=None, manager=None, login=True):
    user = add_user(db, username, access) if login else None
    p = StaffProfile(full_name=name, performance_role_id=w.role_of(role_key).id, admin_user_id=user.id if user else None, unit_id=unit.id if unit else None, manager_id=manager.id if manager else None)
    db.add(p)
    db.commit()
    if user:
        w.h[username] = token_headers(user)
    return p


def create(client, w, who, profile, period=PERIOD):
    return client.post(f"{BASE}/staff/{profile.id}/appraisals", json={"period": period}, headers=w.h[who])


def get(client, w, who, aid):
    return client.get(f"{BASE}/appraisals/{aid}", headers=w.h[who])


def form(client, w, who, aid, **body):
    return client.put(f"{BASE}/appraisals/{aid}/form", json=body, headers=w.h[who])


def sign(client, w, who, aid, comment=None, decision="sign"):
    return client.post(f"{BASE}/appraisals/{aid}/sign", json={"decision": decision, "comment": comment}, headers=w.h[who])


def rate_staff(client, w, who, aid, rating=4, texts=True):
    d = get(client, w, who, aid).json()
    return form(client, w, who, aid,
                items=[{"kpi_key": i["kpi_key"], "self_rating": rating, **({"self_text": f"Did {i['label']}"} if texts else {})} for i in d["items"]],
                competencies=[{"key": c["key"], "self_rating": rating} for c in d["competencies"]])


def rate_manager(client, w, who, aid, rating=4):
    d = get(client, w, who, aid).json()
    return form(client, w, who, aid, items=[{"kpi_key": i["kpi_key"], "manager_rating": rating} for i in d["items"]],
                competencies=[{"key": c["key"], "manager_rating": rating} for c in d["competencies"]])


def start(client, w, owner="lo1", profile=None):
    r = create(client, w, owner, profile or w.p.lo1)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def to_manager(client, w, aid, who="lo1", rating=4):
    assert rate_staff(client, w, who, aid, rating).status_code == 200
    r = client.post(f"{BASE}/appraisals/{aid}/submit", headers=w.h[who])
    assert r.status_code == 200, r.text
    return r


def to_rm_gm(client, w, aid, self_rating=4, mgr_rating=4):
    to_manager(client, w, aid, rating=self_rating)
    assert rate_manager(client, w, "bm1", aid, mgr_rating).status_code == 200
    assert sign(client, w, "bm1", aid, "Solid quarter").status_code == 200, "manager sign"


def finish(client, w, aid, self_rating=4, mgr_rating=4):
    to_rm_gm(client, w, aid, self_rating, mgr_rating)
    assert sign(client, w, "gm", aid).status_code == 200
    r = sign(client, w, "hr1", aid, "Approved")
    assert r.status_code == 200, r.text
    return r.json()["data"]


# ---- creating --------------------------------------------------------------------------------------------------------


def test_who_may_start_an_appraisal(client, w):
    assert create(client, w, "lo1", w.p.lo1).status_code == 201  # yourself
    assert create(client, w, "bm1", w.p.lo2).status_code == 201  # your manager
    assert create(client, w, "hr1", w.p.lo3).status_code == 201
    assert create(client, w, "admin1", w.p.bm1).status_code == 201
    assert create(client, w, "lo2", w.p.lo1, "2026-Q2").status_code == 404  # a peer can't see them
    assert create(client, w, "bm2", w.p.lo1, "2026-Q2").status_code == 404
    assert create(client, w, "ceo", w.p.lo1, "2026-Q2").status_code == 403  # read-only executive
    assert create(client, w, "gm", w.p.lo1, "2026-Q2").status_code == 403  # sees them, but isn't their manager/HR


def test_one_appraisal_per_person_per_period_and_period_rules(client, w):
    assert create(client, w, "lo1", w.p.lo1).status_code == 201
    assert create(client, w, "lo1", w.p.lo1).status_code == 409
    assert create(client, w, "lo1", w.p.lo1, "2026-Q2").status_code == 201
    for bad in ("2026-03", "junk", "2026-Q5", "26-Q1"):
        assert create(client, w, "lo1", w.p.lo1, bad).status_code == 400, bad
    assert create(client, w, "lo1", w.p.lo1, "2026-H1").status_code == 201 and create(client, w, "lo1", w.p.lo1, "2026").status_code == 201


def test_a_role_with_no_kpi_set_cannot_be_appraised(client, db, w):
    p = person(db, w, "hrp", "HR Person", "hr_officer")
    r = create(client, w, "hr1", p)
    assert r.status_code == 400 and "no complete KPI set" in r.json()["detail"]


def test_the_form_is_built_from_the_job_role_and_locks_its_version(client, db, w):
    assert db.query(PerformanceRoleVersion).filter_by(role_id=w.role.id).one().locked_at is None
    d = create(client, w, "lo1", w.p.lo1).json()["data"]
    assert db.query(PerformanceRoleVersion).filter_by(role_id=w.role.id).one().locked_at is not None
    assert [(i["label"], i["weight"]) for i in d["items"]] == [
        ("Sales - loan value disbursed", 50.0), ("New customers acquired", 20.0), ("Loan quality - 30-day PAR", 20.0),
        ("Customers deactivated or balanced off", 5.0), ("Self (character) and people skills", 5.0)]
    assert d["items"][0]["criteria"] == "Total value of loans disbursed per month. Minimum Target as per staff level"  # the form's own words
    assert [o["label"] for o in d["objectives"]] == ["New customers", "Sales", "Quality (PAR)"] and d["objective_months"] == ["Apr 2026", "May 2026", "Jun 2026"]
    assert [n["slot"] for n in d["development_needs"]] == [1, 2] and d["competencies"] == []
    assert [c["step"] for c in d["chain"]] == ["staff", "manager", "rm_gm", "hr"] and d["status"] == "draft" and d["current_step"] == "staff"


def test_editing_the_job_role_later_never_changes_an_appraisal_in_flight(client, db, w):
    aid = start(client, w)
    body = {"jd": {"title": "Loan Officer v2", "responsibilities": [{"title": "Only", "weight_pct": 100}]},
            "kpis": [{"label": "Only KPI", "unit": "kes", "direction": "higher", "weight": 100, "source": "kpi_entry", "frequency": "monthly", "jd_section": "Only"}],
            "signoff_chain": ["staff", "manager"], "requires_ceo_signoff": False}
    assert client.put(f"{BASE}/roles/{w.role.id}/jd", json=body, headers=w.h["admin1"]).json()["created_new_version"] is True  # v1 was locked
    d = get(client, w, "lo1", aid).json()
    assert len(d["items"]) == 5 and [c["step"] for c in d["chain"]] == ["staff", "manager", "rm_gm", "hr"]


def test_self_assessment_is_prefilled_from_verified_figures_only(client, db, w):
    pid = w.p.lo1.id
    for m, status in ((1, "verified"), (2, "verified"), (3, "verified")):
        client.put(f"{BASE}/staff/{pid}/targets", json={"kpi_key": "sales", "effective_from": f"2026-0{m}-01", "value": 600000, "reason": "Level DSE1"}, headers=w.h["hr1"])
        db.add(KpiEntry(profile_id=pid, kpi_key="sales", month=date(2026, m, 1), value=D(600000), status=status))
    db.add(KpiEntry(profile_id=pid, kpi_key="par30", month=date(2026, 2, 1), value=D(6), status="verified"))
    db.add(KpiEntry(profile_id=pid, kpi_key="par30", month=date(2026, 3, 1), value=D(4), status="verified"))
    db.add(KpiEntry(profile_id=pid, kpi_key="new_customers", month=date(2026, 3, 1), value=D(9), status="submitted"))  # not verified
    db.commit()
    d = get(client, w, "lo1", start(client, w)).json()
    by = {i["kpi_key"]: i for i in d["items"]}
    assert by["sales"]["self_text"] == "Verified over the period: KES 1,800,000 against a target of KES 1,800,000 (100.0%)."
    assert by["par30"]["self_text"] == "Latest verified (Mar 2026): 4%."
    assert by["new_customers"]["self_text"] is None  # pending verification doesn't count


def test_competencies_come_from_the_jd(client, db, w):
    p = person(db, w, "cra1", "Risk Analyst", "credit_risk_analyst", manager=w.p.gm)
    d = create(client, w, "hr1", p).json()["data"]
    assert [c["title"] for c in d["competencies"]][:2] == ["Putting customers first", "Performing through our people"] and len(d["competencies"]) == 6
    assert [i["weight"] for i in d["items"]] == [30.0, 25.0, 15.0, 15.0, 15.0]


# ---- staff step -------------------------------------------------------------------------------------------------------------


def test_staff_must_rate_every_row_before_submitting(client, w):
    aid = start(client, w)
    form(client, w, "lo1", aid, items=[{"kpi_key": "sales", "self_rating": 4}])
    r = client.post(f"{BASE}/appraisals/{aid}/submit", headers=w.h["lo1"])
    assert r.status_code == 400 and "Still to rate" in r.json()["detail"] and "New customers acquired" in r.json()["detail"]


def test_submit_moves_to_the_manager_and_closes_the_staff_step(client, w):
    aid = start(client, w)
    d = to_manager(client, w, aid).json()["data"]
    assert (d["status"], d["current_step"]) == ("in_review", "manager") and d["chain"][0]["state"] == "done" and d["chain"][1]["state"] == "current"
    assert d["permissions"]["can_edit"] is False  # the staff member's turn is over
    assert form(client, w, "lo1", aid, items=[{"kpi_key": "sales", "self_rating": 1}]).status_code == 403
    assert client.post(f"{BASE}/appraisals/{aid}/submit", headers=w.h["lo1"]).status_code == 409


@pytest.mark.parametrize("bad", [0, 6, -1, 2.5, "4", True])
def test_ratings_are_whole_numbers_1_to_5(client, w, bad):
    aid = start(client, w)
    r = form(client, w, "lo1", aid, items=[{"kpi_key": "sales", "self_rating": bad}])
    assert r.status_code in (400, 422)


def test_unknown_rows_and_oversized_text_are_rejected(client, w):
    aid = start(client, w)
    assert form(client, w, "lo1", aid, items=[{"kpi_key": "nope", "self_rating": 3}]).status_code == 400
    assert form(client, w, "lo1", aid, items=[{"kpi_key": "sales", "self_text": "x" * 4001}]).status_code == 400
    assert form(client, w, "lo1", aid, development_needs=[{"slot": 3, "text": "x"}]).status_code == 400
    assert form(client, w, "lo1", aid, objectives=[{"label": "Sales", "values": ["a"]}]).status_code == 400


def test_each_step_can_only_change_its_own_fields(client, w):
    aid = start(client, w)
    assert form(client, w, "lo1", aid, items=[{"kpi_key": "sales", "manager_rating": 5}]).status_code == 403  # staff can't rate themselves as the manager
    to_manager(client, w, aid)
    r = form(client, w, "bm1", aid, items=[{"kpi_key": "sales", "self_rating": 5}])
    assert r.status_code == 403 and "self_rating" in r.json()["detail"]  # the manager can't rewrite the self-assessment
    assert form(client, w, "bm1", aid, items=[{"kpi_key": "sales", "manager_rating": 5}], development_needs=[{"slot": 1, "text": "Excel course"}]).status_code == 200


def test_form_values_are_saved_and_shown_back_with_weighted_ratings(client, w):
    aid = start(client, w)
    form(client, w, "lo1", aid, items=[{"kpi_key": "sales", "self_rating": 4, "self_text": "Hit most targets"}], objectives=[{"label": "Sales", "values": ["a", "b", "c"]}], development_needs=[{"slot": 2, "text": "Coaching"}])
    d = get(client, w, "lo1", aid).json()
    sales = next(i for i in d["items"] if i["kpi_key"] == "sales")
    assert (sales["self_text"], sales["self_rating"], sales["self_weighted"]) == ("Hit most targets", 4, 2.0)  # 50 x 4 / 100
    assert next(o for o in d["objectives"] if o["label"] == "Sales")["values"] == ["a", "b", "c"] and d["development_needs"][1]["text"] == "Coaching"


# ---- who may act at each step -------------------------------------------------------------------------------------------------


def test_only_the_direct_manager_acts_at_the_manager_step(client, w):
    aid = start(client, w)
    to_manager(client, w, aid)
    for who in ("lo1", "lo2", "bm2", "gm", "hr1", "admin1", "ceo"):
        assert form(client, w, who, aid, items=[{"kpi_key": "sales", "manager_rating": 3}]).status_code in (403, 404), who
        assert sign(client, w, who, aid).status_code in (403, 404), who
    assert rate_manager(client, w, "bm1", aid).status_code == 200


def test_the_manager_must_rate_every_row_before_signing(client, w):
    aid = start(client, w)
    to_manager(client, w, aid)
    form(client, w, "bm1", aid, items=[{"kpi_key": "sales", "manager_rating": 4}])
    r = sign(client, w, "bm1", aid)
    assert r.status_code == 400 and "Still to rate" in r.json()["detail"]


def test_rm_gm_step_is_resolved_by_role_not_by_who_is_nearby(client, w):
    aid = start(client, w)
    to_rm_gm(client, w, aid)
    for who in ("bm1", "lo1", "hr1", "admin1", "bm2", "ceo"):  # not even HR: the GM exists
        assert sign(client, w, who, aid).status_code in (403, 404), who
    d = sign(client, w, "gm", aid, "Agreed").json()["data"]
    assert d["current_step"] == "hr" and d["status"] == "in_review"


def test_only_hr_signs_the_hr_step_and_signing_the_last_step_finalises(client, w):
    aid = start(client, w)
    to_rm_gm(client, w, aid)
    sign(client, w, "gm", aid)
    for who in ("gm", "bm1", "lo1", "ceo"):
        assert sign(client, w, who, aid).status_code in (403, 404), who
    d = sign(client, w, "hr1", aid, "Done").json()
    assert d["message"] == "Appraisal finalised." and d["data"]["status"] == "finalised" and d["data"]["current_step"] is None
    assert [c["state"] for c in d["data"]["chain"]] == ["done"] * 4
    assert [(s["step"], s["decision"], s["actor_username"]) for s in d["data"]["signoffs"]] == [("staff", "signed", "lo1"), ("manager", "signed", "bm1"), ("rm_gm", "signed", "gm"), ("hr", "signed", "hr1")]


def test_nobody_signs_their_own_appraisal_beyond_the_staff_step(client, w):
    aid = start(client, w, "bm1", w.p.bm1)  # the branch manager's own appraisal; their manager is the GM
    to_manager(client, w, aid, who="bm1")
    assert sign(client, w, "bm1", aid).status_code == 403
    assert rate_manager(client, w, "bm1", aid).status_code == 403
    assert rate_manager(client, w, "gm", aid).status_code == 200


def test_a_person_with_no_manager_falls_back_to_hr(client, db, w):
    solo = person(db, w, "solo", "Solo Officer", "loan_officer", unit=w.alpha)  # configured role, but nobody manages them
    aid = start(client, w, "solo", solo)
    to_manager(client, w, aid, who="solo")
    assert rate_manager(client, w, "bm1", aid).status_code == 403  # their unit's manager isn't their line manager
    assert rate_manager(client, w, "hr1", aid).status_code == 200 and sign(client, w, "hr1", aid).status_code == 200


def test_csm_step_goes_to_the_customer_service_manager_and_hr_covers_an_empty_role(client, db, w):
    admin = person(db, w, "ba1", "Office Admin", "branch_admin", access="branch_office_admin", unit=w.alpha, manager=w.p.bm1)
    aid = start(client, w, "ba1", admin)
    to_manager(client, w, aid, who="ba1")
    rate_manager(client, w, "bm1", aid)
    assert [c["step"] for c in get(client, w, "ba1", aid).json()["chain"]] == ["staff", "manager", "csm", "rm_gm"]  # the admins form has no HR step
    assert sign(client, w, "bm1", aid).json()["data"]["current_step"] == "csm"
    assert sign(client, w, "gm", aid).status_code == 403 and sign(client, w, "bm1", aid).status_code == 403
    assert sign(client, w, "hr1", aid).status_code == 200  # nobody holds the CSM role yet, so HR keeps it moving
    d2 = start(client, w, "ba1", admin) if False else None  # noqa: F841
    csm = person(db, w, "csm1", "Support Manager", "customer_service_manager", manager=w.p.gm)
    other = person(db, w, "ba2", "Office Admin Two", "branch_admin", access="branch_office_admin", unit=w.alpha, manager=w.p.bm1)
    aid2 = start(client, w, "ba2", other)
    to_manager(client, w, aid2, who="ba2")
    rate_manager(client, w, "bm1", aid2)
    sign(client, w, "bm1", aid2)
    assert sign(client, w, "hr1", aid2).status_code == 403  # now the CSM exists, HR may not stand in
    assert sign(client, w, "csm1", aid2).status_code == 200 and csm.id


def test_staff_without_a_login_are_handled_by_hr_or_their_manager(client, db, w):
    p = person(db, w, "x", "No Login Officer", "loan_officer", unit=w.alpha, manager=w.p.bm1, login=False)
    aid = create(client, w, "bm1", p).json()["data"]["id"]
    assert form(client, w, "lo2", aid, items=[]).status_code == 404  # a peer can't see them
    assert rate_staff(client, w, "bm1", aid).status_code == 200  # the manager fills it in on their behalf
    assert client.post(f"{BASE}/appraisals/{aid}/submit", headers=w.h["hr1"]).status_code == 200
    assert get(client, w, "bm1", aid).json()["current_step"] == "manager"


# ---- the CEO step ---------------------------------------------------------------------------------------------------------------


def role_with_ceo(client, w, role_id, chain=None):
    cur = client.get(f"{BASE}/roles/{role_id}/jd", headers=w.h["admin1"]).json()
    body = {"jd": cur["jd"], "kpis": cur["kpis"], "signoff_chain": chain or cur["signoff_chain"], "requires_ceo_signoff": True}
    assert client.put(f"{BASE}/roles/{role_id}/jd", json=body, headers=w.h["admin1"]).status_code == 200


def test_senior_roles_add_a_ceo_step_and_only_the_executive_signs_it(client, db, w):
    role_with_ceo(client, w, w.role.id)
    aid = start(client, w)
    assert [c["step"] for c in get(client, w, "lo1", aid).json()["chain"]] == ["staff", "manager", "rm_gm", "hr", "ceo"]
    to_rm_gm(client, w, aid)
    sign(client, w, "gm", aid)
    d = sign(client, w, "hr1", aid).json()["data"]
    assert d["status"] == "in_review" and d["current_step"] == "ceo"  # HR no longer finishes it
    for who in ("hr1", "gm", "admin1", "bm1", "lo1"):
        assert sign(client, w, who, aid).status_code in (403, 404), who
    assert get(client, w, "ceo", aid).json()["permissions"]["can_sign"] is True  # the read-only executive may sign THIS step
    assert form(client, w, "ceo", aid, items=[{"kpi_key": "sales", "manager_rating": 1}]).status_code == 403  # but never edit
    assert sign(client, w, "ceo", aid, "Congratulations").json()["data"]["status"] == "finalised"


def test_the_executive_cannot_sign_any_other_step(client, w):
    aid = start(client, w)
    to_rm_gm(client, w, aid)
    assert sign(client, w, "ceo", aid).status_code == 403


# ---- final rating ---------------------------------------------------------------------------------------------------------------


def test_final_rating_follows_the_manager_by_default(client, w):
    d = finish(client, w, start(client, w), self_rating=5, mgr_rating=3)
    assert [i["final_rating"] for i in d["items"]] == [3.0] * 5 and d["final_weighted_rating"] == 3.0
    assert (d["self_weighted_total"], d["manager_weighted_total"]) == (5.0, 3.0)  # the form's two totals
    assert [i["self_rating"] for i in d["items"]] == [5] * 5  # the gap stays visible


def test_final_rating_can_average_self_and_manager(client, w):
    client.put(f"{BASE}/config", json={"config": {"rating_authority": "average"}}, headers=w.h["admin1"])
    d = finish(client, w, start(client, w), self_rating=5, mgr_rating=3)
    assert [i["final_rating"] for i in d["items"]] == [4.0] * 5 and d["final_weighted_rating"] == 4.0


def test_weighted_final_rating_uses_the_row_weights(client, db, w):
    aid = start(client, w)
    to_manager(client, w, aid)
    d = get(client, w, "bm1", aid).json()
    ratings = {"sales": 5, "new_customers": 3, "par30": 4, "retention": 2, "self_character": 5}
    form(client, w, "bm1", aid, items=[{"kpi_key": k, "manager_rating": v} for k, v in ratings.items()])
    sign(client, w, "bm1", aid)
    sign(client, w, "gm", aid)
    out = sign(client, w, "hr1", aid).json()["data"]
    assert out["final_weighted_rating"] == 4.25  # (50x5 + 20x3 + 20x4 + 5x2 + 5x5) / 100 = 425 / 100
    assert d["items"] and db.query(AppraisalItem).count() == 5


def test_finalised_appraisals_are_read_only(client, w):
    aid = start(client, w)
    finish(client, w, aid)
    for who in ("lo1", "bm1", "hr1"):
        assert form(client, w, who, aid, items=[{"kpi_key": "sales", "manager_rating": 1}]).status_code == 409
    assert sign(client, w, "hr1", aid).status_code == 409


# ---- returning -----------------------------------------------------------------------------------------------------------------


def test_a_reviewer_can_return_it_one_step_with_a_reason(client, w):
    aid = start(client, w)
    to_rm_gm(client, w, aid)
    assert sign(client, w, "gm", aid, decision="return").status_code == 400  # a reason is required
    d = sign(client, w, "gm", aid, "Ratings look generous", decision="return").json()
    assert d["data"]["current_step"] == "manager" and d["data"]["status"] == "in_review" and d["message"] == "Returned."
    assert d["data"]["signoffs"][-1]["decision"] == "returned" and d["data"]["signoffs"][-1]["comment"] == "Ratings look generous"
    assert rate_manager(client, w, "bm1", aid, 3).status_code == 200 and sign(client, w, "bm1", aid).status_code == 200


def test_returning_to_the_staff_member_reopens_their_form(client, w):
    aid = start(client, w)
    to_manager(client, w, aid)
    d = sign(client, w, "bm1", aid, "Please add evidence for sales", decision="return").json()["data"]
    assert (d["status"], d["current_step"]) == ("draft", "staff") and d["permissions"]["can_edit"] is False  # for bm1 now
    assert get(client, w, "lo1", aid).json()["permissions"]["can_edit"] is True
    assert sign(client, w, "lo1", aid).status_code == 409  # the staff step is submitted, not signed


# ---- disputes -------------------------------------------------------------------------------------------------------------------


def dispute(client, w, who, aid, item="sales", reason="I closed 12 loans that were not counted"):
    return client.post(f"{BASE}/appraisals/{aid}/disputes", json={"item_key": item, "reason": reason}, headers=w.h[who])


def resolve(client, w, who, aid, did, resolution="Reviewed with the manager", rating=None):
    return client.post(f"{BASE}/appraisals/{aid}/disputes/{did}/resolve", json={"resolution": resolution, "new_manager_rating": rating}, headers=w.h[who])


def test_a_dispute_needs_a_rating_to_dispute(client, w):
    aid = start(client, w)
    to_manager(client, w, aid)
    assert dispute(client, w, "lo1", aid).status_code == 409  # the manager hasn't rated yet


def test_only_the_appraisee_disputes_and_hr_resolves(client, w):
    aid = start(client, w)
    to_rm_gm(client, w, aid)
    for who in ("bm1", "gm", "hr1"):
        assert dispute(client, w, who, aid).status_code == 403, who
    assert dispute(client, w, "lo2", aid).status_code == 404
    assert dispute(client, w, "lo1", aid, reason="no").status_code == 400
    assert dispute(client, w, "lo1", aid, item="bogus").status_code == 400
    r = dispute(client, w, "lo1", aid)
    assert r.status_code == 201
    did = r.json()["data"]["disputes"][0]["id"]
    for who in ("lo1", "bm1", "gm", "ceo"):  # an administrator can resolve like HR
        assert resolve(client, w, who, aid, did).status_code in (403, 404), who
    assert resolve(client, w, "hr1", aid, did, "").status_code == 400
    assert resolve(client, w, "hr1", aid, did, rating=9).status_code == 400
    done = resolve(client, w, "hr1", aid, did, "Counted the 12 loans", rating=5).json()["data"]
    assert done["disputes"][0]["status"] == "resolved" and "changed from 4 to 5" in done["disputes"][0]["resolution"]
    assert next(i for i in done["items"] if i["kpi_key"] == "sales")["manager_rating"] == 5
    assert resolve(client, w, "hr1", aid, did).status_code == 409


def test_open_disputes_block_the_hr_step_and_the_last_step(client, w):
    aid = start(client, w)
    to_rm_gm(client, w, aid)
    did = dispute(client, w, "lo1", aid).json()["data"]["disputes"][0]["id"]
    assert sign(client, w, "gm", aid).status_code == 200  # a middle step may proceed
    blocked = sign(client, w, "hr1", aid)
    assert blocked.status_code == 409 and "open disputes" in blocked.json()["detail"]
    resolve(client, w, "hr1", aid, did)
    assert sign(client, w, "hr1", aid).json()["data"]["status"] == "finalised"


def test_a_dispute_without_hr_in_the_chain_still_blocks_the_final_step(client, db, w):
    admin = person(db, w, "ba1", "Office Admin", "branch_admin", access="branch_office_admin", unit=w.alpha, manager=w.p.bm1)
    person(db, w, "csm1", "Support Manager", "customer_service_manager", manager=w.p.gm)
    aid = start(client, w, "ba1", admin)
    to_manager(client, w, aid, who="ba1")
    rate_manager(client, w, "bm1", aid)
    sign(client, w, "bm1", aid)
    dispute(client, w, "ba1", aid, item="office_management")
    sign(client, w, "csm1", aid)
    assert sign(client, w, "gm", aid).status_code == 409  # rm_gm is the LAST step in this chain


def test_a_rating_cannot_be_changed_by_a_dispute_after_finalisation(client, w):
    aid = start(client, w)
    to_rm_gm(client, w, aid)
    did = dispute(client, w, "lo1", aid).json()["data"]["disputes"][0]["id"]
    resolve(client, w, "hr1", aid, did)
    sign(client, w, "gm", aid)
    sign(client, w, "hr1", aid)
    did2 = dispute(client, w, "lo1", aid, reason="Still disagree with sales").json()["data"]["disputes"][-1]["id"]  # allowed on a finalised appraisal
    assert resolve(client, w, "hr1", aid, did2, rating=5).status_code == 409


# ---- acknowledge & reopen --------------------------------------------------------------------------------------------------------------


def test_the_appraisee_acknowledges_once_after_finalisation(client, w):
    aid = start(client, w)
    assert client.post(f"{BASE}/appraisals/{aid}/acknowledge", headers=w.h["lo1"]).status_code == 409  # not finalised
    finish(client, w, aid)
    assert get(client, w, "lo1", aid).json()["permissions"]["can_acknowledge"] is True
    for who in ("bm1", "hr1", "gm"):
        assert client.post(f"{BASE}/appraisals/{aid}/acknowledge", headers=w.h[who]).status_code == 403
    d = client.post(f"{BASE}/appraisals/{aid}/acknowledge", headers=w.h["lo1"]).json()["data"]
    assert d["acknowledged_by_username"] == "lo1" and d["acknowledged_at"]
    assert client.post(f"{BASE}/appraisals/{aid}/acknowledge", headers=w.h["lo1"]).status_code == 409


def test_reopening_is_admin_or_hr_with_a_reason_and_restarts_review(client, w):
    aid = start(client, w)
    finish(client, w, aid)
    for who in ("lo1", "bm1", "gm", "ceo"):
        assert client.post(f"{BASE}/appraisals/{aid}/reopen", json={"reason": "Mistake in sales"}, headers=w.h[who]).status_code == 403
    assert client.post(f"{BASE}/appraisals/{aid}/reopen", json={"reason": ""}, headers=w.h["hr1"]).status_code == 400
    d = client.post(f"{BASE}/appraisals/{aid}/reopen", json={"reason": "Mistake in sales"}, headers=w.h["hr1"]).json()["data"]
    assert (d["status"], d["current_step"], d["final_weighted_rating"]) == ("in_review", "manager", None)
    assert all(i["final_rating"] is None for i in d["items"])
    assert d["signoffs"][-1]["step"] == "reopened" and "Mistake in sales" in d["signoffs"][-1]["comment"] and len(d["signoffs"]) == 5  # history is kept
    assert client.post(f"{BASE}/appraisals/{aid}/reopen", json={"reason": "again please"}, headers=w.h["hr1"]).status_code == 409
    assert rate_manager(client, w, "bm1", aid, 5).status_code == 200


def test_nobody_reopens_their_own_appraisal(client, db, w):
    hr_user = db.query(__import__("app.models.admin_user", fromlist=["AdminUser"]).AdminUser).filter_by(username="hr1").one()
    p = StaffProfile(full_name="HR Person", performance_role_id=w.role.id, admin_user_id=hr_user.id, manager_id=w.p.gm.id)
    db.add(p)
    db.commit()
    ap = Appraisal(profile_id=p.id, period_key="2026-Q1", period_start=date(2026, 1, 1), period_end=date(2026, 3, 31), role_id=w.role.id, role_version_id="x",
                   status="finalised", signoff_chain=["staff", "manager"])
    db.add(ap)
    db.commit()
    assert client.post(f"{BASE}/appraisals/{ap.id}/reopen", json={"reason": "my own"}, headers=w.h["hr1"]).status_code == 403
    assert client.post(f"{BASE}/appraisals/{ap.id}/reopen", json={"reason": "by admin"}, headers=w.h["admin1"]).status_code == 200


# ---- scope, IDOR, lists ----------------------------------------------------------------------------------------------------------------


def test_appraisals_are_visible_only_within_scope_with_a_uniform_404(client, w):
    aid = start(client, w)
    for who in ("lo1", "bm1", "gm", "hr1", "admin1", "ceo"):
        assert get(client, w, who, aid).status_code == 200, who
    for who in ("lo2", "lo3", "bm2"):
        hit, miss = get(client, w, who, aid), get(client, w, who, "nope")
        assert hit.status_code == miss.status_code == 404 and hit.json() == miss.json(), who
    bodies = {"/form": {}, "/submit": None, "/sign": {}, "/acknowledge": None, "/pdf": None, "/disputes": {"reason": "I disagree with this"}}
    for path, body in bodies.items():
        for who in ("lo2", "bm2"):
            method = "GET" if path == "/pdf" else "PUT" if path == "/form" else "POST"
            r = client.request(method, f"{BASE}/appraisals/{aid}{path}", json=body, headers=w.h[who])
            assert r.status_code == 404, (path, who, r.status_code)


def test_the_list_shows_each_viewer_only_their_own_people(client, w):
    for pr in (w.p.lo1, w.p.lo2, w.p.lo3):
        create(client, w, "hr1", pr)
    names = lambda who: {i["profile_name"] for i in client.get(f"{BASE}/appraisals", headers=w.h[who]).json()["items"]}  # noqa: E731
    assert names("lo1") == {"Officer One"} and names("bm1") == {"Officer One", "Officer Two"} and names("bm2") == {"Officer Three"}
    assert len(names("hr1")) == len(names("ceo")) == 3
    assert names("lo2") == {"Officer Two"}
    assert client.get(f"{BASE}/appraisals", params={"status": "bogus"}, headers=w.h["hr1"]).status_code == 422


def test_awaiting_me_lists_only_what_needs_my_action(client, w):
    a1, a2 = start(client, w), start(client, w, "lo2", w.p.lo2)
    to_manager(client, w, a1)
    mine = lambda who: {i["id"] for i in client.get(f"{BASE}/appraisals", params={"awaiting_me": "true"}, headers=w.h[who]).json()["items"]}  # noqa: E731
    assert mine("bm1") == {a1} and mine("lo2") == {a2} and mine("lo1") == set() and mine("hr1") == set()
    rate_manager(client, w, "bm1", a1)
    sign(client, w, "bm1", a1)
    assert mine("gm") == {a1} and mine("bm1") == set()


def test_list_query_count_does_not_grow_with_appraisals(client, db, session_factory, w):
    from sqlalchemy import event

    engine, counter = session_factory.kw["bind"], {"n": 0}
    bump = lambda *a, **k: counter.__setitem__("n", counter["n"] + 1)  # noqa: E731

    def run():
        counter["n"] = 0
        assert client.get(f"{BASE}/appraisals", headers=w.h["hr1"]).status_code == 200
        return counter["n"]

    create(client, w, "hr1", w.p.lo1)
    event.listen(engine, "before_cursor_execute", bump)
    try:
        few = run()
        for i in range(30):
            p = StaffProfile(full_name=f"Extra {i}", performance_role_id=w.role.id, unit_id=w.alpha.id, manager_id=w.p.bm1.id)
            db.add(p)
            db.flush()
            db.add(Appraisal(profile_id=p.id, period_key="2026-Q1", period_start=date(2026, 1, 1), period_end=date(2026, 3, 31), role_id=w.role.id, role_version_id="x", status="in_review", current_step="manager", signoff_chain=["staff", "manager"]))
        db.commit()
        many = run()
    finally:
        event.remove(engine, "before_cursor_execute", bump)
    assert many == few


# ---- audit ----------------------------------------------------------------------------------------------------------------------------


def test_views_and_changes_are_audited_with_ids_only(client, db, w):
    aid = start(client, w)
    form(client, w, "lo1", aid, items=[{"kpi_key": "sales", "self_rating": 4, "self_text": "SECRET-SELF-TEXT"}])
    get(client, w, "bm1", aid)
    get(client, w, "hr1", aid)
    blob = json.dumps([e.details for e in db.query(PerformanceAuditLog)])
    assert "SECRET-SELF-TEXT" not in blob
    views_ = [(e.actor_username, e.entity_id) for e in db.query(PerformanceAuditLog).filter_by(action="appraisal_viewed")]
    assert ("bm1", aid) in views_ and ("hr1", aid) in views_  # who looked at it
    actions = {e.action for e in db.query(PerformanceAuditLog)}
    assert {"appraisal_created", "appraisal_form_saved", "appraisal_viewed"} <= actions


def test_comments_and_disputes_never_reach_the_audit_log(client, db, w):
    aid = start(client, w)
    to_rm_gm(client, w, aid)
    dispute(client, w, "lo1", aid, reason="SECRET-DISPUTE-TEXT here")
    sign(client, w, "gm", aid, "SECRET-COMMENT-TEXT")
    blob = json.dumps([e.details for e in db.query(PerformanceAuditLog)])
    assert "SECRET" not in blob


# ---- PDF -----------------------------------------------------------------------------------------------------------------------------


def pdf_text(data: bytes) -> str:
    import io

    from pypdf import PdfReader

    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)


def test_pdf_mirrors_the_paper_form_and_is_an_attachment(client, w):
    aid = start(client, w)
    to_rm_gm(client, w, aid)
    r = client.get(f"{BASE}/appraisals/{aid}/pdf", headers=w.h["bm1"])
    assert r.status_code == 200 and r.content.startswith(b"%PDF-") and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == f'attachment; filename="appraisal-{aid[:8]}.pdf"' and r.headers["cache-control"] == "no-store"
    text = " ".join(pdf_text(r.content).split())  # header cells wrap onto several lines
    for expected in ("Job Objectives for the Period", "Measurement Criteria", "Manager Assessment", "Part A: Performance Assessment Section", "Assessment Guide", "Part C: Training Requirements", "Officer One", "Q1 2026", "Sales - loan value disbursed", "Rating guide", "Solid quarter", "Immediate manager"):
        assert expected in text, expected


def test_pdf_escapes_markup_in_user_text(client, w):
    aid = start(client, w)
    form(client, w, "lo1", aid, items=[{"kpi_key": "sales", "self_text": "<b>bold</b> & <script>alert(1)</script> <para>"}])
    r = client.get(f"{BASE}/appraisals/{aid}/pdf", headers=w.h["lo1"])
    assert r.status_code == 200 and "<script>alert(1)</script>" in pdf_text(r.content)  # shown literally, never interpreted


def test_the_blank_template_has_the_roles_rows_and_no_names(client, w):
    r = client.get(f"{BASE}/roles/{w.role.id}/appraisal-template.pdf", headers=w.h["lo1"])
    assert r.status_code == 200 and r.headers["content-disposition"] == 'attachment; filename="appraisal-template-loan_officer.pdf"'
    text = " ".join(pdf_text(r.content).split())
    assert "Job Role: Loan Officer" in text and "Status" not in text and "Customers deactivated or balanced off" in text and "Quality (PAR)" in text and "Officer One" not in text
    cra = w.role_of("credit_risk_analyst").id
    assert "Putting customers first" in pdf_text(client.get(f"{BASE}/roles/{cra}/appraisal-template.pdf", headers=w.h["lo1"]).content)
    fresh = client.post(f"{BASE}/roles", json={"name": "Brand New"}, headers=w.h["admin1"]).json()["data"]["id"]
    assert client.get(f"{BASE}/roles/{fresh}/appraisal-template.pdf", headers=w.h["lo1"]).status_code == 404
    assert client.get(f"{BASE}/roles/nope/appraisal-template.pdf", headers=w.h["lo1"]).status_code == 404


# ---- named reviewers can open THAT appraisal, nothing else ---------------------------------------------------------------------------


def test_a_named_reviewer_can_open_the_appraisal_but_not_the_person(client, db, w):
    admin = person(db, w, "ba1", "Office Admin", "branch_admin", access="branch_office_admin", unit=w.alpha, manager=w.p.bm1)
    person(db, w, "csm1", "Support Manager", "customer_service_manager", manager=w.p.gm)
    aid = start(client, w, "ba1", admin)
    assert get(client, w, "csm1", aid).status_code == 200  # the CSM is in this appraisal's chain
    assert client.get(f"{BASE}/staff/{admin.id}", headers=w.h["csm1"]).status_code == 404  # ...but that is all they get
    assert client.get(f"{BASE}/staff/{admin.id}/targets", headers=w.h["csm1"]).status_code == 404
    assert client.get(f"{BASE}/staff/{admin.id}/entries", headers=w.h["csm1"]).status_code == 404
    assert client.get(f"{BASE}/staff/{admin.id}/actions", headers=w.h["csm1"]).status_code == 404
    listed = lambda **p: {i["id"]: i["awaiting_me"] for i in client.get(f"{BASE}/appraisals", params=p, headers=w.h["csm1"]).json()["items"]}  # noqa: E731
    assert listed() == {aid: False}  # visible in the list, not yet their turn
    to_manager(client, w, aid, who="ba1")
    rate_manager(client, w, "bm1", aid)
    sign(client, w, "bm1", aid)
    assert listed(awaiting_me="true") == {aid: True}


def test_other_head_office_people_are_not_reviewers_by_accident(client, db, w):
    admin = person(db, w, "ba1", "Office Admin", "branch_admin", access="branch_office_admin", unit=w.alpha, manager=w.p.bm1)
    person(db, w, "csm1", "Support Manager", "customer_service_manager", manager=w.p.gm)
    person(db, w, "gm2", "Another GM", "general_manager")  # a GM who is NOT this person's reviewer (the real GM exists)
    person(db, w, "fin", "Finance Person", "head_office_staff")
    aid = start(client, w, "ba1", admin)
    for who in ("gm2", "fin", "lo2", "bm2"):
        assert get(client, w, who, aid).status_code == 404, who
        assert client.get(f"{BASE}/appraisals", headers=w.h[who]).json()["items"] == [], who


def test_a_general_manager_covers_for_a_region_with_no_regional_manager(client, db, w):
    person(db, w, "gm2", "Second GM", "general_manager")
    aid = start(client, w, "lo3", w.p.lo3)  # Beta (South): no regional manager and bm2's line goes up to the real GM
    assert get(client, w, "gm", aid).json()["chain"][2]["step"] == "rm_gm"
    to_manager(client, w, aid, who="lo3")
    rate_manager(client, w, "bm2", aid)
    sign(client, w, "bm2", aid)
    assert sign(client, w, "gm2", aid).status_code == 404  # the nearest GM above the manager is the reviewer; not just any GM
    assert sign(client, w, "gm", aid).status_code == 200


def test_an_hr_person_cannot_sign_the_hr_step_of_their_own_appraisal(client, db, w):
    from app.models.admin_user import AdminUser

    hr_user = db.query(AdminUser).filter_by(username="hr1").one()
    mine = StaffProfile(full_name="HR Colleague", performance_role_id=w.role.id, admin_user_id=hr_user.id, unit_id=w.alpha.id, manager_id=w.p.gm.id)
    db.add(mine)
    db.commit()
    add_hr2 = add_user(db, "hr2", "hr")
    w.h["hr2"] = token_headers(add_hr2)
    aid = create(client, w, "hr1", mine).json()["data"]["id"]
    to_manager(client, w, aid, who="hr1")  # their own staff step: allowed
    assert rate_manager(client, w, "gm", aid).status_code == 200 and sign(client, w, "gm", aid).status_code == 200  # manager step
    assert sign(client, w, "gm", aid).status_code == 200  # rm_gm step (the GM is also the nearest GM above)
    assert get(client, w, "hr1", aid).json()["current_step"] == "hr"
    assert sign(client, w, "hr1", aid).status_code == 403  # HR may sign for others, never for themselves
    assert get(client, w, "hr1", aid).json()["permissions"]["can_sign"] is False
    assert sign(client, w, "hr2", aid, "Second HR").json()["data"]["status"] == "finalised"  # another HR person completes it


def test_pdf_labels_are_drawn_in_a_readable_colour_not_white_on_white(client, w):
    """Text extraction can't see colour, so inspect the page's drawing instructions: any text painted white must sit on a filled background."""
    import io
    import re

    from pypdf import PdfReader

    aid = start(client, w)
    data = client.get(f"{BASE}/appraisals/{aid}/pdf", headers=w.h["lo1"]).content
    content = PdfReader(io.BytesIO(data)).pages[0].get_contents().get_data().decode("latin-1")
    # Find each header label and look at the fill colour set immediately before it was drawn.
    for label in ("Name:", "Staff No.:", "Position:", "My Manager:", "Branch/Dept:", "Years/Months Worked:"):
        idx = content.find(f"({label})")
        assert idx > 0, label
        colour = re.findall(r"([\d.]+) ([\d.]+) ([\d.]+) rg", content[:idx])[-1]
        assert tuple(float(c) for c in colour) != (1.0, 1.0, 1.0), f"{label} is drawn in white"
