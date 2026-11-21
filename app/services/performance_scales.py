"""
Effective-dated rating scales: category thresholds, achievement->rating bands
and the rating->PI map. Rows are immutable; a change is a NEW row with a later
effective_from. A period is always classified with the scale in force at the
END of that period (see scale_for_period), so history never changes silently.
"""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.performance import RatingScale
from app.services.performance_scoring import classify_score, to_decimal
from app.services.performance_time import today_nairobi

CATEGORY_KEYS = ["above_expectation", "meeting_expectation", "approaching_expectation", "below_expectation"]

# Confirmed seed: anchored on the two cut-points the appraisal form already uses.
SEED_CATEGORIES: list[dict[str, Any]] = [
    {"key": "above_expectation", "label": "Above Expectation", "min_score": 120},
    {"key": "meeting_expectation", "label": "Meeting Expectation", "min_score": 90},
    {"key": "approaching_expectation", "label": "Approaching Expectation", "min_score": 60},
    {"key": "below_expectation", "label": "Below Expectation", "min_score": 0},
]
# The appraisal form's own guide (>120 Outstanding ... <60 Poor), lower bound inclusive.
SEED_RATING_BANDS: list[dict[str, Any]] = [
    {"rating": 5, "label": "Outstanding", "min_pct": 120},
    {"rating": 4, "label": "Excellent", "min_pct": 100},
    {"rating": 3, "label": "Good", "min_pct": 80},
    {"rating": 2, "label": "Average", "min_pct": 60},
    {"rating": 1, "label": "Poor", "min_pct": 0},
]
# Midpoints of the form's bands.
SEED_RATING_TO_PI: dict[str, int] = {"5": 125, "4": 110, "3": 90, "2": 70, "1": 50}
# Earliest possible date, so the seed scale covers every period that exists.
SEED_EFFECTIVE_FROM = date(2000, 1, 1)

# Thresholds Phase 1 shipped with; a stored config still equal to these is migrated silently.
LEGACY_PHASE1_MIN_SCORES = [110, 90, 70, 0]


def validate_categories(categories: Any) -> list[dict[str, Any]]:
    if not isinstance(categories, list) or len(categories) != 4:
        raise ValueError("Exactly four performance categories are required.")
    if [c.get("key") for c in categories] != CATEGORY_KEYS:
        raise ValueError("Categories must be, in order: Above, Meeting, Approaching and Below Expectation.")
    cleaned = []
    for c in categories:
        label = str(c.get("label", "")).strip()
        if not label:
            raise ValueError("Every category needs a label.")
        if isinstance(c.get("min_score"), bool) or not isinstance(c.get("min_score"), (int, float)):
            raise ValueError("Every category needs a numeric minimum score.")
        cleaned.append({"key": c["key"], "label": label, "min_score": c["min_score"]})
    scores = [c["min_score"] for c in cleaned]
    if scores[-1] != 0:
        raise ValueError("The lowest category must start at 0 so every score falls in a category.")
    if any(scores[i] <= scores[i + 1] for i in range(3)):
        raise ValueError("Category minimum scores must strictly decrease from Above to Below Expectation.")
    return cleaned


def validate_rating_bands(bands: Any) -> list[dict[str, Any]]:
    if not isinstance(bands, list) or len(bands) != 5:
        raise ValueError("Exactly five rating bands (ratings 5 down to 1) are required.")
    if [b.get("rating") for b in bands] != [5, 4, 3, 2, 1]:
        raise ValueError("Rating bands must be ordered 5, 4, 3, 2, 1.")
    cleaned = []
    for b in bands:
        label = str(b.get("label", "")).strip()
        if not label:
            raise ValueError("Every rating band needs a label.")
        if isinstance(b.get("min_pct"), bool) or not isinstance(b.get("min_pct"), (int, float)):
            raise ValueError("Every rating band needs a numeric minimum percentage.")
        cleaned.append({"rating": b["rating"], "label": label, "min_pct": b["min_pct"]})
    mins = [b["min_pct"] for b in cleaned]
    if mins[-1] != 0:
        raise ValueError("The lowest rating band must start at 0%.")
    if any(mins[i] <= mins[i + 1] for i in range(4)):
        raise ValueError("Rating band minimums must strictly decrease from rating 5 to rating 1.")
    return cleaned


