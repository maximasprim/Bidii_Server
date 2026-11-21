"""
Appraisals: the digital version of the paper form.

Form (per KPI row): Job Objective | % Weight | Measurement Criteria | Self Assessment | Rating | Weighted Rating |
Manager Assessment (1-5). Plus behavioural competencies, objectives for the next 3 months, two training needs, and a
comment/sign-off block per step.

Workflow (the chain comes from the job role and is SNAPSHOTTED into the appraisal):
  staff fills the self-assessment -> submits -> manager rates -> [CSM] -> [RM/GM] -> [HR] -> [CEO] -> finalised
* A reviewer may RETURN it one step back (comment required). Reopening a finalised appraisal is admin/HR only, with a reason.
* Nobody acts on their own appraisal except the staff step. Admin sees everything but never signs.
* Who fills each step is resolved from the org structure by ROLE: manager = line manager; CSM = holder of the
  Customer Service Manager role; RM/GM = the nearest Regional/General Manager above the manager (else the unit's
  region manager, else a General Manager); HR = the "hr" access role; CEO = the executive. If a step has nobody, HR
  may act for it, so an appraisal can never get stuck.
* Disputes: the appraisee may dispute a rating once the manager has rated; HR resolves them (optionally changing the
  manager's rating). Open disputes block the HR step and the final step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.performance import (
    Appraisal,
    AppraisalDispute,
    AppraisalItem,
    AppraisalObjective,
    AppraisalSignoff,
    CompetencyRating,
    DevelopmentNeed,
    KpiEntry,
    PerformanceRole,
    Region,
    StaffProfile,
    UnitRegionLink,
)
from app.services.performance_periods import PeriodError, months_in, parse_period
from app.services.performance_roles import is_configured, kpis_for, latest_version, lock_version
from app.services.performance_scope import PerformanceScope
from app.services.performance_scoring import final_rating, weighted_rating
from app.services.performance_targets import in_force_map
from app.services.performance_time import today_nairobi

STEP_LABEL = {"staff": "Staff member", "manager": "Immediate manager", "csm": "Customer Service Manager", "rm_gm": "RM / GM", "hr": "HR", "ceo": "CEO"}
APPRAISAL_PERIOD_KINDS = {"quarter", "half", "year"}
RATINGS = range(1, 6)
MAX_OPEN_DISPUTES = 10


class AppraisalError(ValueError):
    """Something the user can fix; the message is safe to show."""


class Forbidden(AppraisalError):
    """The caller isn't the right person for this action (maps to HTTP 403)."""


class Conflict(AppraisalError):
    """The request clashes with the current state (maps to HTTP 409)."""


# ---- who can act -------------------------------------------------------------------------------------------------


