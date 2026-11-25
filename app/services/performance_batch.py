"""
Batched role lookups, so a report costs a fixed number of queries however many roles there are.

They give exactly the answers of `performance_roles.latest_version` and `performance_roles.kpis_for`, one role at a time, but in one query
for all of them. Reports, health checks and the like used to ask those questions once per role.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.models.performance import PerformanceKpi, PerformanceRoleVersion


def latest_versions(db: Session, role_ids: Iterable[str]) -> dict[str, PerformanceRoleVersion | None]:
    """The current (highest) JD version of each role, or None for a role that has none."""
    ids = list(dict.fromkeys(role_ids))
    out: dict[str, PerformanceRoleVersion | None] = {rid: None for rid in ids}
    if not ids:
        return out
    q = db.query(PerformanceRoleVersion).filter(PerformanceRoleVersion.role_id.in_(ids)).order_by(PerformanceRoleVersion.role_id, PerformanceRoleVersion.version.desc())
    for v in q:
        if out[v.role_id] is None:
            out[v.role_id] = v
    return out


def kpis_by_version(db: Session, version_ids: Iterable[str]) -> dict[str, list[PerformanceKpi]]:
    """The KPIs of each version, in the JD's own order (what `kpis_for` returns for one version)."""
    ids = list(dict.fromkeys(version_ids))
    out: dict[str, list[PerformanceKpi]] = {vid: [] for vid in ids}
    if not ids:
        return out
    for k in db.query(PerformanceKpi).filter(PerformanceKpi.role_version_id.in_(ids)).order_by(PerformanceKpi.sort_order.asc()):
        out[k.role_version_id].append(k)
    return out
