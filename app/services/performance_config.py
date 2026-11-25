"""
Staff Performance - org settings, role seeding, audit logging.

Everything numeric the business may want to tune lives in ONE settings
document editable from the dashboard (never hard-coded in scoring logic).
Category thresholds and rating maps are NOT part of it any more: they are
effective-dated rows in rating_scales (see performance_scales.py).
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services import performance_scales

logger = logging.getLogger("bidii.performance")

DEFAULT_CONFIG: dict[str, Any] = {
    "fiscal_year_start_month": 1,  # 1 = calendar year
    "sales_entry_window_days": 10,  # days after month end an officer may still enter/edit
    "ramp_up_months": 0,  # new-hire months with pro-rated targets
    "show_peer_rankings": False,  # may loan officers see peer league tables?
    "rating_authority": "manager",  # "manager" | "average" - which rating is final
    "achievement_cap_pct": 150,  # a KPI's achievement is capped here before weighting
    "competency_share_pct": 20,  # share of the final score from behavioural competencies
    "retention_years": 7,  # how long performance records are kept
    "early_warning": {
        "consecutive_months_below": 2,
        "below_threshold_pct": 70,
        "appraisal_overdue_days": 14,
    },
}

# (key, name, department, kind). Admins can edit, deactivate or add more;
# seeded ones are is_system so they can't be hard-deleted. KPI sets and JDs for
# them arrive with the JD builder (a later phase) - these are just the roles.
SEED_ROLES: list[tuple[str, str, str, str]] = [
    ("loan_officer", "Loan Officer", "Commercial", "sales"),
    ("credit_risk_analyst", "Credit Risk Analyst", "Commercial", "appraisal"),
    ("branch_manager", "Branch Manager", "Commercial", "hybrid"),
    ("branch_admin", "Branch Admin", "Commercial", "appraisal"),
    ("check_off_agent", "Check-off Agent", "Commercial", "sales"),
    ("debt_recovery", "Debt Recovery", "Commercial", "hybrid"),
    ("hr_officer", "HR Officer", "Human Resources", "appraisal"),
    ("marketing_manager", "Marketing Manager", "Marketing", "appraisal"),
    ("head_office_staff", "Head Office Staff", "Head Office", "appraisal"),
    # From the organisation structure in the HR induction deck.
    ("sales_team_leader", "Sales Team Leader", "Commercial", "hybrid"),
    ("regional_manager", "Regional Manager", "Commercial", "hybrid"),
    ("customer_service_manager", "Customer Service Manager", "Operations", "appraisal"),
    ("general_manager", "General Manager", "Operations", "appraisal"),
    ("ceo", "Chief Executive Officer", "Executive", "appraisal"),
    ("auto_champion", "Auto Champion", "Commercial", "sales"),
]

STAFF_STATUSES = ["active", "probation", "pip", "exited"]
ROLE_KINDS = ["sales", "appraisal", "hybrid"]
UNIT_KINDS = ["branch", "team", "head_office"]
# Action types, and which of them are restricted (HR + direct manager + the person only).
ACTION_TYPES = [
    "talk", "coaching", "verbal_warning", "first_written_warning", "final_warning", "pip", "pip_extended",
    "pip_lifted", "demotion", "dismissal", "probation_confirm", "probation_extend", "promotion", "transfer",
    "exit", "recognition",
]
# The company's disciplinary ladder (HR induction: verbal warning, first written warning, final warning,
# demotion or dismissal) plus PIPs. Visible only to HR, the direct manager and the person concerned.
RESTRICTED_ACTION_TYPES = {
    "verbal_warning", "first_written_warning", "final_warning", "pip", "pip_extended", "pip_lifted",
    "demotion", "dismissal",
}


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Cleaned copy of `cfg` (unknown keys dropped, missing ones defaulted) or ValueError with an admin-actionable message."""
    merged = copy.deepcopy(DEFAULT_CONFIG)
    for key in DEFAULT_CONFIG:
        if key in cfg and cfg[key] is not None:
            merged[key] = copy.deepcopy(cfg[key])

    ranges = (
        ("fiscal_year_start_month", 1, 12, "Fiscal year start month must be a whole number from 1 to 12."),
        ("sales_entry_window_days", 0, 90, "Sales entry window must be between 0 and 90 days."),
        ("ramp_up_months", 0, 12, "Ramp-up months must be between 0 and 12."),
        ("retention_years", 1, 50, "Retention must be between 1 and 50 years."),
    )
    for key, lo, hi, message in ranges:
        if not _whole(merged[key]) or not lo <= merged[key] <= hi:
            raise ValueError(message)

    cap = merged["achievement_cap_pct"]
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not 100 <= cap <= 300:
        raise ValueError("The achievement cap must be between 100% and 300%.")
    share = merged["competency_share_pct"]
    if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 <= share <= 50:
        raise ValueError("The competency share must be between 0% and 50%.")

    if not isinstance(merged["show_peer_rankings"], bool):
        raise ValueError("show_peer_rankings must be true or false.")
    if merged["rating_authority"] not in ("manager", "average"):
        raise ValueError("rating_authority must be 'manager' or 'average'.")

    ew = merged["early_warning"]
    if not isinstance(ew, dict):
        raise ValueError("early_warning must be an object.")
    merged["early_warning"] = {**DEFAULT_CONFIG["early_warning"], **ew}
    for k, lo, hi in (
        ("consecutive_months_below", 1, 12),
        ("below_threshold_pct", 1, 200),
        ("appraisal_overdue_days", 0, 120),
    ):
        v = merged["early_warning"][k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"early_warning.{k} must be between {lo} and {hi}.")
    return merged


