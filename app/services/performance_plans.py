"""
Saved improvement plans (the Outlook chart, step 3).

A plan is a decision: "we agreed this goal and this month-by-month path". It is frozen when saved, so later months can be compared with it.
Rules:
* The numbers are NEVER taken from the browser. The caller passes the outlook result the server just computed; the path is read from that.
* One active plan per person (the database enforces it). Saving a different plan closes the old one as "superseded" and keeps how it stood.
  Saving the same plan again (same start month, goal and length) changes nothing and returns the existing one, so a double click is harmless.
* A plan never changes a score, rating, status or PIP, and never touches the suggestion's own `outcome` (the follow-up job owns that).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.performance import PerformancePlan
from app.services import performance_outlook as outlook

HISTORY_LIMIT = 20


class PlanError(Exception):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.message, self.status_code = message, status_code


def active_plan(db: Session, profile_id: str) -> PerformancePlan | None:
    return db.query(PerformancePlan).filter(PerformancePlan.profile_id == profile_id, PerformancePlan.status == "active").first()


def history(db: Session, profile_id: str, limit: int = HISTORY_LIMIT) -> list[PerformancePlan]:
    return db.query(PerformancePlan).filter(PerformancePlan.profile_id == profile_id).order_by(PerformancePlan.created_at.desc()).limit(limit).all()


def evaluation(plan: PerformancePlan, actuals: Mapping[str, float]) -> dict[str, Any]:
    return outlook.evaluate_plan(plan.path or [], float(plan.goal_pct), actuals)


def to_json(plan: PerformancePlan, actuals: Mapping[str, float] | None = None, *, can_manage: bool = False) -> dict[str, Any]:
    """`note`, who saved it and who closed it are shown to people who can manage the plan, not to the person it is about."""
    out: dict[str, Any] = {
        "id": plan.id, "profile_id": plan.profile_id, "recommendation_id": plan.recommendation_id, "status": plan.status,
        "goal_pct": plan.goal_pct, "goal_label": plan.goal_label, "start_month": plan.start_month, "start_pct": plan.start_pct,
        "plan_months": plan.plan_months, "path": plan.path or [], "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "closed_at": plan.closed_at.isoformat() if plan.closed_at else None, "close_reason": plan.close_reason,
        "result": plan.result,
    }
    if can_manage:
        out.update(note=plan.note, created_by=plan.created_by_username, closed_by=plan.closed_by_username, close_note=plan.close_note)
    if actuals is not None and plan.status == "active":
        out["evaluation"] = evaluation(plan, actuals)
    return out


def _close(plan: PerformancePlan, reason: str, username: str, note: str | None, actuals: Mapping[str, float]) -> None:
    plan.result = {**evaluation(plan, actuals), "closed_on": datetime.now(timezone.utc).date().isoformat()}
    plan.status = "closed" if reason == "manual" else "superseded"
    plan.active_slot = None
    plan.closed_by_username, plan.closed_at, plan.close_reason, plan.close_note = username, datetime.now(timezone.utc), reason, note


def save_plan(db: Session, *, profile_id: str, result: Mapping[str, Any], recommendation_id: str | None, note: str | None, username: str) -> tuple[PerformancePlan, bool]:
    """`result` is the outlook the server just built for this goal and length. Returns (plan, created)."""
    block = result.get("plan") or {}
    if not result.get("available") or not block.get("available"):
        raise PlanError(block.get("reason") or result.get("reason") or "There's no path to save for this person yet.")
    path = outlook.plan_path(result)
    if len(path) < 2:
        raise PlanError("There's no path to save for this person yet.")
    start_month, goal, months = path[0]["month"], float(block["goal_pct"]), int(block["months"])
    actuals = outlook.actuals_by_month(result)
    existing = active_plan(db, profile_id)
    if existing is not None:
        if existing.start_month == start_month and float(existing.goal_pct) == goal and int(existing.plan_months) == months:
            return existing, False
        _close(existing, "superseded", username, None, actuals)
        db.flush()  # frees the person's single active slot before the new plan takes it
    plan = PerformancePlan(
        profile_id=profile_id, recommendation_id=recommendation_id, status="active", active_slot=1, goal_pct=goal, goal_label=block.get("goal_label"),
        start_month=start_month, start_pct=float(block["from_pct"]), plan_months=months, path=path, note=(note or None), created_by_username=username,
    )
    db.add(plan)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise PlanError("Someone saved a plan for this person at the same moment. Reload and check it.", 409)
    return plan, True


def close_plan(db: Session, plan: PerformancePlan, *, username: str, note: str | None, actuals: Mapping[str, float]) -> PerformancePlan:
    if plan.status != "active":
        raise PlanError("That plan is already closed.", 409)
    _close(plan, "manual", username, note, actuals)
    db.commit()
    return plan
