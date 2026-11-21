"""
Staff Performance - scores and reports for people, teams, branches, regions and the organisation (Phase 3).

Everything here is computed from VERIFIED figures with the pure rules in performance_scoring; nothing is stored
by the readers. Numbers are never produced by AI.

How a person's score for a period is built
  * Each KPI the person enters monthly: per month, the verified figure against the target in force that month
    (pro-rated for new hires). Months with a figure but nothing verified are "pending"; months with a target but no
    figure are "unreported". Neither counts as a fake 0. Achievement for the period = sum(actual)/sum(target) over the
    months that have both (for a percent KPI such as PAR: the average of both), inverted for "lower is better".
  * Rated KPIs and competencies come from the latest FINALISED appraisal that overlaps the period (1-5 -> PI via the
    rating map in force).
  * Score = sum(weight x PI) re-normalised over the KPIs actually scored; blended with the competency score by the
    configured share when the appraisal rated competencies. A zero/missing target means "not scored".
  * Category and the 1-5 rating use the rating scale in force on the period's LAST DAY, so old periods never change
    when thresholds do. The legacy four staff-count bands are computed from headline achievement.
  * Teams, branches, regions and the organisation: sum(actual)/sum(target) over members (never an average of
    percentages) plus the distribution of people across the categories and legacy bands.
  * Transfers: a branch's figures for a month count only for people assigned to it that month (assignment history).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.performance import (
    Appraisal,
    AppraisalItem,
    CompetencyRating,
    KpiEntry,
    PerformanceKpi,
    PerformanceRole,
    PerformanceTarget,
    PerformanceUnit,
    Region,
    StaffAssignmentHistory,
    StaffProfile,
    UnitRegionLink,
)
from app.services.performance_config import effective_config, get_org_config
from app.services.performance_periods import Period, PeriodError, month_end, months_in, parse_period
from app.services.performance_roles import kpis_for, latest_version
from app.services.performance_scales import scale_for_period
from app.services.performance_scoring import (
    LEGACY_BANDS,
    achievement_pct,
    blend_with_competencies,
    classify_score,
    composite_score,
    final_rating,
    legacy_band,
    performance_index,
    ramp_target,
    rating_for_achievement,
    rollup,
    round2,
    to_decimal,
)
from app.services.performance_targets import first_of_month, pick_in_force
from app.services.performance_time import today_nairobi

COUNTED = {"verified", "locked"}
_CUSTOM = re.compile(r"^(\d{4}-(?:0[1-9]|1[0-2]))\.\.(\d{4}-(?:0[1-9]|1[0-2]))$")
SMALL_UNIT = 4  # fewer people than this and a branch ranking is not meaningful
MAX_CUSTOM_MONTHS = 36


# ---- periods -------------------------------------------------------------------------------------------------------


def resolve_period(key: str, today: date | None = None, fiscal_start_month: int = 1) -> Period:
    """2026-03 | 2026-Q1 | 2026-H1 | 2026 | ytd | 2026-01..2026-06 (custom)."""
    today = today or today_nairobi()
    text = (key or "").strip().lower()
    if text == "ytd":
        year = today.year if today.month >= fiscal_start_month else today.year - 1
        start = date(year, fiscal_start_month, 1)
        return Period("ytd", "ytd", start, month_end(first_of_month(today)), f"Year to date ({start:%b %Y} to {today:%b %Y})")
    if m := _CUSTOM.match(text):
        a, b = parse_period(m[1]), parse_period(m[2])
        if b.start < a.start:
            raise PeriodError("A custom period must end after it starts.")
        if len(months_in(Period("c", "custom", a.start, b.end, ""))) > MAX_CUSTOM_MONTHS:
            raise PeriodError(f"A custom period can cover at most {MAX_CUSTOM_MONTHS} months.")
        return Period(f"{m[1]}..{m[2]}", "custom", a.start, b.end, f"{a.start:%b %Y} to {b.end:%b %Y}")
    return parse_period(key)


def previous_period(p: Period) -> Period:
    """The same kind of period immediately before `p` (for movers and comparisons)."""
    n = len(months_in(p))
    start_idx = p.start.year * 12 + p.start.month - 1 - n
    y, m = divmod(start_idx, 12)
    start = date(y, m + 1, 1)
    end = month_end(date((p.start.year * 12 + p.start.month - 2) // 12, (p.start.month - 2) % 12 + 1, 1))
    return Period(f"{start:%Y-%m}..{end:%Y-%m}", "custom", start, end, f"{start:%b %Y} to {end:%b %Y}")


def scored_months(p: Period, today: date) -> list[date]:
    """Months of the period that have started (a future month can't have results)."""
    cap = first_of_month(today)
    return [m for m in months_in(p) if m <= cap]


# ---- data loading (batched) ------------------------------------------------------------------------------------------


@dataclass
class Ctx:
    db: Session
    today: date
    scale: Any
    profiles: dict[str, StaffProfile] = field(default_factory=dict)
    roles: dict[str, PerformanceRole] = field(default_factory=dict)
    role_kpis: dict[str, list[PerformanceKpi]] = field(default_factory=dict)
    cfg: dict[str, dict[str, Any]] = field(default_factory=dict)
    entries: dict[tuple[str, str, date], tuple[Decimal, str]] = field(default_factory=dict)
    targets: dict[tuple[str, str], list[PerformanceTarget]] = field(default_factory=dict)
    appraisals: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    units: dict[str, PerformanceUnit] = field(default_factory=dict)
    history: dict[str, list[StaffAssignmentHistory]] = field(default_factory=dict)


def load_ctx(db: Session, profile_ids: list[str], start: date, end: date, *, today: date | None = None) -> Ctx:
    """Everything needed to score `profile_ids` over [start, end] in a fixed number of queries."""
    today = today or today_nairobi()
    ctx = Ctx(db=db, today=today, scale=scale_for_period(db, end))
    ids = list(dict.fromkeys(profile_ids))
    if not ids:
        return ctx
    ctx.profiles = {p.id: p for p in db.query(StaffProfile).filter(StaffProfile.id.in_(ids)).all()}
    role_ids = {p.performance_role_id for p in ctx.profiles.values()}
    ctx.roles = {r.id: r for r in db.query(PerformanceRole).filter(PerformanceRole.id.in_(role_ids)).all()}
    for rid, role in ctx.roles.items():
        version = latest_version(db, rid)
        ctx.role_kpis[rid] = kpis_for(db, version.id) if version else []
        ctx.cfg[rid] = effective_config(db, role)
    for e in db.query(KpiEntry).filter(KpiEntry.profile_id.in_(ids), KpiEntry.month >= first_of_month(start), KpiEntry.month <= first_of_month(end)):
        ctx.entries[(e.profile_id, e.kpi_key, e.month)] = (Decimal(e.value), e.status)
    for t in db.query(PerformanceTarget).filter(PerformanceTarget.profile_id.in_(ids), PerformanceTarget.effective_from <= first_of_month(end)).order_by(PerformanceTarget.effective_from, PerformanceTarget.created_at):
        ctx.targets.setdefault((t.profile_id, t.kpi_key), []).append(t)
    ctx.units = {u.id: u for u in db.query(PerformanceUnit).all()}
    for h in db.query(StaffAssignmentHistory).filter(StaffAssignmentHistory.profile_id.in_(ids)).order_by(StaffAssignmentHistory.effective_from):
        ctx.history.setdefault(h.profile_id, []).append(h)
    items_by_appraisal: dict[str, dict[str, Decimal | None]] = defaultdict(dict)
    comps_by_appraisal: dict[str, list[Decimal]] = defaultdict(list)
    done = db.query(Appraisal).filter(Appraisal.profile_id.in_(ids), Appraisal.status == "finalised", Appraisal.period_end >= start, Appraisal.period_start <= end).all()
    if done:
        aids = [a.id for a in done]
        for it in db.query(AppraisalItem).filter(AppraisalItem.appraisal_id.in_(aids)):
            items_by_appraisal[it.appraisal_id][it.kpi_key] = Decimal(it.final_rating) if it.final_rating is not None else None
        authority_of = {a.id: ctx.cfg.get(ctx.profiles[a.profile_id].performance_role_id, {}).get("rating_authority", "manager") for a in done if a.profile_id in ctx.profiles}
        for c in db.query(CompetencyRating).filter(CompetencyRating.appraisal_id.in_(aids)):
            fr = final_rating(c.self_rating, c.manager_rating, authority_of.get(c.appraisal_id, "manager"))
            if fr is not None:
                comps_by_appraisal[c.appraisal_id].append(fr)
        for a in sorted(done, key=lambda x: x.period_end):
            ctx.appraisals.setdefault(a.profile_id, []).append(
                {"id": a.id, "period_key": a.period_key, "start": a.period_start, "end": a.period_end, "weighted": a.final_weighted_rating,
                 "items": items_by_appraisal.get(a.id, {}), "competencies": comps_by_appraisal.get(a.id, [])}
            )
    return ctx


def unit_at(ctx: Ctx, profile: StaffProfile, month: date) -> str | None:
    """The unit the person belonged to at the start of `month` (assignment history; the profile's unit if there is none)."""
    rows = ctx.history.get(profile.id)
    if not rows:
        return profile.unit_id
    best = None
    for h in rows:
        if h.effective_from <= month_end(month) and (h.effective_to is None or h.effective_to >= month):
            best = h
    return best.unit_id if best is not None else profile.unit_id


# ---- a person -------------------------------------------------------------------------------------------------------


def _pi_from_rating(rating: Decimal, table: dict[str, Any]) -> Decimal:
    """1-5 (may be .5 when ratings are averaged) -> PI through the map, interpolating between whole ratings."""
    lo = max(1, min(5, int(rating)))
    hi = min(5, lo + 1)
    plo, phi = to_decimal(table[str(lo)]), to_decimal(table[str(hi)])
    return plo + (phi - plo) * (rating - lo) if hi != lo else plo


def _kpi_period(ctx: Ctx, profile: StaffProfile, kpi: PerformanceKpi, months: list[date], ramp: int) -> dict[str, Any]:
    scored: list[tuple[date, Decimal, Decimal]] = []
    pending = unreported = no_target = 0
    per_month: dict[str, dict[str, Any]] = {}
    for m in months:
        rows = ctx.targets.get((profile.id, kpi.key), [])
        row = pick_in_force(rows, m)
        target = ramp_target(Decimal(row.value), profile.hire_date, m, ramp) if row is not None else None
        entry = ctx.entries.get((profile.id, kpi.key, m))
        t_ok = target is not None and target > 0
        if entry is None:
            if t_ok:
                unreported += 1
            else:
                no_target += 1
            per_month[m.strftime("%Y-%m")] = {"actual": None, "target": float(target) if t_ok else None, "pct": None, "state": "unreported" if t_ok else "no_target"}
            continue
        value, status = entry
        if status not in COUNTED:
            pending += 1
            per_month[m.strftime("%Y-%m")] = {"actual": float(value), "target": float(target) if t_ok else None, "pct": None, "state": "pending"}
            continue
        if not t_ok:
            no_target += 1
            per_month[m.strftime("%Y-%m")] = {"actual": float(value), "target": None, "pct": None, "state": "no_target"}
            continue
        scored.append((m, value, target))  # type: ignore[arg-type]
        pct = achievement_pct(value, target, kpi.direction)
        per_month[m.strftime("%Y-%m")] = {"actual": float(value), "target": float(target), "pct": float(round2(pct)) if pct is not None and pct.is_finite() else None, "state": "scored"}  # type: ignore[arg-type]
    actual = target_v = None
    if scored:
        if kpi.unit == "percent":
            actual = sum((v for _, v, _t in scored), Decimal(0)) / len(scored)
            target_v = sum((t for _, _v, t in scored), Decimal(0)) / len(scored)
        else:
            actual = sum((v for _, v, _t in scored), Decimal(0))
            target_v = sum((t for _, _v, t in scored), Decimal(0))
    pct = achievement_pct(actual, target_v, kpi.direction) if actual is not None else None
    return {
        "actual": actual, "target": target_v, "pct": pct, "months_scored": len(scored), "months_pending": pending,
        "months_unreported": unreported, "months_no_target": no_target, "per_month": per_month,
    }


def _appraisal_for(ctx: Ctx, profile_id: str, start: date, end: date) -> dict[str, Any] | None:
    best = None
    for a in ctx.appraisals.get(profile_id, []):
        if a["end"] >= start and a["start"] <= end:
            best = a  # list is ordered by end date: the latest overlapping one wins
    return best


def is_headline(k: PerformanceKpi) -> bool:
    return k.source == "kpi_entry" and k.unit == "kes" and k.direction == "higher"


def score_person(ctx: Ctx, profile_id: str, period: Period, *, unit_filter: str | None = None) -> dict[str, Any]:
    """The full result for one person over one period. `unit_filter` keeps only the months they belonged to that unit."""
    profile = ctx.profiles[profile_id]
    role_id = profile.performance_role_id
    cfg = ctx.cfg.get(role_id) or get_org_config_defaults()
    cap = to_decimal(cfg.get("achievement_cap_pct", 150))
    table = ctx.scale.rating_to_pi
    months = scored_months(period, ctx.today)
    if unit_filter is not None:
        months = [m for m in months if unit_at(ctx, profile, m) == unit_filter]
    kpis = ctx.role_kpis.get(role_id, [])
    appraisal = _appraisal_for(ctx, profile_id, period.start, period.end)
    results: list[dict[str, Any]] = []
    composite_items: list[tuple[Decimal, Decimal | None]] = []
    headline: dict[str, Any] | None = None
    for k in kpis:
        weight = Decimal(k.weight)
        row: dict[str, Any] = {"key": k.key, "label": k.label, "unit": k.unit, "direction": k.direction, "weight": float(weight), "source": k.source}
        pi = None
        if k.source == "kpi_entry":
            agg = _kpi_period(ctx, profile, k, months, int(cfg.get("ramp_up_months", 0)))
            kcap = to_decimal(k.cap_pct) if k.cap_pct is not None else cap
            pct = agg["pct"]
            pi = performance_index(pct, kcap) if pct is not None and pct.is_finite() else None
            row.update(
                actual=float(agg["actual"]) if agg["actual"] is not None else None, target=float(agg["target"]) if agg["target"] is not None else None,
                achievement_pct=float(round2(pct)) if pct is not None and pct.is_finite() else None, months_scored=agg["months_scored"],
                months_pending=agg["months_pending"], months_unreported=agg["months_unreported"], months_no_target=agg["months_no_target"],
                per_month=agg["per_month"], not_scored_reason=None if pi is not None else _reason(agg),
            )
            if headline is None and is_headline(k):
                headline = {**row, "_agg": agg}
        elif k.source == "appraisal_rating":
            rating = appraisal["items"].get(k.key) if appraisal else None
            if rating is not None:
                pi = _pi_from_rating(rating, table)
            row.update(rating=float(rating) if rating is not None else None, achievement_pct=None,
                       not_scored_reason=None if rating is not None else "No finalised appraisal rating for this period.")
        else:
            row.update(not_scored_reason="Not measured in the system yet.")
        row["pi"] = float(round2(pi)) if pi is not None else None
        results.append(row)
        composite_items.append((weight, pi))
    comp = composite_score(composite_items)
    kpi_score = comp.score
    comp_pis = [_pi_from_rating(r, table) for r in (appraisal["competencies"] if appraisal else [])]
    competency_score = sum(comp_pis, Decimal(0)) / len(comp_pis) if comp_pis else None
    score = blend_with_competencies(kpi_score, competency_score, cfg.get("competency_share_pct", 20)) if (kpi_score is not None or competency_score is not None) else None
    cats = ctx.scale.categories
    head_pct = headline["achievement_pct"] if headline else None
    head_dec = Decimal(str(head_pct)) if head_pct is not None else None
    legacy_basis = head_dec if head_dec is not None else score
    pm = (headline or {}).get("per_month", {})
    return {
        "profile_id": profile_id, "name": profile.full_name, "role_id": role_id, "role_name": ctx.roles[role_id].name if role_id in ctx.roles else None,
        "unit_id": profile.unit_id, "unit_name": ctx.units[profile.unit_id].name if profile.unit_id in ctx.units else None,
        "status": profile.status, "is_executive": profile.is_executive,
        "score": float(round2(score)) if score is not None else None,
        "category": classify_score(score, cats),
        "rating": rating_for_achievement(legacy_basis, ctx.scale.rating_bands),
        "legacy_band": legacy_band(legacy_basis),
        "headline": {"label": headline["label"], "key": headline["key"], "actual": headline["actual"], "target": headline["target"], "pct": headline["achievement_pct"]} if headline else None,
        "coverage_pct": float(round2(comp.coverage_pct)) if comp.coverage_pct is not None else None,
        "kpis": results, "competency_score": float(round2(competency_score)) if competency_score is not None else None,
        "appraisal": {"id": appraisal["id"], "period_key": appraisal["period_key"], "weighted_rating": float(appraisal["weighted"]) if appraisal["weighted"] is not None else None} if appraisal else None,
        "trend": [{"month": k_, **v} for k_, v in sorted(pm.items())],
        "scale_effective_from": ctx.scale.effective_from.isoformat(),
        "_score": score, "_head_actual": headline["_agg"]["actual"] if headline else None, "_head_target": headline["_agg"]["target"] if headline else None,
    }


def get_org_config_defaults() -> dict[str, Any]:
    from app.services.performance_config import DEFAULT_CONFIG

    return DEFAULT_CONFIG


def _reason(agg: dict[str, Any]) -> str:
    if agg["months_pending"] and not agg["months_scored"]:
        return "Figures are waiting to be verified."
    if agg["months_no_target"] and not agg["months_scored"] and not agg["months_unreported"]:
        return "No target is set, so this isn't scored."
    if agg["months_unreported"] and not agg["months_scored"]:
        return "No figures have been entered yet."
    return "Nothing could be scored for this period."


def public(person: dict[str, Any]) -> dict[str, Any]:
    """The JSON-safe view: private working fields (leading underscore) and per-month detail on the KPI rows stay server-side."""
    return {k: v for k, v in person.items() if not k.startswith("_")}


# ---- groups ----------------------------------------------------------------------------------------------------------


def group_summary(ctx: Ctx, people: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll-up (sum actual / sum target), target-weighted average score, and where the people land."""
    roll = rollup((p["_head_actual"], p["_head_target"]) for p in people if p["headline"] is not None)
    cats = ctx.scale.categories
    counts = {c["key"]: 0 for c in cats}
    legacy = {b: 0 for b in LEGACY_BANDS}
    unscored = 0
    weighted = weight_sum = Decimal(0)
    plain: list[Decimal] = []
    for p in people:
        s = p["_score"]
        if s is None:
            unscored += 1
            continue
        counts[p["category"]["key"]] += 1
        plain.append(s)
        w = p["_head_target"] if p["_head_target"] else None
        if w:
            weighted += s * w
            weight_sum += w
    for p in people:
        if p["legacy_band"] is not None:
            legacy[p["legacy_band"]] += 1
    avg = weighted / weight_sum if weight_sum > 0 else (sum(plain, Decimal(0)) / len(plain) if plain else None)
    return {
        "people": len(people),
        "achievement_pct": float(round2(roll.pct)) if roll.pct is not None else None,
        "actual_total": float(roll.actual_total), "target_total": float(roll.target_total),
        "scored_people": roll.scored, "no_target": roll.excluded_no_target, "unreported": roll.unreported,
        "average_score": float(round2(avg)) if avg is not None else None,
        "average_category": classify_score(avg, cats),
        "category_counts": counts, "legacy_counts": legacy, "unscored": unscored,
    }


def score_many(ctx: Ctx, profile_ids: list[str], period: Period, *, unit_filter: str | None = None) -> list[dict[str, Any]]:
    return [score_person(ctx, pid, period, unit_filter=unit_filter) for pid in profile_ids if pid in ctx.profiles]


def attention_reasons(person: dict[str, Any], cfg: dict[str, Any], show_pip: bool) -> list[str]:
    """Plain, deterministic reasons someone needs a conversation (the full rules engine comes in Phase 4)."""
    out: list[str] = []
    ew = cfg.get("early_warning", {})
    threshold = float(ew.get("below_threshold_pct", 70))
    need = int(ew.get("consecutive_months_below", 2))
    run = best = 0
    for t in person["trend"]:
        if t["pct"] is not None and t["pct"] < threshold:
            run += 1
            best = max(best, run)
        elif t["pct"] is not None:
            run = 0
    if best >= need:
        out.append(f"Below {threshold:g}% for {best} months running")
    head = person["headline"]
    if head and head["pct"] is not None and head["pct"] < threshold and best < need:
        out.append(f"Achievement {head['pct']:g}% is below {threshold:g}%")
    if any(k.get("months_pending") for k in person["kpis"]):
        out.append("Figures waiting to be verified")
    if person["headline"] is None and person["score"] is None:
        out.append("Nothing scored yet")
    elif any(k["source"] == "kpi_entry" and k.get("months_no_target") and not k.get("months_scored") for k in person["kpis"]):
        out.append("No target set")
    if show_pip and person["status"] == "pip":
        out.append("On a performance improvement plan")
    return out


def sparkline(person: dict[str, Any], n: int = 6) -> list[float | None]:
    return [t["pct"] for t in person["trend"]][-n:]


# ---- membership --------------------------------------------------------------------------------------------------------


def unit_member_ids(db: Session, unit_id: str, start: date, end: date, allowed: set[str] | None) -> list[str]:
    """Everyone who belonged to the unit at any time in the period (current members and people who moved away)."""
    ids = {p for (p,) in db.query(StaffProfile.id).filter(StaffProfile.unit_id == unit_id)}
    for (pid,) in db.query(StaffAssignmentHistory.profile_id).filter(
        StaffAssignmentHistory.unit_id == unit_id, StaffAssignmentHistory.effective_from <= end,
        (StaffAssignmentHistory.effective_to.is_(None)) | (StaffAssignmentHistory.effective_to >= start),
    ):
        ids.add(pid)
    if allowed is not None:
        ids &= allowed
    return sorted(ids)


def region_unit_ids(db: Session, region_id: str, at: date) -> list[str]:
    return [
        u for (u,) in db.query(UnitRegionLink.unit_id).filter(
            UnitRegionLink.region_id == region_id, UnitRegionLink.effective_from <= at,
            (UnitRegionLink.effective_to.is_(None)) | (UnitRegionLink.effective_to >= at),
        )
    ]


def reporting_line(db: Session, leader_id: str) -> list[str]:
    """The leader's direct and indirect reports (not the leader)."""
    rows = db.query(StaffProfile.id, StaffProfile.manager_id).all()
    children: dict[str, list[str]] = defaultdict(list)
    for pid, mgr in rows:
        if mgr:
            children[mgr].append(pid)
    seen: set[str] = set()
    stack = [leader_id]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in seen and c != leader_id:
                seen.add(c)
                stack.append(c)
    return sorted(seen)


__all__ = [
    "Ctx", "load_ctx", "resolve_period", "previous_period", "score_person", "score_many", "group_summary", "public", "attention_reasons",
    "sparkline", "unit_member_ids", "region_unit_ids", "reporting_line", "unit_at", "scored_months", "SMALL_UNIT", "PeriodError", "get_org_config",
]
