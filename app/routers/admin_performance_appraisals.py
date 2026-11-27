"""
Staff Performance - appraisals (digital form, sign-off chain, disputes, PDF).

Prefix: /api/admin/performance
Every route needs the performance menu AND the caller must be allowed to see the person (out-of-scope ids are the
same 404 as missing ones). Who may DO each thing is computed by the server (see performance_appraisals.can_act) and
returned to the UI as `permissions`; the UI never decides. Viewing, downloading and every change are audited with ids
only - never any appraisal text.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import Appraisal, AppraisalDispute, PerformanceRole, StaffProfile
from app.routers.admin_performance import _profile_in_scope, admin_or_hr, manage_access
from app.schemas.performance import (
    AppraisalCreate,
    AppraisalDetail,
    AppraisalFormSave,
    AppraisalListResponse,
    AppraisalReopen,
    AppraisalSign,
    AppraisalSummary,
    AppraisalWriteResponse,
    DisputeCreate,
    DisputeResolve,
)
from app.services import performance_appraisal_pdf as pdf
from app.services import performance_appraisal_views as views
from app.services import performance_appraisals as pa
from app.services.auth import get_current_admin
from app.services.performance_config import effective_config, log_audit
from app.services.performance_periods import parse_period
from app.services.performance_roles import kpis_for, latest_version
from app.services.performance_scope import PerformanceScope, get_performance_scope
from app.services.performance_permissions import needs

router = APIRouter(prefix="/api/admin/performance", tags=["admin-performance-appraisals"])


def _http(exc: pa.AppraisalError) -> HTTPException:
    code = 403 if isinstance(exc, pa.Forbidden) else 409 if isinstance(exc, pa.Conflict) else 400
    return HTTPException(status_code=code, detail=str(exc))


def _load(db: Session, scope: PerformanceScope, appraisal_id: str) -> tuple[Appraisal, StaffProfile, pa.ActorResolver]:
    ap = db.get(Appraisal, appraisal_id)
    profile = db.get(StaffProfile, ap.profile_id) if ap else None
    resolver = pa.ActorResolver(db)
    if ap is None or profile is None or not pa.appraisal_visible(scope, resolver, profile, ap):
        raise HTTPException(status_code=404, detail="Appraisal not found.")  # identical for missing and out of scope
    return ap, profile, resolver


def _config(db: Session, profile: StaffProfile) -> dict:
    return effective_config(db, db.get(PerformanceRole, profile.performance_role_id))


def _respond(db: Session, ap: Appraisal, profile: StaffProfile, scope: PerformanceScope, message: str, resolver: pa.ActorResolver | None = None) -> AppraisalWriteResponse:
    db.refresh(ap)
    return AppraisalWriteResponse(message=message, data=AppraisalDetail(**views.detail(db, ap, profile, resolver or pa.ActorResolver(db), scope)))


def _audit(db: Session, admin: AdminUser, action: str, ap: Appraisal, **extra) -> None:
    log_audit(db, admin.username, action, "appraisal", ap.id, {"profile_id": ap.profile_id, "period": ap.period_key, **extra})


@router.post("/staff/{profile_id}/appraisals", response_model=AppraisalWriteResponse, status_code=201, dependencies=[manage_access])
def create_appraisal(profile_id: str, payload: AppraisalCreate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    profile = _profile_in_scope(db, scope, profile_id)
    own = scope.own_profile_id
    allowed = not scope.read_only and (scope.is_admin or scope.is_hr or (own is not None and (own == profile.id or profile.manager_id == own)))
    if not allowed:
        raise HTTPException(status_code=403, detail="You can't start an appraisal for this person.")
    try:
        ap = pa.create_appraisal(db, profile, payload.period, current_admin.username)
    except pa.AppraisalError as exc:
        raise _http(exc) from exc
    _audit(db, current_admin, "appraisal_created", ap)
    return _respond(db, ap, profile, scope, "Appraisal started.")


@router.get("/appraisals", response_model=AppraisalListResponse, dependencies=[manage_access])
def list_appraisals(
    profile_id: str | None = None,
    status: str = Query(default="all", pattern="^(draft|in_review|finalised|all)$"),
    period: str | None = Query(default=None, max_length=10),
    awaiting_me: bool = False,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
):
    scope = get_performance_scope(current_admin, db)
    q = db.query(Appraisal)
    if scope.visible_ids is not None:
        q = q.filter(Appraisal.profile_id.in_(scope.visible_ids or {""}))
    if profile_id:
        q = q.filter(Appraisal.profile_id == profile_id)
    if status != "all":
        q = q.filter(Appraisal.status == status)
    if period:
        q = q.filter(Appraisal.period_key == period.upper())
    rows = q.order_by(Appraisal.period_start.desc(), Appraisal.created_at.desc()).limit(300).all()
    resolver = pa.ActorResolver(db)  # one org load for the whole list
    own = scope.own_profile_id
    reviewer_roles = {"customer_service_manager", "general_manager", "regional_manager", "ceo"}
    if scope.visible_ids is not None and own is not None and (resolver.role.get(own) in reviewer_roles or own in resolver.ceo or own in resolver.unit_region_manager.values()):
        # Also the appraisals where the chain names this person as a reviewer (head-office / regional reviewers).
        others = db.query(Appraisal).filter(~Appraisal.profile_id.in_(scope.visible_ids or {""}))
        if profile_id:
            others = others.filter(Appraisal.profile_id == profile_id)
        if status != "all":
            others = others.filter(Appraisal.status == status)
        if period:
            others = others.filter(Appraisal.period_key == period.upper())
        others = others.order_by(Appraisal.period_start.desc(), Appraisal.created_at.desc()).limit(500).all()
        pmap = {p.id: p for p in db.query(StaffProfile).filter(StaffProfile.id.in_({a.profile_id for a in others} or {""}))}
        rows += [a for a in others if pa.appraisal_visible(scope, resolver, pmap[a.profile_id], a)]
        rows = sorted(rows, key=lambda a: (a.period_start, a.created_at), reverse=True)[:300]
    profiles = {p.id: p for p in db.query(StaffProfile).filter(StaffProfile.id.in_({a.profile_id for a in rows} or {""}))}
    open_counts = dict(
        db.query(AppraisalDispute.appraisal_id, func.count(AppraisalDispute.id))
        .filter(AppraisalDispute.appraisal_id.in_([a.id for a in rows] or [""]), AppraisalDispute.status == "open")
        .group_by(AppraisalDispute.appraisal_id).all()
    )
    items = []
    for a in rows:
        p = profiles[a.profile_id]
        mine = a.status != "finalised" and pa.can_act(resolver, scope, p, a.current_step)
        if awaiting_me and not mine:
            continue
        items.append(AppraisalSummary(
            id=a.id, profile_id=p.id, profile_name=p.full_name, period_key=a.period_key, period_label=parse_period(a.period_key).label, status=a.status,
            current_step=a.current_step, current_step_label=pa.STEP_LABEL.get(a.current_step) if a.current_step else None, awaiting_me=mine,
            open_disputes=open_counts.get(a.id, 0), final_weighted_rating=float(a.final_weighted_rating) if a.final_weighted_rating is not None else None,
        ))
    return AppraisalListResponse(items=items)


@router.get("/appraisals/{appraisal_id}", response_model=AppraisalDetail, dependencies=[manage_access])
def get_appraisal(appraisal_id: str, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    ap, profile, resolver = _load(db, scope, appraisal_id)
    _audit(db, current_admin, "appraisal_viewed", ap)
    return AppraisalDetail(**views.detail(db, ap, profile, resolver, scope))


@router.put("/appraisals/{appraisal_id}/form", response_model=AppraisalWriteResponse, dependencies=[manage_access])
def save_form(appraisal_id: str, payload: AppraisalFormSave, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    ap, profile, resolver = _load(db, scope, appraisal_id)
    try:
        pa.save_form(db, ap, profile, resolver, scope, payload.model_dump())
    except pa.AppraisalError as exc:
        db.rollback()
        raise _http(exc) from exc
    _audit(db, current_admin, "appraisal_form_saved", ap, step=ap.current_step)
    return _respond(db, ap, profile, scope, "Saved.")


@router.post("/appraisals/{appraisal_id}/submit", response_model=AppraisalWriteResponse, dependencies=[manage_access])
def submit_self_assessment(appraisal_id: str, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    ap, profile, resolver = _load(db, scope, appraisal_id)
    try:
        pa.submit_self(db, ap, profile, resolver, scope, current_admin.username)
    except pa.AppraisalError as exc:
        raise _http(exc) from exc
    _audit(db, current_admin, "appraisal_submitted", ap, step="staff")
    return _respond(db, ap, profile, scope, "Self-assessment submitted to your manager.")


@router.post("/appraisals/{appraisal_id}/sign", response_model=AppraisalWriteResponse, dependencies=[manage_access])
def sign_step(appraisal_id: str, payload: AppraisalSign, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    ap, profile, resolver = _load(db, scope, appraisal_id)
    step = ap.current_step
    try:
        outcome = pa.sign(db, ap, profile, resolver, scope, current_admin.username, payload.comment, payload.decision, _config(db, profile))
    except pa.AppraisalError as exc:
        raise _http(exc) from exc
    _audit(db, current_admin, "appraisal_returned" if payload.decision == "return" else "appraisal_signed", ap, step=step, to=outcome)
    message = "Returned." if payload.decision == "return" else "Appraisal finalised." if outcome == "finalised" else "Signed and passed on."
    return _respond(db, ap, profile, scope, message)


@router.post("/appraisals/{appraisal_id}/disputes", response_model=AppraisalWriteResponse, status_code=201, dependencies=[manage_access])
def raise_dispute(appraisal_id: str, payload: DisputeCreate, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    ap, profile, resolver = _load(db, scope, appraisal_id)
    try:
        d = pa.raise_dispute(db, ap, profile, scope, current_admin.username, payload.item_key, payload.reason)
    except pa.AppraisalError as exc:
        raise _http(exc) from exc
    _audit(db, current_admin, "appraisal_disputed", ap, dispute_id=d.id)
    return _respond(db, ap, profile, scope, "Your dispute was sent to HR.")


@router.post("/appraisals/{appraisal_id}/disputes/{dispute_id}/resolve", response_model=AppraisalWriteResponse, dependencies=[manage_access])
def resolve_dispute(appraisal_id: str, dispute_id: str, payload: DisputeResolve, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    ap, profile, resolver = _load(db, scope, appraisal_id)
    dispute = db.get(AppraisalDispute, dispute_id)
    if dispute is None or dispute.appraisal_id != ap.id:
        raise HTTPException(status_code=404, detail="Appraisal not found.")
    try:
        pa.resolve_dispute(db, ap, profile, dispute, scope, current_admin.username, payload.resolution, payload.new_manager_rating)
    except pa.AppraisalError as exc:
        db.rollback()
        raise _http(exc) from exc
    _audit(db, current_admin, "appraisal_dispute_resolved", ap, dispute_id=dispute.id, rating_changed=dispute.new_manager_rating is not None)
    return _respond(db, ap, profile, scope, "Dispute resolved.")


@router.post("/appraisals/{appraisal_id}/acknowledge", response_model=AppraisalWriteResponse, dependencies=[manage_access])
def acknowledge(appraisal_id: str, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    ap, profile, resolver = _load(db, scope, appraisal_id)
    try:
        pa.acknowledge(db, ap, profile, scope, current_admin.username)
    except pa.AppraisalError as exc:
        raise _http(exc) from exc
    _audit(db, current_admin, "appraisal_acknowledged", ap)
    return _respond(db, ap, profile, scope, "Thank you - acknowledged.")


@router.post("/appraisals/{appraisal_id}/reopen", response_model=AppraisalWriteResponse, dependencies=[needs("appraisals.reopen")])
def reopen(appraisal_id: str, payload: AppraisalReopen, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    ap, profile, resolver = _load(db, scope, appraisal_id)
    try:
        pa.reopen(db, ap, profile, scope, current_admin.username, payload.reason)
    except pa.AppraisalError as exc:
        raise _http(exc) from exc
    _audit(db, current_admin, "appraisal_reopened", ap)
    return _respond(db, ap, profile, scope, "Reopened for review.")


def _pdf_response(data: bytes, name: str) -> Response:
    # The file name is built here from fixed text and an id - never from anything the user typed.
    return Response(content=data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.get("/appraisals/{appraisal_id}/pdf", dependencies=[manage_access])
def appraisal_pdf(appraisal_id: str, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    scope = get_performance_scope(current_admin, db)
    ap, profile, resolver = _load(db, scope, appraisal_id)
    _audit(db, current_admin, "appraisal_downloaded", ap)
    return _pdf_response(pdf.render(views.detail(db, ap, profile, resolver, scope)), f"appraisal-{ap.id[:8]}.pdf")


@router.get("/roles/{role_id}/appraisal-template.pdf", dependencies=[manage_access])
def blank_template(role_id: str, db: Session = Depends(get_db)):
    """A blank appraisal form for a job role, laid out like the paper form."""
    role = db.get(PerformanceRole, role_id)
    version = latest_version(db, role_id) if role else None
    if role is None or version is None:
        raise HTTPException(status_code=404, detail="That job role has no job description yet.")
    data = pdf.render(pdf.blank_detail(role.name, version, kpis_for(db, version.id)), blank=True)
    return _pdf_response(data, f"appraisal-template-{role.key}.pdf")
