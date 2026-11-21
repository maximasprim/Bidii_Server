"""
Levels (seniority tiers) within a performance role and their default monthly targets.

Seeded from the HR induction deck:
  * Loan Officer (SME staff): DSE1 600,000 / DSE2 700,000 / SNR DSE 800,000; 30-day PAR below 5%
  * Auto Champion: 1,500,000 / 2,000,000 (senior) / 3,000,000 (BDO); 30-day PAR below 7.5%
  * Check-off Officer: 1,000,000 - from the Check-off sheet (most officers have it), NOT from the deck; to confirm
Defaults are only applied by an explicit bulk action - they never change anyone's target on their own.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.performance import PerformanceLevel, PerformanceRole
from app.services.performance_roles import slugify, targetable_kpis

MAX_TARGET = Decimal("1000000000000")

SEED_LEVELS: dict[str, list[tuple[str, str, int, dict[str, float], str]]] = {
    "loan_officer": [
        ("dse1", "DSE1", 1, {"sales": 600000, "par30": 5}, "SME staff incentive table (HR induction): SME target 600,000; PAR below 5%."),
        ("dse2", "DSE2", 2, {"sales": 700000, "par30": 5}, "SME staff incentive table (HR induction): SME target 700,000; PAR below 5%."),
        ("snr_dse", "SNR DSE", 3, {"sales": 800000, "par30": 5}, "SME staff incentive table (HR induction): SME target 800,000; PAR below 5%."),
    ],
    "auto_champion": [
        ("auto_champion", "Auto Champion", 1, {"sales": 1500000, "par30": 7.5}, "Auto staff table (HR induction): target 1,500,000; PAR below 7.5%."),
        ("senior_auto_champion", "Senior Auto Champion", 2, {"sales": 2000000, "par30": 7.5}, "Auto staff table (HR induction): target 2,000,000; PAR below 7.5%."),
        ("bdo_auto_champion", "BDO Auto Champion", 3, {"sales": 3000000, "par30": 7.5}, "Auto staff table (HR induction): target 3,000,000; PAR below 7.5%."),
    ],
    "check_off_agent": [
        ("check_off_officer", "Check-off Officer", 1, {"disbursement": 1000000}, "From the Check-off sheet (most officers have this target). Not in the induction deck - confirm."),
    ],
}


class LevelError(ValueError):
    pass


def validate_default_targets(db: Session, role_id: str, targets: Any) -> dict[str, float]:
    """Every key must be a targetable KPI of the role's current JD version, every value a number from 0."""
    if not isinstance(targets, dict):
        raise LevelError("Default targets must be a set of KPI keys and numbers.")
    allowed = targetable_kpis(db, role_id)
    cleaned: dict[str, float] = {}
    for key, value in targets.items():
        if key not in allowed:
            raise LevelError(f'"{key}" is not a KPI of this role that can carry a target.')
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise LevelError(f'The default for "{key}" must be a number.')
        try:
            d = Decimal(str(value))
        except Exception:
            raise LevelError(f'The default for "{key}" must be a number.') from None
        if not d.is_finite() or d < 0 or d > MAX_TARGET:
            raise LevelError(f'The default for "{key}" is out of range.')
        cleaned[key] = float(d)
    return cleaned


def seed_levels(db: Session) -> int:
    """Idempotent: creates each seeded level that doesn't exist yet. Never edits one that does."""
    created = 0
    roles = {r.key: r for r in db.query(PerformanceRole).filter(PerformanceRole.key.in_(list(SEED_LEVELS)))}
    for role_key, levels in SEED_LEVELS.items():
        role = roles.get(role_key)
        if role is None:
            continue
        have = {key for (key,) in db.query(PerformanceLevel.key).filter(PerformanceLevel.role_id == role.id)}
        for key, label, rank, targets, note in levels:
            if key in have:
                continue
            cleaned = validate_default_targets(db, role.id, targets)  # a typo'd KPI key fails loudly, never seeds bad data
            db.add(PerformanceLevel(role_id=role.id, key=key, label=label, rank=rank, default_targets=cleaned, note=note))
            created += 1
    if created:
        db.commit()
    return created


def make_key(label: str) -> str:
    return slugify(label)[:40] or "level"
