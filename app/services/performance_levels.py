"""
Levels (seniority tiers) within a performance role and their default monthly targets.

Seeded from the HR induction deck:
  * Loan Officer (SME staff): DSE1 600,000 / DSE2 700,000 / SNR DSE 800,000 / BDO 1,000,000; 30-day PAR below 5%
  * Auto Champion: L1 1,000,000 / L2 1,500,000 / Senior 2,000,000 / BDO 3,000,000; 30-day PAR below 10% (updated by HR)
  * Check-off: L1 1,000,000 / L2 1,500,000 / Senior Check-off Agent 2,000,000 (updated by HR)
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
        ("bdo", "BDO", 4, {"sales": 1000000, "par30": 5}, "Business Development Officer (senior SME sales level, above SNR DSE): target 1,000,000; PAR below 5%."),
    ],
    "auto_champion": [
        ("auto_champion", "Auto Champion Level 1", 1, {"sales": 1000000, "par30": 10}, "Auto Champion Level 1: target 1,000,000; PAR below 10%."),
        ("auto_champion_l2", "Auto Champion Level 2", 2, {"sales": 1500000, "par30": 10}, "Auto Champion Level 2: target 1,500,000; PAR below 10%."),
        ("senior_auto_champion", "Senior Auto Champion", 3, {"sales": 2000000, "par30": 10}, "Senior Auto Champion: target 2,000,000; PAR below 10%."),
        ("bdo_auto_champion", "BDO Auto Champion", 4, {"sales": 3000000, "par30": 10}, "BDO Auto Champion: target 3,000,000; PAR below 10%."),
    ],
    "check_off_agent": [
        ("check_off_officer", "Check-off Officer Level 1", 1, {"disbursement": 1000000}, "Check-off Level 1: target 1,000,000."),
        ("check_off_officer_l2", "Check-off Officer Level 2", 2, {"disbursement": 1500000}, "Check-off Level 2: target 1,500,000."),
        ("senior_check_off_agent", "Senior Check-off Agent", 3, {"disbursement": 2000000}, "Senior Check-off Agent: target 2,000,000."),
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
