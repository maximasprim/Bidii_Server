"""Phase 4 - projection maths (pure)."""

import pytest

from app.services import performance_projection as pj

CATS = [{"key": "above", "label": "Above", "min_score": 120}, {"key": "meeting", "label": "Meeting", "min_score": 90}, {"key": "approaching", "label": "Approaching", "min_score": 60}, {"key": "below", "label": "Below", "min_score": 0}]


def test_run_rate_weights_recent_months_more():
    assert pj.weighted_run_rate([1, 1, 1]) == 1
    assert pj.weighted_run_rate([0, 0, 6]) == 3  # (6*3)/6
    assert pj.weighted_run_rate([6, 0, 0]) == 1


def test_trend_slope_and_flat():
    s, i = pj.linear_trend([1, 2, 3, 4])
    assert round(s, 6) == 1 and round(i, 6) == 1
    assert pj.linear_trend([5, 5, 5]) == (0.0, 5.0)
    assert pj.linear_trend([7]) == (0.0, 7)


def test_insufficient_history_is_not_projected():
    r = pj.project_period([(100, 100), (90, 100)], [100] * 4)
    assert r["available"] is False and "Insufficient history" in r["reason"]
    assert pj.category_probabilities([(100, 100)] * 2, [100], CATS)["available"] is False


def test_steady_performer_projects_on_run_rate():
    r = pj.project_period([(90, 100)] * 4, [100] * 8)
    assert r["available"] and r["projected_pct"] == 90.0 and r["target_total"] == 1200 and r["data_window_months"] == 4


def test_improving_trend_beats_flat_average_and_never_goes_negative():
    up = pj.project_period([(60, 100), (80, 100), (100, 100)], [100] * 3)
    flat = pj.project_period([(80, 100)] * 3, [100] * 3)
    assert up["projected_pct"] > flat["projected_pct"]
    down = pj.project_period([(100, 100), (40, 100), (10, 100)], [100] * 12)
    assert down["projected_actual_total"] >= down["actual_to_date"]


def test_required_run_rate():
    r = pj.required_run_rate([(50, 100), (50, 100)], [100] * 2, 90)
    assert r["needed_total"] == 260 and r["needed_per_month"] == 130 and r["needed_vs_monthly_target_pct"] == 130 and not r["already_met"]
    done = pj.required_run_rate([(200, 100), (200, 100)], [100], 90)
    assert done["already_met"] and done["needed_per_month"] == 0
    assert pj.required_run_rate([(1, 1)], [], 90)["available"] is False


def test_probabilities_are_ranges_rounded_to_five_and_deterministic():
    hist = [(80, 100), (95, 100), (110, 100), (70, 100), (100, 100)]
    a = pj.category_probabilities(hist, [100] * 7, CATS)
    b = pj.category_probabilities(hist, [100] * 7, CATS)
    assert a == b and a["available"]
    for v in a["categories"].values():
        assert v["low_pct"] % 5 == 0 and v["high_pct"] % 5 == 0 and v["low_pct"] <= v["high_pct"]
    assert a["categories"]["above"]["high_pct"] < 20


def test_certain_outcome_has_high_range_and_others_near_zero():
    r = pj.category_probabilities([(100, 100)] * 4, [100] * 4, CATS)
    assert r["categories"]["meeting"]["low_pct"] >= 90 and r["categories"]["below"]["high_pct"] <= 5


def test_group_projection_is_sum_over_sum():
    a = pj.project_period([(100, 100)] * 3, [100] * 3)
    b = pj.project_period([(50, 1000)] * 3, [1000] * 3)
    g = pj.project_group([a, b, {"available": False}])
    assert g["people_projected"] == 2 and g["people"] == 3
    assert g["projected_pct"] == round((a["projected_actual_total"] + b["projected_actual_total"]) / (a["target_total"] + b["target_total"]) * 100, 1)
    assert pj.project_group([{"available": False}])["available"] is False


def test_base_overrides_period_to_date_but_not_the_ratio_history():
    hist = [(100, 100)] * 6  # six good months of history...
    q = pj.project_period(hist, [100], base=(200, 200))  # ...but only two of them are in this quarter
    assert q["target_total"] == 300 and q["projected_pct"] == 100.0 and q["actual_to_date"] == 200
    assert pj.required_run_rate(hist, [100], 90, base=(100, 200))["needed_total"] == 170
    p = pj.category_probabilities(hist, [100], CATS, base=(200, 200))
    assert p["categories"]["meeting"]["low_pct"] >= 90
