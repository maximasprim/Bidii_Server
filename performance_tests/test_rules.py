"""Phase 4 - rules engine (pure)."""

from datetime import date, timedelta

from app.services import performance_rules as r

T = date(2026, 6, 15)
EW = {"consecutive_months_below": 2, "below_threshold_pct": 70, "appraisal_overdue_days": 30}


def keys(**f):
    return {x["rule_key"] for x in r.person_recommendations({"today": T, "early_warning": EW, "status": "active", **f})}


def test_helpers_ignore_unscored_months():
    assert r.trailing_below([50, None, 60, None], 70) == 2
    assert r.trailing_below([50, 90], 70) == 0
    assert r.trailing_at_least([None, 100, 130], 100) == 2
    assert r.rising([1, None, 2, 3]) and not r.rising([1, 3, 2]) and not r.rising([1, 2])


def test_coaching_then_pip_ladder():
    assert keys(pcts=[90, 69, 65]) == {"below_threshold_coaching"}
    assert keys(pcts=[90, 69]) == set()  # only one month below so far
    assert keys(pcts=[90, 69.9, 40, 30]) == {"pip_suggested"}
    assert keys(pcts=[70, 65]) == set()  # exactly at the threshold is not below
    assert keys(pcts=[65]) == set()


def test_pip_suggestion_is_restricted_and_coaching_is_not():
    rec = r.person_recommendations({"today": T, "early_warning": EW, "pcts": [10, 10, 10]})[0]
    assert rec["restricted"] and rec["rule_key"] == "pip_suggested"
    rec = r.person_recommendations({"today": T, "early_warning": EW, "pcts": [10, 10]})[0]
    assert not rec["restricted"]


def test_no_new_pip_while_on_a_pip_and_lift_after_two_good_months():
    assert keys(status="pip", pcts=[10, 10, 10]) == set()
    assert keys(status="pip", pcts=[40, 100, 110]) == {"pip_lift"}
    assert keys(status="pip", pcts=[40, 100]) == set()


def test_pip_expiring_window():
    assert keys(status="pip", pcts=[50], pip_ends_on=T + timedelta(days=10)) == {"pip_expiring"}
    assert keys(status="pip", pcts=[50], pip_review_on=T - timedelta(days=3)) == {"pip_expiring"}
    assert keys(status="pip", pcts=[50], pip_ends_on=T + timedelta(days=40)) == set()
    assert keys(status="active", pcts=[50], pip_ends_on=T) == set()


def test_recognition_after_three_months_at_120():
    assert keys(pcts=[121, 130, 120]) == {"sustained_above"}
    assert keys(pcts=[121, 119, 130]) == set()


def test_quality_warning_needs_both_series_rising():
    assert keys(sales=[1, 2, 3], quality=[2, 3, 4]) == {"quality_warning"}
    assert keys(sales=[1, 2, 3], quality=[4, 3, 2]) == set()
    assert keys(sales=[3, 2, 1], quality=[2, 3, 4]) == set()


def test_target_review_when_even_the_best_month_is_far_below():
    assert "target_review" in keys(pcts=[50, 55, 40, 45])
    assert "target_review" not in keys(pcts=[50, 55, 40, 80])
    assert "target_review" not in keys(pcts=[50, 55, 45])


def test_probation_and_appraisal():
    assert keys(probation_end=T + timedelta(days=5), probation_decided=False) == {"probation_decision"}
    assert keys(probation_end=T + timedelta(days=5), probation_decided=True) == set()
    assert keys(probation_end=T + timedelta(days=60), probation_decided=False) == set()
    assert keys(probation_end=T - timedelta(days=2)) == {"probation_decision"}
    assert keys(appraisal_overdue_days=31) == {"appraisal_overdue"} and keys(appraisal_overdue_days=30) == set()


def test_branch_intervention():
    c = {"above_expectation": 0, "meeting_expectation": 1, "approaching_expectation": 2, "below_expectation": 1}
    assert r.unit_recommendations(c, 4, "Alpha")[0]["rule_key"] == "branch_intervention"
    assert r.unit_recommendations({"meeting_expectation": 3, "below_expectation": 1}, 4, "Alpha") == []
    assert r.unit_recommendations({"below_expectation": 2}, 2, "Alpha") == []  # too few to judge a branch
