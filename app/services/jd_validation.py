"""
Pure helpers for checking and tidying the formal Job Description content
(app/schemas/job_description.py) without ever refusing existing data.

Everything here is ADVISORY by default: it returns warnings for a human to
act on and never raises, so JDs that were saved before these checks existed
keep loading, saving and rendering exactly as they did. The only strict
behaviour is opt-in (JD_ENFORCE_TOTAL_100 in config) and is applied by the
save endpoint, not here.

The "% of Time" total of 100 is the same rule the Staff Performance module
enforces on role JDs (app/services/performance_roles.py::validate_jd), so a
JD that is clean here can be imported there without surprises.
"""

from __future__ import annotations

from typing import Any

MIN_PURPOSE_CHARS = 20


def total_pct_time(key_responsibilities: list[dict[str, Any]]) -> int:
    return sum(int(item.get("pct_time", 0) or 0) for item in key_responsibilities)


def normalise_pct_time(key_responsibilities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Returns copies of the responsibilities with pct_time rescaled to total
    exactly 100 using largest-remainder rounding (so relative weights are
    preserved and no value goes negative). If every pct_time is 0 the list is
    returned unchanged - there is nothing to scale from, and inventing an even
    split would be a guess a human should make.
    """
    items = [dict(item) for item in key_responsibilities]
    total = total_pct_time(items)
    if not items or total == 100 or total <= 0:
        return items

    raw = [(int(item.get("pct_time", 0) or 0) * 100) / total for item in items]
    floors = [int(value) for value in raw]
    shortfall = 100 - sum(floors)
    # Hand the leftover points to the largest fractional parts first.
    order = sorted(range(len(items)), key=lambda i: raw[i] - floors[i], reverse=True)
    for i in order[:shortfall]:
        floors[i] += 1
    for item, value in zip(items, floors):
        item["pct_time"] = value
    return items


def jd_warnings(content: dict[str, Any]) -> list[str]:
    """Human-readable problems with a JD's content. Empty list means 'clean'."""
    warnings: list[str] = []
    resps = content.get("key_responsibilities") or []

    total = total_pct_time(resps)
    if resps and total != 100:
        warnings.append(f"Key responsibilities add up to {total}% of time; they should total 100%.")

    seen: set[str] = set()
    for index, item in enumerate(resps, start=1):
        heading = (item.get("heading") or "").strip()
        label = f'"{heading}"' if heading else f"#{index}"
        if heading.lower() in seen:
            warnings.append(f"Two responsibilities are both called {label}.")
        seen.add(heading.lower())
        if not item.get("bullets"):
            warnings.append(f"Responsibility {label} has no duties listed.")
        if not item.get("criteria"):
            warnings.append(f"Responsibility {label} has no performance measurement criteria.")
        if int(item.get("pct_time", 0) or 0) == 0:
            warnings.append(f"Responsibility {label} has 0% of time.")

    if len((content.get("overall_role_purpose") or "").strip()) < MIN_PURPOSE_CHARS:
        warnings.append("The overall role purpose is very short or missing.")
    if not (content.get("reports_to") or "").strip():
        warnings.append("Reports-to is not set.")
    if not (content.get("grade") or "").strip():
        warnings.append("Grade is not set.")
    if not content.get("minimum_qualifications"):
        warnings.append("No minimum qualifications are listed.")
    return warnings