class ActorResolver:
    """Loads the org once, so resolving a step for many appraisals costs no extra queries per row."""

    def __init__(self, db: Session):
        profiles = db.query(StaffProfile.id, StaffProfile.manager_id, StaffProfile.performance_role_id, StaffProfile.unit_id, StaffProfile.is_active, StaffProfile.is_executive).all()
        role_key = {rid: key for rid, key in db.query(PerformanceRole.id, PerformanceRole.key)}
        self.manager = {p.id: p.manager_id for p in profiles}
        self.role = {p.id: role_key.get(p.performance_role_id) for p in profiles}
        self.unit = {p.id: p.unit_id for p in profiles}
        active = [p for p in profiles if p.is_active]
        self.csm = {p.id for p in active if self.role[p.id] == "customer_service_manager"}
        self.ceo = {p.id for p in active if p.is_executive or self.role[p.id] == "ceo"}
        self.gm = {p.id for p in active if self.role[p.id] == "general_manager"}
        today = today_nairobi()
        region_of = {
            uid: rid for uid, rid in db.query(UnitRegionLink.unit_id, UnitRegionLink.region_id).filter(
                UnitRegionLink.effective_from <= today, (UnitRegionLink.effective_to.is_(None)) | (UnitRegionLink.effective_to >= today))
        }
        region_mgr = {rid: mgr for rid, mgr in db.query(Region.id, Region.manager_profile_id).filter(Region.is_active.is_(True))}
        self.unit_region_manager = {uid: region_mgr.get(rid) for uid, rid in region_of.items()}

    def rm_gm(self, profile_id: str) -> set[str]:
        found, seen, cur = set(), {profile_id}, self.manager.get(profile_id)
        cur = self.manager.get(cur) if cur else None  # start ABOVE the direct manager
        while cur and cur not in seen:
            seen.add(cur)
            if self.role.get(cur) in ("regional_manager", "general_manager"):
                found.add(cur)
                break
            cur = self.manager.get(cur)
        region_mgr = self.unit_region_manager.get(self.unit.get(profile_id) or "")
        if region_mgr:
            found.add(region_mgr)
        return found or set(self.gm)

    def actors(self, profile: StaffProfile, step: str) -> set[str]:
        if step == "staff":
            return {profile.id}
        if step == "manager":
            return {profile.manager_id} if profile.manager_id else set()
        if step == "csm":
            return set(self.csm)
        if step == "rm_gm":
            return self.rm_gm(profile.id)
        if step == "ceo":
            return set(self.ceo)
        return set()  # "hr" is by access role


def can_act(resolver: ActorResolver, scope: PerformanceScope, profile: StaffProfile, step: str | None) -> bool:
    if step is None:
        return False
    own = scope.own_profile_id
    if step == "staff":
        if own is not None and own == profile.id:
            return True
        return profile.admin_user_id is None and (scope.is_hr or (own is not None and profile.manager_id == own))
    if own is not None and own == profile.id:
        return False  # nobody signs off their own appraisal
    if step == "ceo":
        return own is not None and own in resolver.actors(profile, "ceo") or (scope.is_hr and not resolver.ceo)
    if scope.read_only:
        return False
    if step == "hr":
        return scope.is_hr
    who = resolver.actors(profile, step)
    if not who:
        return scope.is_hr  # nobody holds the role yet: HR keeps the appraisal moving
    return own is not None and own in who


def appraisal_visible(scope: PerformanceScope, resolver: ActorResolver, profile: StaffProfile, ap: Appraisal) -> bool:
    """
    Normal scope, PLUS anyone the appraisal's chain names as a reviewer (Customer Service Manager, RM/GM, CEO): a head-office
    reviewer must be able to open the appraisal they are asked to sign even though the person isn't in their reporting line.
    This opens THAT appraisal only, not the person's profile, targets or actions.
    """
    if scope.can_see(profile.id):
        return True
    own = scope.own_profile_id
    if own is None:
        return False
    return any(own in resolver.actors(profile, step) for step in ap.signoff_chain if step in ("csm", "rm_gm", "ceo"))


# ---- creating ---------------------------------------------------------------------------------------------------------


def _criteria_for(jd: dict[str, Any], kpi) -> str | None:
    for r in jd.get("responsibilities", []):
        if r.get("title") == kpi.jd_section and r.get("criteria"):
            return "\n".join(r["criteria"])
    return kpi.description or None


def _fmt(n: Decimal) -> str:
    return f"{n:,.0f}" if n == n.to_integral_value() else f"{n:,.1f}"


