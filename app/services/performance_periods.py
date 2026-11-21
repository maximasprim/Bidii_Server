"""
Reporting periods: a month (2026-03), a quarter (2026-Q1), a half-year (2026-H1) or a whole year (2026).
Pure and calendar-based (fiscal-year start is applied in the dashboards phase). Appraisals use the same keys.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date


class PeriodError(ValueError):
    pass


@dataclass(frozen=True)
class Period:
    key: str
    kind: str  # month | quarter | half | year
    start: date
    end: date
    label: str


_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_QUARTER = re.compile(r"^(\d{4})-Q([1-4])$", re.IGNORECASE)
_HALF = re.compile(r"^(\d{4})-H([12])$", re.IGNORECASE)
_YEAR = re.compile(r"^(\d{4})$")


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def parse_period(key: str) -> Period:
    text = (key or "").strip()
    if m := _MONTH.match(text):
        y, mo = int(m[1]), int(m[2])
        start = date(y, mo, 1)
        return Period(f"{y}-{mo:02d}", "month", start, month_end(start), start.strftime("%B %Y"))
    if m := _QUARTER.match(text):
        y, q = int(m[1]), int(m[2])
        start = date(y, 3 * q - 2, 1)
        return Period(f"{y}-Q{q}", "quarter", start, month_end(date(y, 3 * q, 1)), f"Q{q} {y}")
    if m := _HALF.match(text):
        y, h = int(m[1]), int(m[2])
        return Period(f"{y}-H{h}", "half", date(y, 1 if h == 1 else 7, 1), month_end(date(y, 6 if h == 1 else 12, 1)), f"H{h} {y}")
    if m := _YEAR.match(text):
        y = int(m[1])
        return Period(str(y), "year", date(y, 1, 1), date(y, 12, 31), f"Full year {y}")
    raise PeriodError("Use a period like 2026-03, 2026-Q1, 2026-H1 or 2026.")


def months_in(period: Period) -> list[date]:
    out, cur = [], period.start
    while cur <= period.end:
        out.append(cur)
        cur = date(cur.year + (cur.month // 12), cur.month % 12 + 1, 1)
    return out


def following_months(period: Period, count: int) -> list[date]:
    """The `count` months AFTER the period ends (for the form's 'objectives for the next 3 months')."""
    out, cur = [], date(period.end.year + (period.end.month // 12), period.end.month % 12 + 1, 1)
    for _ in range(count):
        out.append(cur)
        cur = date(cur.year + (cur.month // 12), cur.month % 12 + 1, 1)
    return out
