"""
Projections - pure functions, no database, no AI. Every result says which method and which months of data it used.

Inputs are simple lists so the maths can be unit-tested on its own:
  history           [(actual, target), ...]  one pair per month already scored (oldest first, target > 0)
  remaining_targets [target, ...]            the target in force for each month still to come

Method ("blend"): each future month is projected as 50% the recency-weighted run-rate and 50% the linear trend continued,
never below zero. With fewer than MIN_MONTHS of history nothing is projected ("insufficient data").
Probability of each category: a bootstrap that re-draws the person's OWN monthly achievement ratios for the remaining months, many times, and
classifies the resulting period achievement. It is shown as a RANGE rounded to the nearest 5 points, never a single precise number.
The seed is fixed, so the same data always gives the same answer.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from typing import Any

MIN_MONTHS = 3
SIMULATIONS = 2000
SEED = 20260101
Z90 = 1.645


def _ratios(history: Sequence[tuple[float, float]]) -> list[float]:
    return [a / t for a, t in history if t > 0]


def weighted_run_rate(values: Sequence[float]) -> float:
    """Recency-weighted mean: the latest month counts n times, the one before n-1, ... the first once."""
    n = len(values)
    return sum(v * (i + 1) for i, v in enumerate(values)) / (n * (n + 1) / 2)


def linear_trend(values: Sequence[float]) -> tuple[float, float]:
    """(slope, intercept) of a least-squares line through values at x = 0..n-1."""
    n = len(values)
    if n < 2:
        return 0.0, values[0] if values else 0.0
    mx, my = (n - 1) / 2, sum(values) / n
    den = sum((i - mx) ** 2 for i in range(n))
    slope = sum((i - mx) * (v - my) for i, v in enumerate(values)) / den
    return slope, my - slope * mx


def project_ratios(history: Sequence[tuple[float, float]], months_ahead: int) -> list[float]:
    """Projected achievement ratio (1.0 = on target) for each of the next `months_ahead` months."""
    r = _ratios(history)
    if len(r) < MIN_MONTHS:
        return []
    rate = weighted_run_rate(r)
    slope, intercept = linear_trend(r)
    out = []
    for k in range(months_ahead):
        trend = intercept + slope * (len(r) + k)
        out.append(max(0.0, 0.5 * rate + 0.5 * trend))
    return out


def _to_date(history: Sequence[tuple[float, float]], base: tuple[float, float] | None) -> tuple[float, float]:
    """(actual, target) already banked in the period being projected. Defaults to all of `history`; pass `base` when the period is shorter than the history used."""
    return base if base is not None else (sum(a for a, _ in history), sum(t for _, t in history))


def project_period(history: Sequence[tuple[float, float]], remaining_targets: Sequence[float], base: tuple[float, float] | None = None) -> dict[str, Any]:
    """Projected end-of-period achievement: actual so far plus projected months, over the whole period's target."""
    months = len(history)
    meta: dict[str, Any] = {
        "method": "Recency-weighted run-rate blended with the linear trend of the months scored so far.",
        "data_window_months": months, "remaining_months": len(remaining_targets), "min_months": MIN_MONTHS,
    }
    if months < MIN_MONTHS:
        return {**meta, "available": False, "reason": f"Insufficient history for a projection (needs at least {MIN_MONTHS} scored months, has {months})."}
    actual_to_date, target_to_date = _to_date(history, base)
    ratios = project_ratios(history, len(remaining_targets))
    projected_rest = sum(r * t for r, t in zip(ratios, remaining_targets))
    target_total = target_to_date + sum(remaining_targets)
    projected_total = actual_to_date + projected_rest
    return {
        **meta, "available": True, "actual_to_date": round(actual_to_date, 2), "target_to_date": round(target_to_date, 2),
        "target_total": round(target_total, 2), "projected_actual_total": round(projected_total, 2),
        "projected_pct": round(projected_total / target_total * 100, 1) if target_total > 0 else None,
        "projected_monthly_average": round(projected_rest / len(remaining_targets), 2) if remaining_targets else None,
    }


