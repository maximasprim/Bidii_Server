"""
Staff Performance - pure scoring rules.

No database, no framework, no I/O: every function takes plain values and
returns plain values, so each rule is unit-testable in isolation. Money and
percentages are Decimal throughout, never float.

Rules implemented here (the single source of truth for them):

* Achievement % = actual / target x 100. For "lower is better" KPIs (e.g. PAR)
  it is inverted: target / actual x 100.
* A zero, negative or missing target means "not scored": the KPI is excluded
  and reported as such - never a divide-by-zero, never a fake 0 or 100.
* Performance Index (PI) = achievement % capped at an admin-set cap (default
  150) and floored at 0.
* Qualitative 1-5 ratings convert to PI through an editable map.
* Composite = sum(weight x PI) re-normalised over the KPIs actually scored.
* Roll-ups (team/branch/region/org) are sum(actual) / sum(target) over members
  that have both - NEVER an average of percentages.
* Category lookups use the score rounded half-up to 2 decimals, so what a
  person sees on screen is what gets classified.
* Category and rating boundaries are INCLUSIVE at the lower bound: a score of
  exactly 120 reaches the band whose minimum is 120.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

Number = int | float | Decimal | str

DEFAULT_CAP_PCT = Decimal("150")
DEFAULT_RATING_TO_PI: dict[str, int] = {"5": 125, "4": 110, "3": 90, "2": 70, "1": 50}
LEGACY_BANDS = (">100%", "80%-99%", "60%-79%", "below 60%")

_HUNDRED = Decimal("100")
_ZERO = Decimal("0")
_CENT = Decimal("0.01")


def to_decimal(value: Number) -> Decimal:
    """Decimal from int/str/float/Decimal. Floats go through str() so 0.1 stays 0.1."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def round2(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# One KPI
# --------------------------------------------------------------------------


def achievement_pct(actual: Number | None, target: Number | None, direction: str = "higher") -> Decimal | None:
    """
    Achievement % for one KPI, or None when it cannot be scored (missing
    actual, or a missing/zero/negative target).

    direction="lower" (PAR etc.): target / actual x 100. An actual of 0 is the
    best possible outcome and returns Decimal("Infinity"); feed the result to
    performance_index(), which caps it.
    """
    if actual is None or target is None:
        return None
    a, t = to_decimal(actual), to_decimal(target)
    if t <= 0:
        return None
    if a < 0:
        a = _ZERO
    if direction == "lower":
        if a == 0:
            return Decimal("Infinity")
        return t / a * _HUNDRED
    if direction != "higher":
        raise ValueError("direction must be 'higher' or 'lower'.")
    return a / t * _HUNDRED


def performance_index(pct: Decimal | None, cap_pct: Number = DEFAULT_CAP_PCT) -> Decimal | None:
    """Achievement % capped at `cap_pct` and floored at 0. None stays None (not scored)."""
    if pct is None:
        return None
    cap = to_decimal(cap_pct)
    if cap <= 0:
        raise ValueError("The achievement cap must be greater than 0.")
    return max(_ZERO, min(pct, cap))


def kpi_index(
    actual: Number | None, target: Number | None, direction: str = "higher", cap_pct: Number = DEFAULT_CAP_PCT
) -> Decimal | None:
    """Convenience: achievement then cap, in one call."""
    return performance_index(achievement_pct(actual, target, direction), cap_pct)


def rating_to_pi(rating: int | None, table: Mapping[str, Number] | None = None) -> Decimal | None:
    """Converts a 1-5 rating to a Performance Index via the editable map (default 5->125 ... 1->50)."""
    if rating is None:
        return None
    if int(rating) != rating or not 1 <= int(rating) <= 5:
        raise ValueError("A rating must be a whole number from 1 to 5.")
    mapping = table if table is not None else DEFAULT_RATING_TO_PI
    return to_decimal(mapping[str(int(rating))])


