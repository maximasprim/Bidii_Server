"""
Outlook series for one person's chart - pure functions, no database, no AI.

Three lines, all as monthly achievement % of target (100 = on target):
  actual     the scored months so far
  projected  the next HORIZON months from the existing projection method (performance_projection.project_ratios), with a 90% range
  plan       the path NEEDED to reach a goal (default: Meeting Expectation) in `plan_months`, a straight line from the latest scored month.
             It is arithmetic, not a forecast: it says what would have to happen, never what the AI thinks will happen.

A SAVED plan (performance_plans) is a frozen copy of the `plan` path; attach_saved_plan() draws it and evaluate_plan() compares later months with it.

The range is the person's own month-to-month swing (sample standard deviation of their achievement ratios), the same idea as the
bootstrap range used elsewhere. With fewer than MIN_MONTHS scored months nothing is projected; the plan needs only one scored month.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from app.services import performance_projection as pj

HORIZON = 3
MAX_PLAN_MONTHS = 6


def _add_months(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    idx = y * 12 + (m - 1) + k
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _sd(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def _r1(x: float) -> float:
    return round(x, 1)


def default_goal(categories: Sequence[Mapping[str, Any]]) -> float:
    return next((float(c["min_score"]) for c in categories if c.get("key") == "meeting_expectation"), 90.0)


def build_outlook(trend: Sequence[Mapping[str, Any]], categories: Sequence[Mapping[str, Any]], goal_pct: float | None = None, plan_months: int = 3, horizon: int = HORIZON) -> dict[str, Any]:
    """`trend` is the person's report trend: [{month 'YYYY-MM', actual, target, pct, state}, ...]."""
    plan_months = max(1, min(MAX_PLAN_MONTHS, int(plan_months)))
    rows = sorted(trend, key=lambda t: t["month"])
    scored = [t for t in rows if t.get("state") == "scored" and t.get("actual") is not None and t.get("target")]
    goal_options = [{"key": c["key"], "label": c.get("label", c["key"]), "pct": float(c["min_score"])} for c in categories if float(c["min_score"]) > 0]
    base: dict[str, Any] = {"horizon_months": horizon, "plan_months": plan_months, "goal_options": goal_options}
    if not scored:
        return {**base, "available": False, "reason": "No scored months yet, so there is nothing to chart.", "points": [], "projection": {"available": False}, "plan": {"available": False}}

    history = [(t["actual"], t["target"]) for t in scored]
    ratios = [a / t for a, t in history]
    last = scored[-1]["month"]
    last_pct = _r1(ratios[-1] * 100)
    by_month: dict[str, dict[str, Any]] = {}
    for t in rows:
        by_month[t["month"]] = {"month": t["month"], "actual": _r1(t["pct"]) if t.get("state") == "scored" and t.get("pct") is not None else None, "state": t.get("state"), "projected": None, "low": None, "high": None, "plan": None, "saved_plan": None}
    future = [_add_months(last, k) for k in range(1, max(horizon, plan_months) + 1)]
    for m in future:
        by_month.setdefault(m, {"month": m, "actual": None, "state": "future", "projected": None, "low": None, "high": None, "plan": None, "saved_plan": None})

    projection: dict[str, Any] = {"available": False, "min_months": pj.MIN_MONTHS, "data_window_months": len(history)}
    proj = pj.project_ratios(history, horizon)
    if proj:
        half = pj.Z90 * _sd(ratios) * math.sqrt(1 + 1 / len(ratios)) * 100
        by_month[last].update(projected=last_pct, low=last_pct, high=last_pct)  # joins the dashed line to the last real point
        for m, r in zip(future[:horizon], proj):
            p = r * 100
            by_month[m].update(projected=_r1(p), low=_r1(max(0.0, p - half)), high=_r1(p + half))
        projection = {**projection, "available": True, "range_pct_points": _r1(half),
                      "method": "Recency-weighted run-rate blended with the linear trend of the scored months; the shaded band is the person's own month-to-month swing (90% range)."}
    else:
        projection["reason"] = f"Insufficient history for a projection (needs at least {pj.MIN_MONTHS} scored months, has {len(history)})."

    goal = float(goal_pct) if goal_pct is not None else default_goal(categories)
    plan: dict[str, Any] = {"available": False, "goal_pct": _r1(goal), "months": plan_months, "from_pct": last_pct}
    goal_label = next((o["label"] for o in goal_options if abs(o["pct"] - goal) < 1e-9), None)
    if goal_label:
        plan["goal_label"] = goal_label
    if last_pct >= goal:
        plan["reason"] = f"The latest month ({last_pct}%) is already at or above the {_r1(goal)}% goal."
    else:
        step = (goal - last_pct) / plan_months
        by_month[last]["plan"] = last_pct
        for k, m in enumerate(future[:plan_months], start=1):
            by_month[m]["plan"] = _r1(last_pct + step * k)
        plan = {**plan, "available": True, "step_pct_points": _r1(step),
                "note": "The path needed to reach the goal. It shows what would have to happen; it is not a prediction."}
    return {**base, "available": True, "last_scored_month": last, "months_scored": len(history), "points": [by_month[m] for m in sorted(by_month)], "projection": projection, "plan": plan}


