"""Period boundaries use Africa/Nairobi. Kenya has no daylight saving (fixed UTC+3), so a fixed
offset is exact and, unlike zoneinfo, needs no tz database on slim containers."""

from datetime import date, datetime, timedelta, timezone

NAIROBI = timezone(timedelta(hours=3), name="Africa/Nairobi")


def today_nairobi() -> date:
    return datetime.now(NAIROBI).date()