# --------------------------------------------------------------------------
# Composite
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CompositeResult:
    score: Decimal | None  # None when nothing could be scored
    scored_weight: Decimal
    total_weight: Decimal
    excluded: int  # KPIs left out because they could not be scored

    @property
    def coverage_pct(self) -> Decimal | None:
        """Share of the total weight that was actually scored (for 'based on N% of KPIs' notes)."""
        if self.total_weight <= 0:
            return None
        return self.scored_weight / self.total_weight * _HUNDRED


def composite_score(items: Iterable[tuple[Number, Decimal | None]]) -> CompositeResult:
    """
    items = (weight, PI or None). Returns sum(w x PI) / sum(w), taken over the
    items with a PI only ("re-normalised"): an unscored KPI neither drags the
    score down nor is silently treated as 100.
    """
    total = scored = weighted = _ZERO
    excluded = 0
    for weight, pi in items:
        w = to_decimal(weight)
        if w < 0:
            raise ValueError("A KPI weight can't be negative.")
        total += w
        if pi is None:
            excluded += 1
            continue
        scored += w
        weighted += w * pi
    score = weighted / scored if scored > 0 else None
    return CompositeResult(score=score, scored_weight=scored, total_weight=total, excluded=excluded)


def blend_with_competencies(
    kpi_score: Decimal | None, competency_score: Decimal | None, competency_share_pct: Number = 20
) -> Decimal | None:
    """
    Final score = KPIs x (100 - share)% + competencies x share%. If only one
    side has a score it is returned as-is (re-normalised, same principle as
    composite_score); if neither does, None.
    """
    share = to_decimal(competency_share_pct)
    if not _ZERO <= share <= _HUNDRED:
        raise ValueError("The competency share must be between 0 and 100.")
    if kpi_score is None and competency_score is None:
        return None
    if kpi_score is None:
        return competency_score
    if competency_score is None:
        return kpi_score
    return (kpi_score * (_HUNDRED - share) + competency_score * share) / _HUNDRED


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def classify_score(score: Decimal | None, categories: Sequence[Mapping[str, Any]]) -> dict[str, str] | None:
    """
    Maps a score to a category. `categories` is ordered highest -> lowest with
    an inclusive `min_score`. The score is rounded half-up to 2 decimals first.
    """
    if score is None:
        return None
    s = round2(score)
    for cat in categories:
        if s >= to_decimal(cat["min_score"]):
            return {"key": cat["key"], "label": cat["label"]}
    last = categories[-1]
    return {"key": last["key"], "label": last["label"]}


def rating_for_achievement(pct: Decimal | None, bands: Sequence[Mapping[str, Any]]) -> int | None:
    """Achievement % -> 1-5 appraisal rating (bands ordered 5 -> 1, inclusive lower bound)."""
    if pct is None:
        return None
    p = round2(pct) if pct.is_finite() else pct
    for band in bands:
        if p >= to_decimal(band["min_pct"]):
            return int(band["rating"])
    return int(bands[-1]["rating"])


def legacy_band(pct: Decimal | None) -> str | None:
    """
    The four staff-count bands their JAN-OCT sheets use. Labels are taken
    literally: ">100%" is strictly above 100, so exactly 100 falls in "80%-99%".
    """
    if pct is None:
        return None
    p = round2(pct) if pct.is_finite() else pct
    if p > _HUNDRED:
        return LEGACY_BANDS[0]
    if p >= 80:
        return LEGACY_BANDS[1]
    if p >= 60:
        return LEGACY_BANDS[2]
    return LEGACY_BANDS[3]


# --------------------------------------------------------------------------
# Roll-ups and distributions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RollupResult:
    pct: Decimal | None  # sum(actual) / sum(target) x 100; None if no member could be scored
    actual_total: Decimal
    target_total: Decimal
    scored: int
    excluded_no_target: int  # members with no usable target
    unreported: int  # members with a target but nothing entered yet


