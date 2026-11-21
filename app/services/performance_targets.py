"""
Targets: effective-dated, append-only, with bulk tools and a request/approve workflow.

* A target row means "from this month on, this person's target for this KPI is X". The target IN FORCE for a
  month is the row with the latest effective_from not after it (ties: the most recently created). Rows are never
  updated or deleted, so changing a target mid-year cannot rewrite earlier months, and every change keeps who,
  when and why.
* Only KPIs of the person's role that can carry a number can have a target (rated KPIs cannot).
* Every write needs a reason (3-300 characters).
* Bulk operations are PLANS first: nothing is written until the plan is applied.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.performance import (
    PerformanceLevel,
    PerformanceTarget,
    StaffProfile,
    TargetChangeRequest,
    UnitRegionLink,
)
from app.services.performance_levels import MAX_TARGET
from app.services.performance_roles import targetable_kpis
from app.services.performance_time import today_nairobi

MAX_PLAN_ITEMS = 5000


class TargetError(ValueError):
    """A problem the admin can fix; the message is safe to show."""


def first_of_month(d: date) -> date:
    return d.replace(day=1)


def clean_value(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise TargetError("A target must be a number.")
    try:
        d = Decimal(str(value))
    except Exception:
        raise TargetError("A target must be a number.") from None
    if not d.is_finite() or d < 0 or d > MAX_TARGET:
        raise TargetError("A target must be a number from 0 upwards (a target of 0 means the KPI is not scored).")
    return d.quantize(Decimal("0.0001"))


def clean_reason(reason: Any) -> str:
    text = reason.strip() if isinstance(reason, str) else ""
    if len(text) < 3:
        raise TargetError("Please give a reason for this target (at least a few words).")
    if len(text) > 300:
        raise TargetError("The reason is too long (max 300 characters).")
    return text


def pick_in_force(rows: Iterable[Any], month: date) -> Any | None:
    """The row in force for `month` from rows with .effective_from and .created_at (pure; used by tests and callers)."""
    m = first_of_month(month)
    best = None
    for r in rows:
        if r.effective_from <= m and (best is None or (r.effective_from, r.created_at) > (best.effective_from, best.created_at)):
            best = r
    return best


def in_force_map(db: Session, profile_ids: Iterable[str], month: date, kpi_keys: Iterable[str] | None = None) -> dict[tuple[str, str], PerformanceTarget]:
    """{(profile_id, kpi_key): row in force for the month} in ONE query, however many people."""
    ids = list(profile_ids)
    if not ids:
        return {}
    q = db.query(PerformanceTarget).filter(PerformanceTarget.profile_id.in_(ids), PerformanceTarget.effective_from <= first_of_month(month))
    if kpi_keys is not None:
        q = q.filter(PerformanceTarget.kpi_key.in_(list(kpi_keys)))
    out: dict[tuple[str, str], PerformanceTarget] = {}
    for row in q.order_by(PerformanceTarget.effective_from.asc(), PerformanceTarget.created_at.asc()):
        out[(row.profile_id, row.kpi_key)] = row  # ascending, so the latest wins
    return out


def _require_targetable(db: Session, profile: StaffProfile, kpi_key: str):
    kpis = targetable_kpis(db, profile.performance_role_id)
    if not kpis:
        raise TargetError("This person's job role has no KPIs set up yet.")
    if kpi_key not in kpis:
        raise TargetError(f'"{kpi_key}" isn\'t a KPI with a target for this person\'s job role.')
    return kpis[kpi_key]


def set_target(
    db: Session, profile: StaffProfile, kpi_key: str, effective_from: date, value: Any, reason: Any, username: str,
    *, source: str = "manual", commit: bool = True,
) -> PerformanceTarget | None:
    """Appends a target row. Returns None (and writes nothing) when it would not change what is in force."""
    _require_targetable(db, profile, kpi_key)
    month = first_of_month(effective_from)
    v, why = clean_value(value), clean_reason(reason)
    current = in_force_map(db, [profile.id], month, [kpi_key]).get((profile.id, kpi_key))
    if current is not None and Decimal(current.value) == v:
        return None
    row = PerformanceTarget(profile_id=profile.id, kpi_key=kpi_key, effective_from=month, value=v, source=source, reason=why, set_by_username=username)
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def history(db: Session, profile_id: str, kpi_key: str | None = None) -> list[PerformanceTarget]:
    q = db.query(PerformanceTarget).filter(PerformanceTarget.profile_id == profile_id)
    if kpi_key:
        q = q.filter(PerformanceTarget.kpi_key == kpi_key)
    return q.order_by(PerformanceTarget.effective_from.desc(), PerformanceTarget.created_at.desc()).all()


# ---- bulk plans ---------------------------------------------------------------------------------------------


@dataclass
class PlanItem:
    profile_id: str
    full_name: str
    kpi_key: str | None
    current: Decimal | None
    new: Decimal | None
    action: str  # create | unchanged | skip
    note: str | None = None


def select_profiles(
    db: Session, *, role_id: str | None = None, unit_id: str | None = None, region_id: str | None = None,
    level_id: str | None = None, profile_ids: list[str] | None = None, include_inactive: bool = False,
) -> list[StaffProfile]:
    q = db.query(StaffProfile)
    if not include_inactive:
        q = q.filter(StaffProfile.is_active.is_(True))
    if role_id:
        q = q.filter(StaffProfile.performance_role_id == role_id)
    if unit_id:
        q = q.filter(StaffProfile.unit_id == unit_id)
    if level_id:
        q = q.filter(StaffProfile.level_id == level_id)
    if profile_ids is not None:
        q = q.filter(StaffProfile.id.in_(profile_ids or [""]))
    if region_id:
        today = today_nairobi()
        in_region = db.query(UnitRegionLink.unit_id).filter(
            UnitRegionLink.region_id == region_id, UnitRegionLink.effective_from <= today,
            (UnitRegionLink.effective_to.is_(None)) | (UnitRegionLink.effective_to >= today),
        )
        q = q.filter(StaffProfile.unit_id.in_(in_region))
    rows = q.order_by(StaffProfile.full_name.asc()).limit(MAX_PLAN_ITEMS + 1).all()
    if len(rows) > MAX_PLAN_ITEMS:
        raise TargetError(f"That selects more than {MAX_PLAN_ITEMS} people; narrow it down.")
    return rows


def _kpi_cache(db: Session):
    cache: dict[str, dict] = {}

    def get(role_id: str):
        if role_id not in cache:
            cache[role_id] = targetable_kpis(db, role_id)
        return cache[role_id]

    return get


def plan_set_value(db: Session, profiles: list[StaffProfile], kpi_key: str, value: Any, effective_from: date) -> list[PlanItem]:
    v, month = clean_value(value), first_of_month(effective_from)
    kpis = _kpi_cache(db)
    current = in_force_map(db, [p.id for p in profiles], month, [kpi_key])
    items = []
    for p in profiles:
        if kpi_key not in kpis(p.performance_role_id):
            items.append(PlanItem(p.id, p.full_name, kpi_key, None, None, "skip", "This role has no target for that KPI"))
            continue
        cur = current.get((p.id, kpi_key))
        cur_v = Decimal(cur.value) if cur else None
        items.append(PlanItem(p.id, p.full_name, kpi_key, cur_v, v, "unchanged" if cur_v == v else "create"))
    return items


def plan_level_defaults(db: Session, profiles: list[StaffProfile], effective_from: date, overwrite: bool) -> list[PlanItem]:
    month = first_of_month(effective_from)
    levels = {l.id: l for l in db.query(PerformanceLevel).filter(PerformanceLevel.id.in_({p.level_id for p in profiles if p.level_id} or {""}))}
    kpis = _kpi_cache(db)
    current = in_force_map(db, [p.id for p in profiles], month)
    items: list[PlanItem] = []
    for p in profiles:
        level = levels.get(p.level_id) if p.level_id else None
        if level is None:
            items.append(PlanItem(p.id, p.full_name, None, None, None, "skip", "No level set"))
            continue
        if not level.default_targets:
            items.append(PlanItem(p.id, p.full_name, None, None, None, "skip", f"{level.label} has no default targets"))
            continue
        for key, raw in level.default_targets.items():
            if key not in kpis(p.performance_role_id):
                items.append(PlanItem(p.id, p.full_name, key, None, None, "skip", "This role has no target for that KPI"))
                continue
            new, cur = clean_value(raw), current.get((p.id, key))
            cur_v = Decimal(cur.value) if cur else None
            if cur_v == new:
                items.append(PlanItem(p.id, p.full_name, key, cur_v, new, "unchanged"))
            elif cur_v is not None and not overwrite:
                items.append(PlanItem(p.id, p.full_name, key, cur_v, new, "skip", "Already has a target (kept)"))
            else:
                items.append(PlanItem(p.id, p.full_name, key, cur_v, new, "create"))
    return items


def plan_copy_forward(
    db: Session, profiles: list[StaffProfile], source_month: date, effective_from: date, uplift_pct: Any = 0, kpi_key: str | None = None,
) -> list[PlanItem]:
    try:
        uplift = Decimal(str(uplift_pct))
    except Exception:
        raise TargetError("The uplift must be a number.") from None
    if not uplift.is_finite() or not Decimal(-100) < uplift <= Decimal(1000):
        raise TargetError("The uplift must be between -100% and 1000%.")
    src_month, month = first_of_month(source_month), first_of_month(effective_from)
    keys = [kpi_key] if kpi_key else None
    source = in_force_map(db, [p.id for p in profiles], src_month, keys)
    target_now = in_force_map(db, [p.id for p in profiles], month, keys)
    items: list[PlanItem] = []
    for p in profiles:
        found = [(k, r) for (pid, k), r in source.items() if pid == p.id]
        if not found:
            items.append(PlanItem(p.id, p.full_name, kpi_key, None, None, "skip", f"No target in force in {src_month:%b %Y}"))
            continue
        for key, row in sorted(found):
            new = (Decimal(row.value) * (Decimal(1) + uplift / Decimal(100))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            cur = target_now.get((p.id, key))
            cur_v = Decimal(cur.value) if cur else None
            items.append(PlanItem(p.id, p.full_name, key, cur_v, new, "unchanged" if cur_v == new else "create"))
    return items


def apply_plan(db: Session, items: list[PlanItem], effective_from: date, reason: Any, username: str, source: str) -> int:
    """Writes every 'create' item in ONE transaction (all or nothing)."""
    why, month = clean_reason(reason), first_of_month(effective_from)
    created = 0
    for it in items:
        if it.action == "create" and it.new is not None and it.kpi_key:
            db.add(PerformanceTarget(profile_id=it.profile_id, kpi_key=it.kpi_key, effective_from=month, value=it.new, source=source, reason=why, set_by_username=username))
            created += 1
    db.commit()
    return created


# ---- change requests ------------------------------------------------------------------------------------------


def create_request(db: Session, profile: StaffProfile, kpi_key: str, effective_from: date, value: Any, reason: Any, username: str) -> TargetChangeRequest:
    _require_targetable(db, profile, kpi_key)
    month, v, why = first_of_month(effective_from), clean_value(value), clean_reason(reason)
    dup = db.query(TargetChangeRequest).filter_by(profile_id=profile.id, kpi_key=kpi_key, effective_from=month, status="pending").first()
    if dup is not None:
        raise TargetError("There is already a pending request for that target and month.")
    req = TargetChangeRequest(profile_id=profile.id, kpi_key=kpi_key, effective_from=month, proposed_value=v, reason=why, requested_by_username=username)
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def decide_request(db: Session, req: TargetChangeRequest, approve: bool, username: str, note: str | None) -> PerformanceTarget | None:
    if req.status != "pending":
        raise TargetError(f"That request was already {req.status}.")
    created = None
    if approve:
        profile = db.get(StaffProfile, req.profile_id)
        created = set_target(db, profile, req.kpi_key, req.effective_from, req.proposed_value, req.reason, username, source="request", commit=False)
    req.status = "approved" if approve else "rejected"
    req.decided_by_username, req.decided_at = username, datetime.now(timezone.utc)
    req.decision_note = (note or "").strip()[:300] or None
    db.commit()
    return created


def withdraw_request(db: Session, req: TargetChangeRequest) -> None:
    if req.status != "pending":
        raise TargetError(f"That request was already {req.status}.")
    req.status = "withdrawn"
    db.commit()
