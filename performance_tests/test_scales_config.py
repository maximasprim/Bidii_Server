"""Effective-dated rating scales, org config + history, legacy-threshold migration, seeding."""

import copy
from datetime import date

import pytest

from app.models.performance import (
    PerformanceConfig,
    PerformanceConfigHistory,
    PerformanceRole,
    RatingScale,
)
from app.services import performance_config as cfg
from app.services import performance_scales as sc


# ---- seed ------------------------------------------------------------------------


def test_seed_is_the_confirmed_120_90_60(db):
    sc.ensure_seeded(db)
    row = sc.scale_as_of(db)
    assert [c["min_score"] for c in row.categories] == [120, 90, 60, 0]
    assert [c["label"] for c in row.categories] == [
        "Above Expectation", "Meeting Expectation", "Approaching Expectation", "Below Expectation"]
    assert [b["min_pct"] for b in row.rating_bands] == [120, 100, 80, 60, 0]
    assert row.rating_to_pi == {"5": 125, "4": 110, "3": 90, "2": 70, "1": 50}


def test_seed_is_idempotent(db):
    sc.ensure_seeded(db)
    sc.ensure_seeded(db)
    assert db.query(RatingScale).count() == 1


# ---- effective dating ---------------------------------------------------------------


def _new_cats(above=125, meeting=95, approaching=65):
    cats = copy.deepcopy(sc.SEED_CATEGORIES)
    for c, v in zip(cats, (above, meeting, approaching, 0)):
        c["min_score"] = v
    return cats


def _make(db, when, **kw):
    return sc.create_scale(
        db, effective_from=when, categories=_new_cats(**kw), rating_bands=sc.SEED_RATING_BANDS,
        rating_to_pi=sc.SEED_RATING_TO_PI, note=None, username="tester")


def test_period_uses_the_scale_in_force_then_not_the_latest(db):
    sc.ensure_seeded(db)
    _make(db, date(2027, 1, 1), above=130)
    q4_2026 = sc.scale_for_period(db, date(2026, 12, 31))
    q1_2027 = sc.scale_for_period(db, date(2027, 3, 31))
    assert q4_2026.categories[0]["min_score"] == 120  # history never silently changes
    assert q1_2027.categories[0]["min_score"] == 130


def test_scale_takes_effect_on_its_first_day(db):
    sc.ensure_seeded(db)
    _make(db, date(2027, 1, 1), above=130)
    assert sc.scale_as_of(db, date(2026, 12, 31)).categories[0]["min_score"] == 120
    assert sc.scale_as_of(db, date(2027, 1, 1)).categories[0]["min_score"] == 130


def test_cannot_backdate_or_duplicate_an_effective_date(db):
    sc.ensure_seeded(db)
    _make(db, date(2027, 1, 1), above=130)
    for bad in (date(2026, 6, 1), date(2027, 1, 1), date(2000, 1, 1)):
        with pytest.raises(ValueError):
            _make(db, bad, above=140)
    assert db.query(RatingScale).count() == 2  # nothing was overwritten


def test_date_before_all_scales_falls_back_to_earliest(db):
    sc.ensure_seeded(db)
    assert sc.scale_as_of(db, date(1999, 1, 1)) is not None


# ---- validation ---------------------------------------------------------------------


def _cats_with(**changes):
    cats = copy.deepcopy(sc.SEED_CATEGORIES)
    for idx, val in changes.items():
        cats[int(idx[1:])]["min_score"] = val
    return cats


@pytest.mark.parametrize(
    "bad",
    [
        _cats_with(c1=125),  # meeting above 'above'
        _cats_with(c1=120),  # equal, not strictly decreasing
        _cats_with(c3=5),  # lowest must be 0
        sc.SEED_CATEGORIES[:3],  # wrong count
        list(reversed(sc.SEED_CATEGORIES)),  # wrong order
        "nonsense",
        [{**c, "label": ""} for c in sc.SEED_CATEGORIES],
        [{**c, "min_score": "120"} for c in sc.SEED_CATEGORIES],
        [{**c, "min_score": True} for c in sc.SEED_CATEGORIES],
    ],
)
def test_bad_categories_rejected(bad):
    with pytest.raises(ValueError):
        sc.validate_categories(bad)


@pytest.mark.parametrize(
    "bad",
    [
        {"1": 50, "2": 70, "3": 90, "4": 110},  # missing 5
        {"1": 50, "2": 70, "3": 90, "4": 110, "5": 110},  # not strictly increasing
        {"1": 50, "2": 70, "3": 90, "4": 110, "5": 999},  # out of range
        {"1": 50, "2": 70, "3": 90, "4": 110, "5": "x"},
        [],
    ],
)
def test_bad_rating_to_pi_rejected(bad):
    with pytest.raises(ValueError):
        sc.validate_rating_to_pi(bad)


def test_bad_rating_bands_rejected():
    bands = copy.deepcopy(sc.SEED_RATING_BANDS)
    bands[4]["min_pct"] = 10
    with pytest.raises(ValueError):
        sc.validate_rating_bands(bands)
    bands = copy.deepcopy(sc.SEED_RATING_BANDS)
    bands[1]["min_pct"] = 130
    with pytest.raises(ValueError):
        sc.validate_rating_bands(bands)


# ---- preview ------------------------------------------------------------------------


