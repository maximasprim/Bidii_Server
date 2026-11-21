"""
Recommendations from fixed rules - no database, no AI, fully explainable. Each result carries the figures that triggered it.

`person_recommendations(facts)` takes a plain dict (see `Facts` below) so every rule is unit-tested without a database.
Suggestions only: nothing here changes a rating, a status or a PIP. Rules about a PIP are `restricted` (HR, the direct manager, the person).
Thresholds come from the early-warning settings: consecutive months below, the below-threshold %, and the appraisal-overdue days.
"""

from __future__ import annotations

from datetime import date
from typing import Any, TypedDict


class Facts(TypedDict, total=False):
    status: str  # active | probation | pip
    pcts: list[float | None]  # monthly achievement %, oldest first (None = not scored)
    sales: list[float | None]  # monthly sales actuals, oldest first
    quality: list[float | None]  # monthly PAR %, lower is better
    probation_end: date | None
    probation_decided: bool
    appraisal_overdue_days: int | None
    pip_ends_on: date | None
    pip_review_on: date | None
    today: date
    early_warning: dict[str, Any]


def _clean(xs: list[float | None]) -> list[float]:
    return [x for x in xs if x is not None]


def trailing_below(pcts: list[float | None], threshold: float) -> int:
    """How many of the most recent SCORED months, in a row, were below the threshold."""
    run = 0
    for p in reversed(_clean(pcts)):
        if p < threshold:
            run += 1
        else:
            break
    return run


def trailing_at_least(pcts: list[float | None], level: float) -> int:
    run = 0
    for p in reversed(_clean(pcts)):
        if p >= level:
            run += 1
        else:
            break
    return run


def rising(xs: list[float | None], n: int = 3) -> bool:
    """Strictly rising over the last n SCORED points."""
    v = _clean(xs)[-n:]
    return len(v) == n and all(b > a for a, b in zip(v, v[1:]))


def _rec(rule: str, severity: str, title: str, detail: str, figures: dict[str, Any], *, restricted: bool = False, audience: tuple[str, ...] = ("manager", "hr")) -> dict[str, Any]:
    return {"rule_key": rule, "severity": severity, "title": title, "detail": detail, "figures": figures, "restricted": restricted, "audience": list(audience)}


def person_recommendations(f: Facts) -> list[dict[str, Any]]:
    ew = f.get("early_warning") or {}
    threshold = float(ew.get("below_threshold_pct", 70))
    need = int(ew.get("consecutive_months_below", 2))
    overdue_limit = int(ew.get("appraisal_overdue_days", 30))
    status, pcts, today = f.get("status", "active"), f.get("pcts", []), f.get("today") or date.today()
    out: list[dict[str, Any]] = []

    below = trailing_below(pcts, threshold)
    if status != "pip" and below >= need + 1:
        out.append(_rec("pip_suggested", "urgent", "Consider a performance improvement plan",
                        f"Achievement has been below {threshold:g}% for {below} months in a row and the earlier steps have not turned it around. A PIP is worth discussing with HR.",
                        {"months_below": below, "threshold_pct": threshold}, restricted=True))
    elif status != "pip" and below >= need:
        out.append(_rec("below_threshold_coaching", "action", "Hold a coaching conversation",
                        f"Achievement has been below {threshold:g}% for {below} months in a row. A coaching conversation now is the first step.",
                        {"months_below": below, "threshold_pct": threshold}))

    if status == "pip" and trailing_at_least(pcts, 100) >= 2:
        out.append(_rec("pip_lift", "action", "Consider lifting the PIP",
                        "Achievement has been at or above target for the last 2 months while on a PIP.", {"months_at_or_above_target": trailing_at_least(pcts, 100)}, restricted=True))

    if status == "pip":
        end, review = f.get("pip_ends_on"), f.get("pip_review_on")
        soonest = min([d for d in (end, review) if d is not None], default=None)
        if soonest is not None and (soonest - today).days <= 14:
            days = (soonest - today).days
            when = f"{-days} day(s) overdue" if days < 0 else f"due in {days} day(s)"
            out.append(_rec("pip_expiring", "urgent", "PIP check-in is due", f"The PIP review or end date is {when}. Decide whether to lift, extend or escalate.", {"days_until": days}, restricted=True))

    above = trailing_at_least(pcts, 120)
    if above >= 3 and status != "pip":
        out.append(_rec("sustained_above", "info", "Recognise and stretch", f"Achievement has been at or above 120% for {above} months in a row. Consider recognition or a stretch target.", {"months_at_or_above_120": above}, audience=("manager", "hr", "self")))

    if rising(f.get("quality", []), 3) and rising(f.get("sales", []), 3):
        out.append(_rec("quality_warning", "action", "Sales are rising but portfolio quality is slipping",
                        "PAR has risen three months running while sales have also risen. Review how new loans are being underwritten.", {"par_trend": _clean(f.get("quality", []))[-3:], "sales_trend": _clean(f.get("sales", []))[-3:]}))

    scored = _clean(pcts)
    if len(scored) >= 4 and sum(1 for p in scored[-4:] if p < 60) == 4 and max(scored[-4:]) < 75:
        out.append(_rec("target_review", "action", "Review whether the target is realistic",
                        "The best of the last 4 months reached less than 75% of target and every month was under 60%. Check the target against history before judging the person.", {"best_recent_pct": round(max(scored[-4:]), 1)}))

    pe = f.get("probation_end")
    if pe is not None and not f.get("probation_decided") and (pe - today).days <= 30:
        days = (pe - today).days
        out.append(_rec("probation_decision", "urgent" if days <= 7 else "action", "Probation decision needed",
                        f"Probation ends {'in ' + str(days) + ' day(s)' if days >= 0 else str(-days) + ' day(s) ago'} and no confirm/extend decision is recorded.", {"days_until_end": days}))

    od = f.get("appraisal_overdue_days")
    if od is not None and od > overdue_limit:
        out.append(_rec("appraisal_overdue", "action", "Appraisal is overdue", f"The appraisal is {od} days overdue (limit {overdue_limit}).", {"days_overdue": od}, audience=("manager", "hr", "self")))
    return out


def unit_recommendations(category_counts: dict[str, int], scored: int, unit_name: str) -> list[dict[str, Any]]:
    """Branch-level: when most scored people are Approaching or Below Expectation the branch needs an intervention, not just individual coaching."""
    weak = category_counts.get("approaching_expectation", 0) + category_counts.get("below_expectation", 0)
    if scored >= 3 and weak * 2 >= scored:
        return [_rec("branch_intervention", "action", f"{unit_name}: plan a branch-level intervention",
                     f"{weak} of {scored} scored people are Approaching or Below Expectation. Look at the branch's pipeline, leadership and targets, not only individuals.",
                     {"weak": weak, "scored": scored}, audience=("manager", "hr", "region"))]
    return []