def prefill_text(db: Session, profile: StaffProfile, kpi, months: list[date]) -> str | None:
    """A starting line for the self-assessment from VERIFIED monthly figures (the staff member can edit it)."""
    rows = db.query(KpiEntry).filter(KpiEntry.profile_id == profile.id, KpiEntry.kpi_key == kpi.key, KpiEntry.month.in_(months), KpiEntry.status.in_(("verified", "locked"))).order_by(KpiEntry.month).all()
    if not rows:
        return None
    if kpi.unit == "percent":
        last = rows[-1]
        return f"Latest verified ({last.month:%b %Y}): {_fmt(Decimal(last.value))}%."
    total = sum((Decimal(r.value) for r in rows), Decimal(0))
    target_sum = Decimal(0)
    for r in rows:
        t = in_force_map(db, [profile.id], r.month, [kpi.key]).get((profile.id, kpi.key))
        target_sum += Decimal(t.value) if t else Decimal(0)
    unit = "KES " if kpi.unit == "kes" else ""
    if target_sum > 0 and kpi.direction == "higher":
        pct = (total / target_sum * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        return f"Verified over the period: {unit}{_fmt(total)} against a target of {unit}{_fmt(target_sum)} ({pct}%)."
    return f"Verified over the period: {unit}{_fmt(total)}."


def create_appraisal(db: Session, profile: StaffProfile, period_key: str, username: str) -> Appraisal:
    try:
        period = parse_period(period_key)
    except PeriodError as exc:
        raise AppraisalError(str(exc)) from exc
    if period.kind not in APPRAISAL_PERIOD_KINDS:
        raise AppraisalError("Appraisals cover a quarter (2026-Q1), a half-year (2026-H1) or a full year (2026).")
    if db.query(Appraisal.id).filter_by(profile_id=profile.id, period_key=period.key).first() is not None:
        raise Conflict(f"There is already an appraisal for {period.label}.")
    version = latest_version(db, profile.performance_role_id)
    kpis = kpis_for(db, version.id) if version else []
    if not is_configured(version, kpis):
        raise AppraisalError("This person's job role has no complete KPI set yet (weights must total 100), so it can't be appraised.")
    jd = version.jd or {}
    chain = list(version.signoff_chain or [])
    if version.requires_ceo_signoff:
        chain.append("ceo")
    ap = Appraisal(
        profile_id=profile.id, period_key=period.key, period_start=period.start, period_end=period.end, role_id=profile.performance_role_id,
        role_version_id=version.id, status="draft", current_step="staff", signoff_chain=chain, created_by_username=username,
    )
    db.add(ap)
    db.flush()
    months = months_in(period)
    for i, k in enumerate(kpis, 1):
        db.add(AppraisalItem(appraisal_id=ap.id, kpi_key=k.key, label=k.label, weight=k.weight, criteria=_criteria_for(jd, k), self_text=prefill_text(db, profile, k, months) if k.source == "kpi_entry" else None, sort_order=i))
    for i, c in enumerate(jd.get("competencies", []), 1):
        db.add(CompetencyRating(appraisal_id=ap.id, competency_key=c["key"], title=c["title"], sort_order=i))
    for i, label in enumerate(jd.get("objective_rows", []), 1):
        db.add(AppraisalObjective(appraisal_id=ap.id, label=label, values=["", "", ""], sort_order=i))
    for slot in (1, 2):
        db.add(DevelopmentNeed(appraisal_id=ap.id, slot=slot, text=None))
    db.commit()
    lock_version(db, version.id)  # scored data now depends on this version of the role
    db.refresh(ap)
    return ap


# ---- loading ----------------------------------------------------------------------------------------------------------


@dataclass
class Parts:
    items: list[AppraisalItem]
    competencies: list[CompetencyRating]
    objectives: list[AppraisalObjective]
    needs: list[DevelopmentNeed]
    signoffs: list[AppraisalSignoff]
    disputes: list[AppraisalDispute]


def load_parts(db: Session, ap: Appraisal) -> Parts:
    q = lambda m, *order: db.query(m).filter(m.appraisal_id == ap.id).order_by(*order).all()  # noqa: E731
    return Parts(
        q(AppraisalItem, AppraisalItem.sort_order), q(CompetencyRating, CompetencyRating.sort_order), q(AppraisalObjective, AppraisalObjective.sort_order),
        q(DevelopmentNeed, DevelopmentNeed.slot), q(AppraisalSignoff, AppraisalSignoff.signed_at, AppraisalSignoff.id), q(AppraisalDispute, AppraisalDispute.created_at),
    )


def _rating(v: Any, what: str) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v not in RATINGS:
        raise AppraisalError(f"{what}: a rating is a whole number from 1 to 5.")
    return v


def _text(v: Any, what: str, limit: int) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise AppraisalError(f"{what} must be text.")
    v = v.strip()
    if len(v) > limit:
        raise AppraisalError(f"{what} is too long (max {limit} characters).")
    return v or None


# ---- editing the form --------------------------------------------------------------------------------------------------

STAFF_FIELDS = {"items": {"self_text", "self_rating"}, "competencies": {"self_rating"}}
MANAGER_FIELDS = {"items": {"manager_rating"}, "competencies": {"manager_rating"}}


def save_form(db: Session, ap: Appraisal, profile: StaffProfile, resolver: ActorResolver, scope: PerformanceScope, payload: dict[str, Any]) -> None:
    if ap.status == "finalised":
        raise Conflict("This appraisal is finalised and read-only.")
    step = ap.current_step
    if step not in ("staff", "manager"):
        raise Forbidden("Ratings are set by the staff member and their manager; reviewers add a comment when they sign.")
    if not can_act(resolver, scope, profile, step):
        raise Forbidden("It isn't your turn to edit this appraisal.")
    allowed = STAFF_FIELDS if step == "staff" else MANAGER_FIELDS
    parts = load_parts(db, ap)
    items, comps = {i.kpi_key: i for i in parts.items}, {c.competency_key: c for c in parts.competencies}

    for row in payload.get("items") or []:
        item = items.get(row.get("kpi_key"))
        if item is None:
            raise AppraisalError("One of the KPI rows isn't on this appraisal.")
        extra = set(row) - {"kpi_key"} - allowed["items"]
        if extra:
            raise Forbidden(f"At this step you can't change: {', '.join(sorted(extra))}.")
        if "self_text" in row:
            item.self_text = _text(row["self_text"], f'"{item.label}" self-assessment', 4000)
        if "self_rating" in row:
            item.self_rating = _rating(row["self_rating"], item.label)
        if "manager_rating" in row:
            item.manager_rating = _rating(row["manager_rating"], item.label)
    for row in payload.get("competencies") or []:
        c = comps.get(row.get("key"))
        if c is None:
            raise AppraisalError("One of the competencies isn't on this appraisal.")
        extra = set(row) - {"key"} - allowed["competencies"]
        if extra:
            raise Forbidden(f"At this step you can't change: {', '.join(sorted(extra))}.")
        if "self_rating" in row:
            c.self_rating = _rating(row["self_rating"], c.title)
        if "manager_rating" in row:
            c.manager_rating = _rating(row["manager_rating"], c.title)
    objectives = {o.label: o for o in parts.objectives}
    for row in payload.get("objectives") or []:
        o = objectives.get(row.get("label"))
        vals = row.get("values")
        if o is None or not isinstance(vals, list) or len(vals) != 3:
            raise AppraisalError("Each objective needs its row name and three monthly values.")
        o.values = [(_text(v, "An objective", 300) or "") for v in vals]
    needs = {n.slot: n for n in parts.needs}
    for row in payload.get("development_needs") or []:
        n = needs.get(row.get("slot"))
        if n is None:
            raise AppraisalError("A training need is slot 1 or 2.")
        n.text = _text(row.get("text"), "A training need", 1000)
    db.commit()


# ---- moving through the chain ------------------------------------------------------------------------------------------


def _record(db: Session, ap: Appraisal, step: str, decision: str, comment: str | None, username: str) -> None:
    db.add(AppraisalSignoff(appraisal_id=ap.id, step=step, decision=decision, comment=comment, actor_username=username))


def _next(ap: Appraisal) -> str | None:
    chain = list(ap.signoff_chain)
    i = chain.index(ap.current_step)
    return chain[i + 1] if i + 1 < len(chain) else None


def submit_self(db: Session, ap: Appraisal, profile: StaffProfile, resolver: ActorResolver, scope: PerformanceScope, username: str) -> None:
    if ap.status != "draft" or ap.current_step != "staff":
        raise Conflict("This appraisal isn't waiting for the staff member's self-assessment.")
    if not can_act(resolver, scope, profile, "staff"):
        raise Forbidden("Only the staff member (or HR / their manager, if they have no login) can submit their self-assessment.")
    parts = load_parts(db, ap)
    missing = [i.label for i in parts.items if i.self_rating is None] + [c.title for c in parts.competencies if c.self_rating is None]
    if missing:
        raise AppraisalError("Rate yourself on every row first. Still to rate: " + ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "."))
    _record(db, ap, "staff", "signed", None, username)
    ap.status, ap.current_step = "in_review", _next(ap)
    db.commit()