# ---- saved plans -----------------------------------------------------------------------------------------------------------


def plan_path(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The path to freeze: [{"month", "pct"}], first item is the start month. Empty when the result has no plan."""
    return [{"month": p["month"], "pct": p["plan"]} for p in result.get("points", []) if p.get("plan") is not None]


def actuals_by_month(result: Mapping[str, Any]) -> dict[str, float]:
    return {p["month"]: float(p["actual"]) for p in result.get("points", []) if p.get("actual") is not None}


def evaluate_plan(path: Sequence[Mapping[str, Any]], goal_pct: float, actuals: Mapping[str, float]) -> dict[str, Any]:
    """
    How the months scored since a plan was saved compare with its path. Pure comparison, no judgement beyond ahead/behind:
      awaiting           no month after the start has been scored yet
      on_track / behind  the latest scored month is at or above / below what the path asked for that month
      reached            the latest scored month is at or above the goal
      ended_not_reached  the path's last month (or a later one) is scored and the goal was not reached
    """
    if len(path) < 2:
        return {"state": "awaiting", "goal_pct": goal_pct, "rows": [], "latest": None}
    start_month = path[0]["month"]
    final_month = path[-1]["month"]
    planned = {p["month"]: float(p["pct"]) for p in path[1:]}
    rows = []
    for m, want in planned.items():
        got = actuals.get(m)
        rows.append({"month": m, "planned": _r1(want), "actual": _r1(got) if got is not None else None, "gap": _r1(got - want) if got is not None else None})
    after = sorted(m for m in actuals if m > start_month)
    if not after:
        return {"state": "awaiting", "goal_pct": goal_pct, "rows": rows, "latest": None}
    last = after[-1]
    got = float(actuals[last])
    want = planned.get(last, float(path[-1]["pct"]))
    latest = {"month": last, "actual": _r1(got), "planned": _r1(want), "gap": _r1(got - want)}
    if got >= goal_pct:
        state = "reached"
    elif last >= final_month:
        state = "ended_not_reached"
    else:
        state = "on_track" if got >= want else "behind"
    return {"state": state, "goal_pct": goal_pct, "rows": rows, "latest": latest}


def attach_saved_plan(result: dict[str, Any], path: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Returns a copy of an outlook result with the saved path drawn on its points (months not yet on the chart are added)."""
    points = {p["month"]: dict(p) for p in result.get("points", [])}
    for item in path:
        row = points.setdefault(item["month"], {"month": item["month"], "actual": None, "state": "planned", "projected": None, "low": None, "high": None, "plan": None, "saved_plan": None})
        row["saved_plan"] = _r1(float(item["pct"]))
    return {**result, "points": [points[m] for m in sorted(points)]}