def _write_history(db: Session, row, username: str | None) -> None:
    from app.models.performance import PerformanceConfigHistory

    db.add(
        PerformanceConfigHistory(
            key=row.key, version=row.version, value=copy.deepcopy(row.value), changed_by=username
        )
    )


def _migrate_legacy_thresholds(db: Session, row) -> bool:
    """
    Phase 1 stored categories/rating_bands inside the config document. They now
    live in rating_scales. If the stored thresholds are still exactly Phase 1's
    untouched defaults they are simply dropped (the confirmed seed applies);
    anything an admin actually customised becomes the first scale, so no choice
    is lost. Returns True if the document changed.
    """
    value = dict(row.value or {})
    if "categories" not in value and "rating_bands" not in value:
        return False
    categories = value.pop("categories", None)
    bands = value.pop("rating_bands", None)
    is_phase1_default = (
        isinstance(categories, list)
        and [c.get("min_score") for c in categories if isinstance(c, dict)] == performance_scales.LEGACY_PHASE1_MIN_SCORES
    )
    if categories and not is_phase1_default:
        try:
            performance_scales_seed = performance_scales.validate_categories(categories)
            from app.models.performance import RatingScale

            if db.query(RatingScale.id).first() is None:
                db.add(
                    RatingScale(
                        effective_from=performance_scales.SEED_EFFECTIVE_FROM,
                        categories=performance_scales_seed,
                        rating_bands=(
                            performance_scales.validate_rating_bands(bands)
                            if bands
                            else copy.deepcopy(performance_scales.SEED_RATING_BANDS)
                        ),
                        rating_to_pi=dict(performance_scales.SEED_RATING_TO_PI),
                        note="Migrated from the earlier settings document",
                        created_by="system",
                    )
                )
        except ValueError:
            logger.warning("Legacy performance thresholds were invalid; the confirmed defaults apply instead.")
    row.value = value
    row.version = (row.version or 1) + 1
    row.updated_by = "system:threshold-migration"
    _write_history(db, row, row.updated_by)
    return True


def ensure_defaults(db: Session) -> None:
    """Idempotent: org config, seed roles and the default rating scale. Safe to call repeatedly."""
    from app.models.performance import PerformanceConfig, PerformanceRole

    changed = False
    row = db.get(PerformanceConfig, "org")
    if row is None:
        row = PerformanceConfig(key="org", value=copy.deepcopy(DEFAULT_CONFIG), version=1)
        db.add(row)
        db.flush()
        _write_history(db, row, "system")
        changed = True
    else:
        changed = _migrate_legacy_thresholds(db, row) or changed

    existing = {r.key for r in db.query(PerformanceRole.key).all()}
    for key, name, department, kind in SEED_ROLES:
        if key not in existing:
            db.add(PerformanceRole(key=key, name=name, department=department, kind=kind, is_system=True))
            changed = True
    if changed:
        db.commit()
    performance_scales.ensure_seeded(db)


def get_org_config(db: Session) -> tuple[dict[str, Any], int]:
    from app.models.performance import PerformanceConfig

    ensure_defaults(db)
    row = db.get(PerformanceConfig, "org")
    try:
        return validate_config(row.value or {}), row.version
    except ValueError:
        logger.exception("Stored performance config is invalid - falling back to defaults.")
        return copy.deepcopy(DEFAULT_CONFIG), row.version


def save_org_config(db: Session, cfg: dict[str, Any], username: str) -> tuple[dict[str, Any], int]:
    from app.models.performance import PerformanceConfig

    ensure_defaults(db)
    cleaned = validate_config(cfg)
    row = db.get(PerformanceConfig, "org")
    row.value = cleaned
    row.version = (row.version or 1) + 1
    row.updated_by = username
    _write_history(db, row, username)
    db.commit()
    return cleaned, row.version


def effective_config(db: Session, role, org: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Org config with a role's own overrides layered on top (invalid overrides ignored).
    Pass `org` (from get_org_config) when doing this for many roles, so the org config is read once and not once per role.
    """
    if org is None:
        org, _ = get_org_config(db)
    else:
        org = copy.deepcopy(org)  # each role still gets its own copy, exactly as before
    overrides = getattr(role, "config_overrides", None)
    if not overrides:
        return org
    try:
        return validate_config({**org, **overrides})
    except ValueError:
        logger.warning("Ignoring invalid config overrides on performance role %s", getattr(role, "key", "?"))
        return org


def log_audit(
    db: Session,
    actor_username: str,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append-only audit entry. Callers pass ids and field NAMES only - never IDs of people, free text or appraisal content."""
    from app.models.performance import PerformanceAuditLog

    db.add(
        PerformanceAuditLog(
            actor_username=actor_username, action=action, entity_type=entity_type, entity_id=entity_id, details=details
        )
    )
    db.commit()