def finalise(db: Session, ap: Appraisal, config: dict[str, Any]) -> None:
    parts = load_parts(db, ap)
    authority = config.get("rating_authority", "manager")
    for item in parts.items:
        fr = final_rating(item.self_rating, item.manager_rating, authority)
        item.final_rating = fr.quantize(Decimal("0.1")) if fr is not None else None
    wr = weighted_rating([(i.weight, i.final_rating) for i in parts.items])
    ap.final_weighted_rating = wr.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if wr is not None else None
    ap.status, ap.current_step, ap.finalised_at = "finalised", None, datetime.now(timezone.utc)


def sign(
    db: Session, ap: Appraisal, profile: StaffProfile, resolver: ActorResolver, scope: PerformanceScope, username: str,
    comment: str | None, decision: str, config: dict[str, Any],
) -> str:
    """Signs (or returns) the CURRENT step. Returns 'finalised' or the new current step."""
    if ap.status != "in_review":
        raise Conflict("This appraisal isn't with a reviewer right now.")
    step = ap.current_step
    if not can_act(resolver, scope, profile, step):
        raise Forbidden("It isn't your turn on this appraisal.")
    note = _text(comment, "The comment", 3000)
    parts = load_parts(db, ap)
    chain = list(ap.signoff_chain)
    if decision == "return":
        if not note or len(note) < 3:
            raise AppraisalError("Say why you're returning it.")
        prev = chain[chain.index(step) - 1]
        _record(db, ap, step, "returned", note, username)
        ap.current_step = prev
        ap.status = "draft" if prev == "staff" else "in_review"
        db.commit()
        return prev
    if decision != "sign":
        raise AppraisalError("Choose sign or return.")
    is_last = step == chain[-1]
    if step == "manager":
        missing = [i.label for i in parts.items if i.manager_rating is None] + [c.title for c in parts.competencies if c.manager_rating is None]
        if missing:
            raise AppraisalError("Rate every row before signing. Still to rate: " + ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "."))
    if (step == "hr" or is_last) and any(d.status == "open" for d in parts.disputes):
        raise Conflict("There are open disputes. HR must resolve them before this step can be signed.")
    _record(db, ap, step, "signed", note, username)
    if is_last:
        finalise(db, ap, config)
        db.commit()
        return "finalised"
    ap.current_step = chain[chain.index(step) + 1]
    db.commit()
    return ap.current_step