def rollup(members: Iterable[tuple[Number | None, Number | None]]) -> RollupResult:
    """
    Team / branch / region / organisation achievement from (actual, target)
    pairs: sum(actual) / sum(target). Never an average of percentages. A member
    needs BOTH a target > 0 and an actual to count; the others are reported
    separately so the gap is visible rather than silently zero.
    """
    actual_total = target_total = _ZERO
    scored = no_target = unreported = 0
    for actual, target in members:
        if target is None or to_decimal(target) <= 0:
            no_target += 1
            continue
        if actual is None:
            unreported += 1
            continue
        actual_total += max(_ZERO, to_decimal(actual))
        target_total += to_decimal(target)
        scored += 1
    pct = actual_total / target_total * _HUNDRED if target_total > 0 else None
    return RollupResult(pct, actual_total, target_total, scored, no_target, unreported)


def distribution(scores: Iterable[Decimal | None], categories: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """
    Where a group of staff land: counts per category AND per legacy band (the
    table their JAN-OCT / H1 sheets build by hand), plus the average score.
    Unscored people are counted separately, not as Below.
    """
    counts = {c["key"]: 0 for c in categories}
    legacy = {b: 0 for b in LEGACY_BANDS}
    scored_values: list[Decimal] = []
    unscored = 0
    for score in scores:
        if score is None:
            unscored += 1
            continue
        scored_values.append(score)
        counts[classify_score(score, categories)["key"]] += 1  # type: ignore[index]
        legacy[legacy_band(score)] += 1  # type: ignore[index]
    average = sum(scored_values, _ZERO) / len(scored_values) if scored_values else None
    return {
        "counts": counts,
        "legacy": legacy,
        "scored": len(scored_values),
        "unscored": unscored,
        "average": average,
    }


# --------------------------------------------------------------------------
# New hires, self vs manager
# --------------------------------------------------------------------------


def _months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month)


def ramp_target(target: Number | None, hire_date: date | None, month: date, ramp_months: int) -> Decimal | None:
    """
    Pro-rated target for a new hire. In the hire month the target is
    1/(ramp+1) of full, rising in equal steps to 100% after `ramp_months`
    further months. Months before the hire month have no target (None).
    With ramp_months=0, or no hire date, the full target applies.
    """
    if target is None:
        return None
    t = to_decimal(target)
    if hire_date is None or ramp_months <= 0:
        return t
    elapsed = _months_between(hire_date.replace(day=1), month.replace(day=1))
    if elapsed < 0:
        return None
    if elapsed >= ramp_months:
        return t
    return t * Decimal(elapsed + 1) / Decimal(ramp_months + 1)


def final_rating(
    self_rating: int | None, manager_rating: int | None, authority: str = "manager"
) -> Decimal | None:
    """
    The rating that counts. "manager" (default): the manager's rating, and
    nothing until the manager has rated. "average": the mean of both, needing
    both. The gap is always available via rating_gap().
    """
    if authority == "manager":
        return Decimal(manager_rating) if manager_rating is not None else None
    if authority == "average":
        if self_rating is None or manager_rating is None:
            return None
        return (Decimal(self_rating) + Decimal(manager_rating)) / 2
    raise ValueError("authority must be 'manager' or 'average'.")


def rating_gap(self_rating: int | None, manager_rating: int | None) -> int | None:
    """Self minus manager (positive = the person rated themselves higher). None if either is missing."""
    if self_rating is None or manager_rating is None:
        return None
    return int(self_rating) - int(manager_rating)


def weighted_rating(pairs: Iterable[tuple[Number, Decimal | int | None]]) -> Decimal | None:
    """
    The appraisal form's overall 1-5 rating: sum(weight x rating) / sum(weight), taken over the rows that HAVE a
    rating (re-normalised, like composite_score). None when nothing is rated.
    """
    total = weighted = _ZERO
    for weight, rating in pairs:
        if rating is None:
            continue
        w = to_decimal(weight)
        if w < 0:
            raise ValueError("A KPI weight can't be negative.")
        total += w
        weighted += w * to_decimal(rating)
    return weighted / total if total > 0 else None