def validate_rating_to_pi(mapping: Any) -> dict[str, float | int]:
    if not isinstance(mapping, dict) or sorted(mapping) != ["1", "2", "3", "4", "5"]:
        raise ValueError("The rating-to-index map needs exactly the ratings 1, 2, 3, 4 and 5.")
    for k, v in mapping.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 300:
            raise ValueError(f"The index for rating {k} must be a number between 0 and 300.")
    values = [mapping[str(r)] for r in (1, 2, 3, 4, 5)]
    if any(values[i] >= values[i + 1] for i in range(4)):
        raise ValueError("A higher rating must give a higher index (1 < 2 < 3 < 4 < 5).")
    return {k: mapping[k] for k in ("5", "4", "3", "2", "1")}


def validate_scale(categories: Any, rating_bands: Any, rating_to_pi: Any) -> dict[str, Any]:
    return {
        "categories": validate_categories(categories),
        "rating_bands": validate_rating_bands(rating_bands),
        "rating_to_pi": validate_rating_to_pi(rating_to_pi),
    }


def ensure_seeded(db: Session) -> None:
    """Idempotent: writes the confirmed default scale if no scale exists yet."""
    if db.query(RatingScale.id).first() is not None:
        return
    db.add(
        RatingScale(
            effective_from=SEED_EFFECTIVE_FROM,
            categories=copy.deepcopy(SEED_CATEGORIES),
            rating_bands=copy.deepcopy(SEED_RATING_BANDS),
            rating_to_pi=dict(SEED_RATING_TO_PI),
            note="Confirmed defaults",
            created_by="system",
        )
    )
    db.commit()


def scale_as_of(db: Session, as_of: date | None = None) -> RatingScale:
    """The scale in force on `as_of` (default today): the latest one whose effective_from is not after it."""
    ensure_seeded(db)
    when = as_of or today_nairobi()
    row = (
        db.query(RatingScale)
        .filter(RatingScale.effective_from <= when)
        .order_by(RatingScale.effective_from.desc())
        .first()
    )
    # Cannot be None (the seed starts in 2000) unless someone asks about a pre-2000 date.
    return row or db.query(RatingScale).order_by(RatingScale.effective_from.asc()).first()


def scale_for_period(db: Session, period_end: date) -> RatingScale:
    """The scale a closed period must be (re)classified with: the one in force at its last day."""
    return scale_as_of(db, period_end)


def list_scales(db: Session) -> list[RatingScale]:
    ensure_seeded(db)
    return db.query(RatingScale).order_by(RatingScale.effective_from.desc()).all()


def create_scale(
    db: Session,
    *,
    effective_from: date,
    categories: Any,
    rating_bands: Any,
    rating_to_pi: Any,
    note: str | None,
    username: str,
) -> RatingScale:
    """Appends a new scale. It must take effect AFTER every existing one: history is never rewritten."""
    cleaned = validate_scale(categories, rating_bands, rating_to_pi)
    ensure_seeded(db)
    latest = db.query(RatingScale).order_by(RatingScale.effective_from.desc()).first()
    if latest is not None and effective_from <= latest.effective_from:
        raise ValueError(
            f"A new scale must take effect after the current latest one ({latest.effective_from.isoformat()}); "
            "past scales are never edited or back-dated."
        )
    row = RatingScale(
        effective_from=effective_from,
        categories=cleaned["categories"],
        rating_bands=cleaned["rating_bands"],
        rating_to_pi=cleaned["rating_to_pi"],
        note=(note or None),
        created_by=username,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def preview_distribution(
    scores: list[Decimal | float | int | str], current_categories: list[dict[str, Any]], candidate_categories: list[dict[str, Any]]
) -> dict[str, Any]:
    """
    "What would this change do?" - the same scores classified under the current
    and the candidate thresholds. Pure: the caller supplies the scores (the
    dashboards will feed it last quarter's snapshots).
    """
    candidate = validate_categories(candidate_categories)
    before = {k: 0 for k in CATEGORY_KEYS}
    after = {k: 0 for k in CATEGORY_KEYS}
    moved = 0
    for raw in scores:
        score = to_decimal(raw)
        b = classify_score(score, current_categories)["key"]  # type: ignore[index]
        a = classify_score(score, candidate)["key"]  # type: ignore[index]
        before[b] += 1
        after[a] += 1
        if a != b:
            moved += 1
    return {"before": before, "after": after, "moved": moved, "total": len(scores)}