def required_run_rate(history: Sequence[tuple[float, float]], remaining_targets: Sequence[float], goal_pct: float, base: tuple[float, float] | None = None) -> dict[str, Any]:
    """What each remaining month must deliver to finish the period at `goal_pct` of target (e.g. 90 for Meeting Expectation)."""
    if not remaining_targets:
        return {"available": False, "reason": "No months remain in this period."}
    actual_to_date, target_to_date = _to_date(history, base)
    target_total = target_to_date + sum(remaining_targets)
    needed = goal_pct / 100 * target_total - actual_to_date
    per_month = max(0.0, needed) / len(remaining_targets)
    avg_target = sum(remaining_targets) / len(remaining_targets)
    return {
        "available": True, "goal_pct": goal_pct, "remaining_months": len(remaining_targets), "needed_total": round(max(0.0, needed), 2),
        "needed_per_month": round(per_month, 2), "needed_vs_monthly_target_pct": round(per_month / avg_target * 100, 1) if avg_target > 0 else None,
        "already_met": needed <= 0,
    }


def _wilson(k: int, n: int) -> tuple[float, float]:
    p = k / n
    d = 1 + Z90**2 / n
    centre = (p + Z90**2 / (2 * n)) / d
    half = Z90 * math.sqrt(p * (1 - p) / n + Z90**2 / (4 * n**2)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def _round5(x: float) -> int:
    return int(min(100, max(0, round(x * 100 / 5) * 5)))


def category_probabilities(
    history: Sequence[tuple[float, float]], remaining_targets: Sequence[float], categories: Sequence[Mapping[str, Any]], *, runs: int = SIMULATIONS, seed: int = SEED, base: tuple[float, float] | None = None
) -> dict[str, Any]:
    """
    `categories` is ordered highest to lowest with `min_score` (the same list the rating scale uses). Returns, per category, a low-high percentage
    range (90% interval, rounded to 5) of the chance of finishing the period in it.
    """
    r = _ratios(history)
    meta = {"method": f"Bootstrap of the person's own monthly achievement ({runs} simulated finishes, fixed seed); shown as a range.", "data_window_months": len(r), "min_months": MIN_MONTHS}
    if len(r) < MIN_MONTHS:
        return {**meta, "available": False, "reason": f"Insufficient history for a probability (needs at least {MIN_MONTHS} scored months, has {len(r)})."}
    if not remaining_targets:
        return {**meta, "available": False, "reason": "No months remain in this period."}
    rng = random.Random(seed)
    actual0, target0 = _to_date(history, base)
    target_total = target0 + sum(remaining_targets)
    counts = {c["key"]: 0 for c in categories}
    for _ in range(runs):
        total = actual0 + sum(rng.choice(r) * t for t in remaining_targets)
        pct = total / target_total * 100
        for c in categories:
            if pct >= float(c["min_score"]):
                counts[c["key"]] += 1
                break
        else:
            counts[categories[-1]["key"]] += 1
    out = {}
    for c in categories:
        lo, hi = _wilson(counts[c["key"]], runs)
        out[c["key"]] = {"label": c.get("label", c["key"]), "low_pct": _round5(lo), "high_pct": _round5(hi)}
    return {**meta, "available": True, "categories": out, "simulations": runs}


def project_group(members: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Group projection from the members' own projections: sum of projected actuals over sum of targets (never an average of percentages)."""
    usable = [m for m in members if m.get("available")]
    if not usable:
        return {"available": False, "reason": "None of the people in this group has enough history for a projection.", "people_projected": 0, "people": len(members)}
    projected, target = sum(m["projected_actual_total"] for m in usable), sum(m["target_total"] for m in usable)
    return {
        "available": True, "people": len(members), "people_projected": len(usable), "projected_actual_total": round(projected, 2), "target_total": round(target, 2),
        "projected_pct": round(projected / target * 100, 1) if target > 0 else None,
        "method": "Sum of each person's projected finish over the sum of their targets; people without enough history are left out and counted separately.",
    }
