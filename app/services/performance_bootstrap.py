"""
Staff Performance - startup steps. Called ONLY when PERFORMANCE_MODULE_ENABLED
is true (see app/main.py); with the flag off none of this code ever runs.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.models.performance import PerformanceConfig
from app.models.role_permission import RolePermission
from app.services.performance_config import ensure_defaults
from app.services.performance_levels import seed_levels
from app.services.performance_roles import seed_role_templates
from app.services.role_permissions import PERFORMANCE_DEFAULT_PATHS

logger = logging.getLogger("bidii.performance")

MENU_MIGRATION_KEY = "menu_migration"


def append_menus_to_role_overrides(db: Session) -> int:
    """
    Roles an admin has customised have a stored RolePermission override, which
    REPLACES the defaults - so adding paths to DEFAULT_MENU_ACCESS alone would
    never reach them. This appends the performance paths to those overrides:

      * additive only - nothing is ever removed or reordered
      * never duplicates
      * runs each path ONCE (recorded in performance_config) so an admin who
        later deliberately unticks a performance menu is not overruled by the
        next restart

    Returns the number of override rows changed (0 on a re-run).
    """
    marker = db.get(PerformanceConfig, MENU_MIGRATION_KEY)
    done = set((marker.value or {}).get("appended", [])) if marker else set()
    todo = [p for p in PERFORMANCE_DEFAULT_PATHS if p not in done]
    if not todo:
        return 0

    changed = 0
    for row in db.query(RolePermission).all():
        menus = list(row.allowed_menus or [])
        missing = [p for p in todo if p not in menus]
        if missing:
            row.allowed_menus = [*menus, *missing]
            row.updated_by = "system:performance-module"
            changed += 1

    value = {"appended": sorted(done | set(todo))}
    if marker is None:
        db.add(PerformanceConfig(key=MENU_MIGRATION_KEY, value=value, version=1, updated_by="system"))
    else:
        marker.value = value
    db.commit()
    return changed


def ensure_additive_columns(db: Session) -> list[str]:
    """
    create_all never adds a column to a table that already exists. A database that ran an earlier phase
    therefore lacks columns added since; add them here, idempotently (never alters or drops anything).
    Returns what was added.
    """
    added: list[str] = []
    inspector = inspect(db.get_bind())
    if "staff_profiles" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("staff_profiles")}
        if "level_id" not in columns:
            db.execute(text("ALTER TABLE staff_profiles ADD COLUMN level_id VARCHAR(36) REFERENCES performance_levels (id)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_staff_profiles_level_id ON staff_profiles (level_id)"))
            db.commit()
            added.append("staff_profiles.level_id")
    if "performance_units" in inspector.get_table_names():
        unit_columns = {c["name"] for c in inspector.get_columns("performance_units")}
        if "parent_unit_id" not in unit_columns:
            db.execute(text("ALTER TABLE performance_units ADD COLUMN parent_unit_id VARCHAR(36)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_performance_units_parent_unit_id ON performance_units (parent_unit_id)"))
            db.commit()
            added.append("performance_units.parent_unit_id")
    return added


def bootstrap_performance(db: Session) -> None:
    if ensure_additive_columns(db):
        logger.info("Added missing performance column(s).")
    ensure_defaults(db)
    seed_role_templates(db)
    seed_levels(db)
    from app.services.performance_sync import backfill

    backfill(db)  # every existing login is also a staff member
    changed = append_menus_to_role_overrides(db)
    if changed:
        logger.info("Performance menus appended to %d saved role override(s).", changed)
