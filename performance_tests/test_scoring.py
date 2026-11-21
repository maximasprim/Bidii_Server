"""Pure scoring rules: zero targets, inverted KPIs, caps, re-normalisation, roll-ups, every threshold boundary."""

from datetime import date
from decimal import Decimal as D

import pytest

from app.services import performance_scoring as s
from app.services.performance_scales import SEED_CATEGORIES, SEED_RATING_BANDS

CATS = SEED_CATEGORIES
BANDS = SEED_RATING_BANDS


# ---- achievement / index ---------------------------------------------------


def test_achievement_basic_and_decimal_exact():
    assert s.achievement_pct(1_200_000, 1_000_000) == D("120")
    assert isinstance(s.achievement_pct(1, 3), D)
    assert s.to_decimal(0.1) == D("0.1")  # floats never leak binary noise in


@pytest.mark.parametrize("target", [None, 0, -5, "0"])
def test_missing_zero_or_negative_target_is_not_scored(target):
    assert s.achievement_pct(500_000, target) is None
    assert s.kpi_index(500_000, target) is None
    assert s.achievement_pct(500_000, target, "lower") is None


def test_missing_actual_is_not_scored_not_zero():
    assert s.achievement_pct(None, 1_000_000) is None
    assert s.achievement_pct(0, 1_000_000) == D("0")  # a real zero IS scored


def test_negative_actual_is_floored_at_zero():
    assert s.achievement_pct(-100, 1000) == D("0")


def test_lower_is_better_inverts():
    assert s.achievement_pct(5, 10, "lower") == D("200")  # PAR 5 vs target 10: better than target
    assert s.achievement_pct(10, 5, "lower") == D("50")
    assert s.achievement_pct(5, 5, "lower") == D("100")


def test_lower_is_better_zero_actual_is_best_and_capped():
    assert s.achievement_pct(0, 5, "lower") == D("Infinity")
    assert s.kpi_index(0, 5, "lower") == D("150")


def test_bad_direction_rejected():
    with pytest.raises(ValueError):
        s.achievement_pct(1, 1, "sideways")


def test_cap_default_custom_and_floor():
    assert s.performance_index(D("300")) == D("150")
    assert s.performance_index(D("300"), 200) == D("200")
    assert s.performance_index(D("149.99")) == D("149.99")
    assert s.performance_index(D("-5")) == D("0")
    assert s.performance_index(None) is None
    with pytest.raises(ValueError):
        s.performance_index(D("50"), 0)


# ---- 1-5 ratings -------------------------------------------------------------


@pytest.mark.parametrize("rating,pi", [(5, 125), (4, 110), (3, 90), (2, 70), (1, 50)])
def test_default_rating_to_pi(rating, pi):
    assert s.rating_to_pi(rating) == D(pi)


def test_rating_to_pi_custom_map_none_and_invalid():
    assert s.rating_to_pi(3, {"1": 40, "2": 60, "3": 85, "4": 105, "5": 130}) == D("85")
    assert s.rating_to_pi(None) is None
    for bad in (0, 6, 2.5, -1):
        with pytest.raises(ValueError):
            s.rating_to_pi(bad)


# ---- composite ----------------------------------------------------------------


def test_composite_weighted():
    r = s.composite_score([(50, D(100)), (30, D(50)), (20, D(100))])
    assert r.score == D("85") and r.excluded == 0 and r.coverage_pct == D("100")


def test_composite_renormalises_over_scored_only():
    # 20-weight KPI unscored: (50*100 + 30*50) / 80 = 81.25, NOT 65 (zero) and NOT inflated.
    r = s.composite_score([(50, D(100)), (20, None), (30, D(50))])
    assert r.score == D("81.25")
    assert r.excluded == 1 and r.scored_weight == D("80") and r.coverage_pct == D("80")


def test_composite_loan_officer_weights():
    r = s.composite_score([(50, D("104")), (20, D("90")), (20, D("150")), (5, D("125")), (5, D("110"))])
    assert r.score == D("111.75")  # (5200+1800+3000+625+550)/100


def test_composite_nothing_scored_is_none_not_zero():
    assert s.composite_score([(50, None), (50, None)]).score is None
    assert s.composite_score([]).score is None
    assert s.composite_score([(0, D(100))]).score is None  # zero total weight


def test_composite_negative_weight_rejected():
    with pytest.raises(ValueError):
        s.composite_score([(-1, D(100))])


def test_blend_default_80_20():
    assert s.blend_with_competencies(D("100"), D("50")) == D("90")  # 80 + 10


def test_blend_one_side_missing_and_bounds():
    assert s.blend_with_competencies(D("100"), None) == D("100")
    assert s.blend_with_competencies(None, D("70")) == D("70")
    assert s.blend_with_competencies(None, None) is None
    assert s.blend_with_competencies(D("100"), D("0"), 0) == D("100")
    assert s.blend_with_competencies(D("100"), D("0"), 100) == D("0")
    with pytest.raises(ValueError):
        s.blend_with_competencies(D("1"), D("1"), 101)


# ---- classification boundaries (every threshold) -------------------------------


@pytest.mark.parametrize(
    "score,key",
    [
        ("200", "above_expectation"), ("120", "above_expectation"), ("119.99", "meeting_expectation"),
        ("90", "meeting_expectation"), ("89.99", "approaching_expectation"),
        ("60", "approaching_expectation"), ("59.99", "below_expectation"),
        ("0", "below_expectation"), ("-5", "below_expectation"),
    ],
)
def test_category_boundaries(score, key):
    assert s.classify_score(D(score), CATS)["key"] == key