# ---- disputes, acknowledgement, reopening ----------------------------------------------------------------------------------


def _is_appraisee_or_proxy(scope: PerformanceScope, profile: StaffProfile) -> bool:
    own = scope.own_profile_id
    if own is not None and own == profile.id:
        return True
    return profile.admin_user_id is None and (scope.is_hr or (own is not None and profile.manager_id == own))


def raise_dispute(db: Session, ap: Appraisal, profile: StaffProfile, scope: PerformanceScope, username: str, item_key: str | None, reason: str) -> AppraisalDispute:
    if not _is_appraisee_or_proxy(scope, profile):
        raise Forbidden("Only the person being appraised can dispute a rating.")
    chain = list(ap.signoff_chain)
    rated = ap.status == "finalised" or (ap.status == "in_review" and ap.current_step in chain and chain.index(ap.current_step) > chain.index("manager"))
    if not rated:
        raise Conflict("You can dispute a rating once your manager has rated you.")
    text = _text(reason, "The reason", 2000)
    if not text or len(text) < 5:
        raise AppraisalError("Please explain what you disagree with.")
    parts = load_parts(db, ap)
    if item_key is not None and item_key not in {i.kpi_key for i in parts.items}:
        raise AppraisalError("That KPI isn't on this appraisal.")
    if sum(1 for d in parts.disputes if d.status == "open") >= MAX_OPEN_DISPUTES:
        raise Conflict("There are already several open disputes on this appraisal; wait for HR to resolve them.")
    d = AppraisalDispute(appraisal_id=ap.id, item_key=item_key, reason=text, raised_by_username=username)
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def resolve_dispute(db: Session, ap: Appraisal, profile: StaffProfile, dispute: AppraisalDispute, scope: PerformanceScope, username: str, resolution: str, new_manager_rating: int | None) -> None:
    if not scope.is_hr or (scope.own_profile_id is not None and scope.own_profile_id == profile.id):
        raise Forbidden("Only HR (not the person concerned) can resolve a dispute.")
    if dispute.status != "open":
        raise Conflict("That dispute is already resolved.")
    text = _text(resolution, "The resolution", 2000)
    if not text or len(text) < 3:
        raise AppraisalError("Say how it was resolved.")
    rating = _rating(new_manager_rating, "The new rating")
    if rating is not None:
        if ap.status == "finalised":
            raise Conflict("Reopen the appraisal before changing a rating.")
        if dispute.item_key is None:
            raise AppraisalError("A rating change needs a specific KPI row.")
        item = db.query(AppraisalItem).filter_by(appraisal_id=ap.id, kpi_key=dispute.item_key).one()
        text += f" (Manager rating changed from {item.manager_rating} to {rating}.)"
        item.manager_rating = rating
        dispute.new_manager_rating = rating
    dispute.status, dispute.resolution, dispute.resolved_by_username, dispute.resolved_at = "resolved", text, username, datetime.now(timezone.utc)
    db.commit()