def test_preview_shows_who_would_move(db):
    sc.ensure_seeded(db)
    current = sc.scale_as_of(db).categories
    result = sc.preview_distribution([125, 118, 92, 88, 70, 40], current, _new_cats(above=130, meeting=95, approaching=75))
    # Under 130/95/75: 125 above->meeting, 92 meeting->approaching, 70 approaching->below (3 move).
    assert result["total"] == 6
    assert result["before"]["above_expectation"] == 1 and result["after"]["above_expectation"] == 0
    assert result["moved"] == 3
    assert sum(result["after"].values()) == 6


def test_preview_rejects_invalid_candidate(db):
    sc.ensure_seeded(db)
    with pytest.raises(ValueError):
        sc.preview_distribution([100], sc.SEED_CATEGORIES, _cats_with(c1=130))


# ---- org config + history ---------------------------------------------------------------


def test_default_config_has_no_thresholds_and_validates():
    assert "categories" not in cfg.DEFAULT_CONFIG and "rating_bands" not in cfg.DEFAULT_CONFIG
    out = cfg.validate_config({})
    assert out["achievement_cap_pct"] == 150 and out["competency_share_pct"] == 20 and out["retention_years"] == 7


@pytest.mark.parametrize(
    "patch",
    [
        {"fiscal_year_start_month": 13}, {"sales_entry_window_days": -1}, {"ramp_up_months": 99},
        {"retention_years": 0}, {"achievement_cap_pct": 99}, {"achievement_cap_pct": 301},
        {"competency_share_pct": 51}, {"competency_share_pct": -1}, {"show_peer_rankings": "yes"},
        {"rating_authority": "boss"}, {"early_warning": {"consecutive_months_below": 0}},
        {"fiscal_year_start_month": True},
    ],
)
def test_bad_config_rejected(patch):
    with pytest.raises(ValueError):
        cfg.validate_config(patch)


def test_unknown_keys_dropped():
    assert "evil" not in cfg.validate_config({"evil": 1})


def test_save_bumps_version_and_keeps_history(db):
    cfg.ensure_defaults(db)
    _, v1 = cfg.get_org_config(db)
    _, v2 = cfg.save_org_config(db, {"achievement_cap_pct": 160}, "tester")
    assert v2 == v1 + 1
    versions = {h.version: h for h in db.query(PerformanceConfigHistory).filter_by(key="org")}
    assert versions[v1].value["achievement_cap_pct"] == 150  # old values are kept
    assert versions[v2].value["achievement_cap_pct"] == 160 and versions[v2].changed_by == "tester"


def test_role_overrides_layer_over_org_and_bad_ones_are_ignored(db):
    cfg.ensure_defaults(db)
    role = db.query(PerformanceRole).filter_by(key="loan_officer").first()
    role.config_overrides = {"ramp_up_months": 3}
    assert cfg.effective_config(db, role)["ramp_up_months"] == 3
    role.config_overrides = {"ramp_up_months": 500}
    assert cfg.effective_config(db, role)["ramp_up_months"] == 0


# ---- seeding ------------------------------------------------------------------------------


def test_ensure_defaults_seeds_roles_once(db):
    cfg.ensure_defaults(db)
    cfg.ensure_defaults(db)
    keys = {r.key for r in db.query(PerformanceRole)}
    assert keys == {k for k, *_ in cfg.SEED_ROLES}
    assert db.query(PerformanceRole).count() == len(cfg.SEED_ROLES)
    assert {"check_off_agent", "debt_recovery", "head_office_staff", "branch_admin"} <= keys


# ---- legacy (Phase 1) threshold migration ---------------------------------------------------


def _phase1_config(min_scores):
    names = ["above_expectation", "meeting_expectation", "approaching_expectation", "below_expectation"]
    labels = ["Above Expectation", "Meeting Expectation", "Approaching Expectation", "Below Expectation"]
    value = copy.deepcopy(cfg.DEFAULT_CONFIG)
    value["categories"] = [{"key": k, "label": l, "min_score": m} for k, l, m in zip(names, labels, min_scores)]
    value["rating_bands"] = copy.deepcopy(sc.SEED_RATING_BANDS)
    return value


def test_untouched_phase1_defaults_are_dropped_and_confirmed_seed_applies(db):
    db.add(PerformanceConfig(key="org", value=_phase1_config([110, 90, 70, 0]), version=3))
    db.commit()
    cfg.ensure_defaults(db)
    row = db.get(PerformanceConfig, "org")
    assert "categories" not in row.value and "rating_bands" not in row.value
    assert row.version == 4
    assert [c["min_score"] for c in sc.scale_as_of(db).categories] == [120, 90, 60, 0]


def test_customised_phase1_thresholds_are_preserved_as_the_first_scale(db):
    db.add(PerformanceConfig(key="org", value=_phase1_config([100, 80, 50, 0]), version=2))
    db.commit()
    cfg.ensure_defaults(db)
    assert [c["min_score"] for c in sc.scale_as_of(db).categories] == [100, 80, 50, 0]
    assert db.query(RatingScale).count() == 1


def test_legacy_migration_is_idempotent(db):
    db.add(PerformanceConfig(key="org", value=_phase1_config([110, 90, 70, 0]), version=1))
    db.commit()
    cfg.ensure_defaults(db)
    version_after_first = db.get(PerformanceConfig, "org").version
    cfg.ensure_defaults(db)
    cfg.ensure_defaults(db)
    assert db.get(PerformanceConfig, "org").version == version_after_first
    assert db.query(RatingScale).count() == 1