def test_classification_uses_the_displayed_two_decimal_value():
    assert s.classify_score(D("119.995"), CATS)["key"] == "above_expectation"  # displays as 120.00
    assert s.classify_score(D("119.994"), CATS)["key"] == "meeting_expectation"  # displays as 119.99
    assert s.classify_score(D("89.995"), CATS)["key"] == "meeting_expectation"


def test_classify_none_is_none():
    assert s.classify_score(None, CATS) is None


@pytest.mark.parametrize(
    "pct,rating",
    [("150", 5), ("120", 5), ("119.99", 4), ("100", 4), ("99.99", 3), ("80", 3), ("79.99", 2), ("60", 2), ("59.99", 1), ("0", 1)],
)
def test_rating_band_boundaries(pct, rating):
    assert s.rating_for_achievement(D(pct), BANDS) == rating


def test_rating_for_infinity_and_none():
    assert s.rating_for_achievement(D("Infinity"), BANDS) == 5
    assert s.rating_for_achievement(None, BANDS) is None


@pytest.mark.parametrize(
    "pct,band",
    [("100.01", ">100%"), ("100", "80%-99%"), ("80", "80%-99%"), ("79.99", "60%-79%"), ("60", "60%-79%"), ("59.99", "below 60%"), ("0", "below 60%")],
)
def test_legacy_band_boundaries(pct, band):
    assert s.legacy_band(D(pct)) == band


# ---- roll-ups --------------------------------------------------------------------


def test_rollup_is_sum_over_sum_not_average_of_percentages():
    r = s.rollup([(1_000_000, 1_000_000), (0, 100_000)])
    # average of 100% and 0% would be 50%; the true team figure is 1.0m / 1.1m.
    assert r.pct.quantize(D("0.01")) == D("90.91")
    assert r.actual_total == D("1000000") and r.target_total == D("1100000") and r.scored == 2


def test_rollup_weights_by_target():
    r = s.rollup([(900_000, 1_000_000), (200_000, 100_000)])  # 90% and 200%
    assert r.pct.quantize(D("0.01")) == D("100.00")  # not the 145% a naive average gives


def test_rollup_reports_gaps_instead_of_hiding_them():
    r = s.rollup([(500, 1000), (None, 1000), (700, None), (700, 0), (None, None)])
    assert r.scored == 1 and r.unreported == 1 and r.excluded_no_target == 3
    assert r.pct == D("50")


def test_rollup_empty_or_unscorable_is_none():
    assert s.rollup([]).pct is None
    assert s.rollup([(5, 0), (None, None)]).pct is None


def test_distribution_counts_average_and_unscored():
    d = s.distribution([D("130"), D("95"), D("65"), D("40"), None, D("105")], CATS)
    assert d["counts"] == {"above_expectation": 1, "meeting_expectation": 2, "approaching_expectation": 1, "below_expectation": 1}
    assert d["legacy"] == {">100%": 2, "80%-99%": 1, "60%-79%": 1, "below 60%": 1}
    assert d["scored"] == 5 and d["unscored"] == 1
    assert d["average"] == D("87")  # (130+95+65+40+105)/5


def test_distribution_empty():
    d = s.distribution([], CATS)
    assert d["average"] is None and d["scored"] == 0 and sum(d["counts"].values()) == 0


# ---- new hires, self vs manager -----------------------------------------------------


def test_ramp_target_steps():
    hire = date(2026, 3, 15)
    assert s.ramp_target(1000, hire, date(2026, 2, 1), 3) is None  # before hire
    assert s.ramp_target(1000, hire, date(2026, 3, 1), 3) == D("250")  # 1/4
    assert s.ramp_target(1000, hire, date(2026, 4, 1), 3) == D("500")
    assert s.ramp_target(1000, hire, date(2026, 5, 1), 3) == D("750")
    assert s.ramp_target(1000, hire, date(2026, 6, 1), 3) == D("1000")  # ramp over


def test_ramp_disabled_or_no_hire_date_or_no_target():
    assert s.ramp_target(1000, date(2026, 3, 1), date(2026, 3, 1), 0) == D("1000")
    assert s.ramp_target(1000, None, date(2026, 3, 1), 3) == D("1000")
    assert s.ramp_target(None, date(2026, 3, 1), date(2026, 3, 1), 3) is None


def test_final_rating_rules_and_gap():
    assert s.final_rating(5, 3) == D("3")  # manager is authoritative
    assert s.final_rating(5, None) is None  # nothing until the manager rates
    assert s.final_rating(5, 3, "average") == D("4")
    assert s.final_rating(5, None, "average") is None
    with pytest.raises(ValueError):
        s.final_rating(1, 1, "boss")
    assert s.rating_gap(5, 3) == 2 and s.rating_gap(2, 4) == -2 and s.rating_gap(None, 4) is None


# ---- appraisal form rating ------------------------------------------------------------------------------------------


def test_weighted_rating_is_sum_over_weights_and_renormalises_over_rated_rows():
    assert s.weighted_rating([(50, 4), (20, 3), (30, 5)]) == D("4.1")  # (200+60+150)/100
    assert s.weighted_rating([(50, 4), (50, None)]) == D("4")  # unrated rows neither drag it down nor count as 5
    assert s.weighted_rating([(50, None)]) is None and s.weighted_rating([]) is None
    assert s.weighted_rating([(20, D("3.5")), (80, 4)]) == D("3.9")
    with pytest.raises(ValueError):
        s.weighted_rating([(-1, 3)])