def acknowledge(db: Session, ap: Appraisal, profile: StaffProfile, scope: PerformanceScope, username: str) -> None:
    if ap.status != "finalised":
        raise Conflict("You can acknowledge an appraisal once it is finalised.")
    if not _is_appraisee_or_proxy(scope, profile):
        raise Forbidden("Only the person being appraised can acknowledge it.")
    if ap.acknowledged_at is not None:
        raise Conflict("Already acknowledged.")
    ap.acknowledged_at, ap.acknowledged_by_username = datetime.now(timezone.utc), username
    db.commit()


def reopen(db: Session, ap: Appraisal, profile: StaffProfile, scope: PerformanceScope, username: str, reason: str) -> None:
    if not (scope.is_admin or scope.is_hr) or (scope.own_profile_id is not None and scope.own_profile_id == profile.id):
        raise Forbidden("Only HR or an administrator (not for their own appraisal) can reopen an appraisal.")
    if ap.status != "finalised":
        raise Conflict("Only a finalised appraisal can be reopened.")
    why = _text(reason, "The reason", 300)
    if not why or len(why) < 3:
        raise AppraisalError("Please give a reason for reopening it.")
    parts = load_parts(db, ap)
    for i in parts.items:
        i.final_rating = None
    _record(db, ap, "reopened", "returned", why, username)
    ap.status, ap.current_step = "in_review", "manager"
    ap.final_weighted_rating = ap.finalised_at = ap.acknowledged_at = ap.acknowledged_by_username = None
    db.commit()
