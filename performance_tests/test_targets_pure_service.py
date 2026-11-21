"""Target resolution (pure) and the targets service: append-only history, bulk plans, copy-forward, requests."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace as R

import pytest

from app.models.performance import PerformanceLevel, PerformanceRole, PerformanceTarget, StaffProfile
from app.services import performance_targets as tg
from app.services.performance_config import ensure_defaults
from app.services.performance_levels import seed_levels
from app.services.performance_roles import seed_role_templates

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def row(frm, minutes, value):
    return R(effective_from=frm, created_at=T0 + timedelta(minutes=minutes), value=D(value))


# ---- pure: which target is in force ------------------------------------------------------------------------


def test_mid_year_change_applies_from_its_month_only():
    rows = [row(date(2026, 1, 1), 0, 600000), row(date(2026, 6, 1), 1, 700000)]
    pick = lambda m: tg.pick_in_force(rows, m).value  # noqa: E731
    assert pick(date(2026, 1, 1)) == 600000 and pick(date(2026, 5, 31)) == 600000
    assert pick(date(2026, 6, 1)) == 700000 and pick(date(2026, 12, 1)) == 700000


def test_nothing_in_force_before_the_first_row_or_with_no_rows():
    rows = [row(date(2026, 3, 1), 0, 500)]
    assert tg.pick_in_force(rows, date(2026, 2, 28)) is None
    assert tg.pick_in_force([], date(2026, 2, 1)) is None


def test_same_month_ties_go_to_the_latest_created():
    rows = [row(date(2026, 3, 1), 0, 100), row(date(2026, 3, 1), 5, 200)]
    assert tg.pick_in_force(rows, date(2026, 3, 15)).value == 200


def test_a_back_dated_row_does_not_override_a_later_dated_one():
    rows = [row(date(2026, 1, 1), 0, 1), row(date(2026, 6, 1), 1, 6), row(date(2026, 3, 1), 2, 3)]  # March entered last
    pick = lambda m: tg.pick_in_force(rows, m).value  # noqa: E731
    assert [pick(date(2026, m, 1)) for m in (2, 3, 5, 6, 8)] == [1, 3, 3, 6, 6]


def test_first_of_month():
    assert tg.first_of_month(date(2026, 5, 17)) == date(2026, 5, 1)


@pytest.mark.parametrize("v,ok", [(0, True), (600000, True), ("700000.5", True), (D("1.2345"), True), (-1, False), (True, False),
                                  (None, False), ("abc", False), (float("nan"), False), (float("inf"), False), (10**13, False), ([], False)])
def test_value_validation(v, ok):
    if ok:
        assert tg.clean_value(v) >= 0
    else:
        with pytest.raises(tg.TargetError):
            tg.clean_value(v)


@pytest.mark.parametrize("r,ok", [("Contract review", True), ("  ok  ", False), ("", False), (None, False), ("x" * 301, False), ("x" * 300, True)])
def test_reason_is_required(r, ok):
    if ok:
        assert tg.clean_reason(r)
    else:
        with pytest.raises(tg.TargetError):
            tg.clean_reason(r)


# ---- service -------------------------------------------------------------------------------------------------


@pytest.fixture()
def w(db):
    ensure_defaults(db)
    seed_role_templates(db)
    seed_levels(db)
    return db


def person(db, name, role="loan_officer", level=None, active=True):
    r = db.query(PerformanceRole).filter_by(key=role).one()
    lv = db.query(PerformanceLevel).filter_by(role_id=r.id, key=level).one().id if level else None
    p = StaffProfile(full_name=name, performance_role_id=r.id, level_id=lv, is_active=active)
    db.add(p)
    db.commit()
    return p


def count(db):
    return db.query(PerformanceTarget).count()


def test_set_target_is_append_only_and_keeps_history(w):
    p = person(w, "A")
    first = tg.set_target(w, p, "sales", date(2026, 1, 20), 600000, "Level DSE1", "hr")
    assert first.effective_from == date(2026, 1, 1)  # normalised to the month
    assert tg.set_target(w, p, "sales", date(2026, 1, 1), 600000, "same again", "hr") is None  # no-op
    tg.set_target(w, p, "sales", date(2026, 6, 1), 700000, "Contract change", "hr")
    assert count(w) == 2
    assert tg.in_force_map(w, [p.id], date(2026, 5, 1))[(p.id, "sales")].value == 600000  # earlier months untouched
    assert tg.in_force_map(w, [p.id], date(2026, 6, 1))[(p.id, "sales")].value == 700000
    assert [h.reason for h in tg.history(w, p.id)] == ["Contract change", "Level DSE1"]


def test_only_kpis_that_can_carry_a_target_are_accepted(w):
    p = person(w, "A")
    for bad in ("self_character", "nonexistent"):
        with pytest.raises(tg.TargetError):
            tg.set_target(w, p, bad, date(2026, 1, 1), 5, "reason here", "hr")  # rated / unknown KPI
    skeleton = person(w, "B", role="hr_officer")
    with pytest.raises(tg.TargetError, match="no KPIs"):
        tg.set_target(w, skeleton, "anything", date(2026, 1, 1), 5, "reason here", "hr")
    assert count(w) == 0


def test_a_target_of_zero_is_allowed_and_means_not_scored(w):
    p = person(w, "A")
    assert tg.set_target(w, p, "sales", date(2026, 1, 1), 0, "Debt recovery: no target yet", "hr").value == 0


def test_reason_is_enforced_on_every_write(w):
    p = person(w, "A")
    with pytest.raises(tg.TargetError):
        tg.set_target(w, p, "sales", date(2026, 1, 1), 5, "  ", "hr")
    assert count(w) == 0


# ---- bulk plans -------------------------------------------------------------------------------------------------


def test_set_value_plan_marks_create_unchanged_and_skip(w):
    a, b, c = person(w, "A"), person(w, "B"), person(w, "C", role="hr_officer")
    tg.set_target(w, b, "sales", date(2026, 1, 1), 500000, "initial value", "hr")
    items = {i.full_name: i for i in tg.plan_set_value(w, [a, b, c], "sales", 500000, date(2026, 2, 1))}
    assert (items["A"].action, items["B"].action, items["C"].action) == ("create", "unchanged", "skip")
    assert count(w) == 1  # planning writes nothing


def test_level_defaults_plan_and_apply(w):
    d1, d2, none = person(w, "D1", level="dse1"), person(w, "D2", level="dse2"), person(w, "No Level")
    items = tg.plan_level_defaults(w, [d1, d2, none], date(2026, 1, 1), overwrite=False)
    created = {(i.full_name, i.kpi_key): i.new for i in items if i.action == "create"}
    assert created == {("D1", "sales"): 600000, ("D1", "par30"): 5, ("D2", "sales"): 700000, ("D2", "par30"): 5}
    assert [i.note for i in items if i.full_name == "No Level"] == ["No level set"]
    assert tg.apply_plan(w, items, date(2026, 1, 1), "Apply level defaults", "hr", "level_default") == 4
    # running it again changes nothing
    again = tg.plan_level_defaults(w, [d1, d2], date(2026, 1, 1), overwrite=False)
    assert {i.action for i in again} == {"unchanged"}


def test_level_defaults_keep_existing_targets_unless_overwrite(w):
    p = person(w, "P", level="dse1")
    tg.set_target(w, p, "sales", date(2026, 1, 1), 650000, "negotiated", "hr")
    keep = {i.kpi_key: i for i in tg.plan_level_defaults(w, [p], date(2026, 2, 1), overwrite=False)}
    assert keep["sales"].action == "skip" and "kept" in keep["sales"].note and keep["par30"].action == "create"
    over = {i.kpi_key: i for i in tg.plan_level_defaults(w, [p], date(2026, 2, 1), overwrite=True)}
    assert over["sales"].action == "create" and over["sales"].new == 600000


def test_changing_a_levels_defaults_never_changes_existing_targets(w):
    p = person(w, "P", level="dse1")
    tg.apply_plan(w, tg.plan_level_defaults(w, [p], date(2026, 1, 1), False), date(2026, 1, 1), "Level defaults", "hr", "level_default")
    level = w.query(PerformanceLevel).filter_by(key="dse1").one()
    level.default_targets = {"sales": 999999}
    w.commit()
    assert tg.in_force_map(w, [p.id], date(2026, 3, 1))[(p.id, "sales")].value == 600000


def test_copy_forward_with_uplift_rounds_and_skips_unchanged(w):
    a, b, c = person(w, "A"), person(w, "B"), person(w, "C")
    tg.set_target(w, a, "sales", date(2025, 12, 1), 600000, "2025 target", "hr")
    tg.set_target(w, b, "sales", date(2025, 12, 1), "333.33", "2025 target", "hr")
    items = {i.full_name: i for i in tg.plan_copy_forward(w, [a, b, c], date(2025, 12, 1), date(2026, 1, 1), 10)}
    assert items["A"].new == D("660000.00") and items["B"].new == D("366.66")  # 333.33 x 1.10 = 366.663 -> 366.66
    assert items["C"].action == "skip" and "No target" in items["C"].note
    flat = {i.full_name: i for i in tg.plan_copy_forward(w, [a], date(2025, 12, 1), date(2026, 1, 1), 0)}
    assert flat["A"].action == "unchanged"  # targets persist: December's 600,000 is already in force in January


@pytest.mark.parametrize("uplift", [-100, -150, 1001, "x"])
def test_bad_uplift_rejected(w, uplift):
    with pytest.raises(tg.TargetError):
        tg.plan_copy_forward(w, [], date(2025, 12, 1), date(2026, 1, 1), uplift)


def test_apply_is_all_or_nothing_and_needs_a_reason(w):
    a, b = person(w, "A"), person(w, "B")
    items = tg.plan_set_value(w, [a, b], "sales", 1, date(2026, 1, 1))
    with pytest.raises(tg.TargetError):
        tg.apply_plan(w, items, date(2026, 1, 1), "", "hr", "bulk")
    assert count(w) == 0
    assert tg.apply_plan(w, items, date(2026, 1, 1), "Reset", "hr", "bulk") == 2


def test_selection_filters(w):
    a, b = person(w, "A", level="dse1"), person(w, "B", level="dse2")
    gone = person(w, "Gone", active=False)
    role = w.query(PerformanceRole).filter_by(key="loan_officer").one()
    assert {p.full_name for p in tg.select_profiles(w, role_id=role.id)} == {"A", "B"}  # inactive excluded
    assert {p.full_name for p in tg.select_profiles(w, role_id=role.id, include_inactive=True)} == {"A", "B", "Gone"}
    assert {p.full_name for p in tg.select_profiles(w, level_id=a.level_id)} == {"A"}
    assert [p.full_name for p in tg.select_profiles(w, profile_ids=[b.id, gone.id])] == ["B"]
    assert tg.select_profiles(w, profile_ids=[]) == []


# ---- requests -----------------------------------------------------------------------------------------------------


def test_request_lifecycle(w):
    p = person(w, "P")
    req = tg.create_request(w, p, "sales", date(2026, 4, 9), 750000, "Promoted to DSE2", "bm1")
    assert req.status == "pending" and req.effective_from == date(2026, 4, 1)
    with pytest.raises(tg.TargetError, match="already a pending"):
        tg.create_request(w, p, "sales", date(2026, 4, 1), 800000, "dup request", "bm1")
    assert count(w) == 0  # a request changes nothing by itself
    created = tg.decide_request(w, req, True, "hr1", "OK")
    assert created.source == "request" and created.value == 750000 and req.status == "approved" and req.decision_note == "OK"
    with pytest.raises(tg.TargetError, match="already approved"):
        tg.decide_request(w, req, True, "hr1", None)


def test_rejected_and_withdrawn_requests_change_nothing(w):
    p = person(w, "P")
    r1 = tg.create_request(w, p, "sales", date(2026, 4, 1), 1, "reason one", "bm1")
    tg.decide_request(w, r1, False, "hr1", "no")
    r2 = tg.create_request(w, p, "sales", date(2026, 5, 1), 2, "reason two", "bm1")
    tg.withdraw_request(w, r2)
    assert (r1.status, r2.status, count(w)) == ("rejected", "withdrawn", 0)
    with pytest.raises(tg.TargetError):
        tg.withdraw_request(w, r2)


def test_request_must_target_a_real_numeric_kpi(w):
    p = person(w, "P")
    with pytest.raises(tg.TargetError):
        tg.create_request(w, p, "self_character", date(2026, 4, 1), 3, "reason here", "bm1")
