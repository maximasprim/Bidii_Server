"""Builds the appraisal as the caller is allowed to see and act on it (permissions are computed on the server)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.performance import Appraisal, PerformanceRole, PerformanceUnit, StaffProfile
from app.services import performance_appraisals as pa
from app.services.performance_periods import Period, following_months, parse_period
from app.services.performance_scope import PerformanceScope


def _f(v: Any) -> float | None:
    return float(v) if v is not None else None


def _weighted(weight: Decimal, rating: Any) -> float | None:
    return float(Decimal(weight) * Decimal(rating) / 100) if rating is not None else None


def permissions(ap: Appraisal, profile: StaffProfile, parts: pa.Parts, resolver: pa.ActorResolver, scope: PerformanceScope) -> dict[str, Any]:
    step = ap.current_step
    chain = list(ap.signoff_chain)
    act = pa.can_act(resolver, scope, profile, step) and ap.status != "finalised"
    own = scope.own_profile_id
    is_subject = own is not None and own == profile.id
    editable: list[str] = []
    if act and step == "staff":
        editable = ["self_text", "self_rating", "competency_self_rating", "objectives", "development_needs"]
    elif act and step == "manager":
        editable = ["manager_rating", "competency_manager_rating", "objectives", "development_needs"]
    rated = ap.status == "finalised" or (ap.status == "in_review" and step in chain and chain.index(step) > chain.index("manager"))
    return {
        "can_edit": bool(editable), "editable": editable,
        "can_submit": act and step == "staff" and ap.status == "draft",
        "can_sign": act and ap.status == "in_review",
        "can_return": act and ap.status == "in_review" and step in chain and chain.index(step) > 0,
        "can_dispute": rated and pa._is_appraisee_or_proxy(scope, profile),  # noqa: SLF001
        "can_resolve": scope.is_hr and not is_subject and any(d.status == "open" for d in parts.disputes),
        "can_acknowledge": ap.status == "finalised" and ap.acknowledged_at is None and pa._is_appraisee_or_proxy(scope, profile),  # noqa: SLF001
        "can_reopen": (scope.is_admin or scope.is_hr) and not is_subject and ap.status == "finalised",
    }


def chain_view(ap: Appraisal, parts: pa.Parts) -> list[dict[str, Any]]:
    chain = list(ap.signoff_chain)
    cur = chain.index(ap.current_step) if ap.current_step in chain else (len(chain) if ap.status == "finalised" else 0)
    out = []
    for i, step in enumerate(chain):
        last = next((s for s in reversed(parts.signoffs) if s.step == step and s.decision == "signed"), None)
        state = "done" if i < cur or ap.status == "finalised" else "current" if i == cur else "upcoming"
        out.append({
            "step": step, "label": pa.STEP_LABEL.get(step, step), "state": state,
            "signed_by": last.actor_username if last and state == "done" else None, "signed_at": last.signed_at if last and state == "done" else None,
            "comment": last.comment if last and state == "done" else None,
        })
    return out


def objective_month_labels(period: Period) -> list[str]:
    return [m.strftime("%b %Y") for m in following_months(period, 3)]


def detail(db: Session, ap: Appraisal, profile: StaffProfile, resolver: pa.ActorResolver, scope: PerformanceScope) -> dict[str, Any]:
    parts = pa.load_parts(db, ap)
    role = db.get(PerformanceRole, profile.performance_role_id)
    unit = db.get(PerformanceUnit, profile.unit_id) if profile.unit_id else None
    manager = db.get(StaffProfile, profile.manager_id) if profile.manager_id else None
    period = parse_period(ap.period_key)
    self_total = sum((Decimal(i.weight) * Decimal(i.self_rating) / 100 for i in parts.items if i.self_rating is not None), Decimal(0)) if any(i.self_rating is not None for i in parts.items) else None
    mgr_total = sum((Decimal(i.weight) * Decimal(i.manager_rating) / 100 for i in parts.items if i.manager_rating is not None), Decimal(0)) if any(i.manager_rating is not None for i in parts.items) else None
    return {
        "id": ap.id,
        "profile": {"id": profile.id, "full_name": profile.full_name, "employee_no": profile.employee_no, "hire_date": profile.hire_date, "role_name": role.name if role else None, "unit_name": unit.name if unit else None, "manager_name": manager.full_name if manager else None},
        "period_key": ap.period_key, "period_label": period.label, "period_start": ap.period_start, "period_end": ap.period_end,
        "status": ap.status, "current_step": ap.current_step, "current_step_label": pa.STEP_LABEL.get(ap.current_step) if ap.current_step else None,
        "chain": chain_view(ap, parts),
        "items": [{"kpi_key": i.kpi_key, "label": i.label, "weight": float(i.weight), "criteria": i.criteria, "self_text": i.self_text, "self_rating": i.self_rating,
                   "manager_rating": i.manager_rating, "final_rating": _f(i.final_rating), "self_weighted": _weighted(i.weight, i.self_rating), "manager_weighted": _weighted(i.weight, i.manager_rating)} for i in parts.items],
        "competencies": [{"key": c.competency_key, "title": c.title, "self_rating": c.self_rating, "manager_rating": c.manager_rating} for c in parts.competencies],
        "objective_months": objective_month_labels(period),
        "objectives": [{"label": o.label, "values": list(o.values or ["", "", ""])} for o in parts.objectives],
        "development_needs": [{"slot": n.slot, "text": n.text} for n in parts.needs],
        "signoffs": [{"step": s.step, "label": pa.STEP_LABEL.get(s.step, "Reopened"), "decision": s.decision, "comment": s.comment, "actor_username": s.actor_username, "signed_at": s.signed_at} for s in parts.signoffs],
        "disputes": [{"id": d.id, "item_key": d.item_key, "reason": d.reason, "raised_by_username": d.raised_by_username, "status": d.status, "resolution": d.resolution,
                      "new_manager_rating": d.new_manager_rating, "resolved_by_username": d.resolved_by_username, "created_at": d.created_at, "resolved_at": d.resolved_at} for d in parts.disputes],
        "self_weighted_total": _f(self_total), "manager_weighted_total": _f(mgr_total), "final_weighted_rating": _f(ap.final_weighted_rating),
        "acknowledged_at": ap.acknowledged_at, "acknowledged_by_username": ap.acknowledged_by_username, "source": ap.source, "extraction_status": ap.extraction_status,
        "permissions": permissions(ap, profile, parts, resolver, scope),
    }
