"""
Staff Performance - job-role JDs, KPIs, versions and AI drafting.

Prefix: /api/admin/performance  (same module as admin_performance.py; split for size)

  reads            menu "/admin/performance"  (any staff may see how a role is measured)
  saves, AI        admin only; AI endpoints are also rate-limited per user

AI endpoints only ever return DRAFTS. Saving is a separate, explicit PUT by an admin.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import PerformanceKpi, PerformanceRole, PerformanceRoleVersion
from app.routers.admin_performance import admin_or_hr, manage_access
from app.schemas.performance import (
    JDExtractResponse,
    JDOptions,
    KpiRead,
    KpiSuggestRequest,
    KpiSuggestResponse,
    RoleVersionRead,
    RoleVersionSave,
    VersionListResponse,
    VersionSummary,
)
from app.services import performance_ai as ai
from app.services import performance_jd_import as jd_import
from app.services import performance_roles as roles
from app.services import performance_upload as upload
from app.services.auth import get_current_admin
from app.services.performance_config import log_audit
from app.services.performance_ratelimit import rate_limited
from app.services.performance_role_templates import DEFAULT_COMPETENCIES

router = APIRouter(prefix="/api/admin/performance", tags=["admin-performance-roles"])
ai_rate_limit = Depends(rate_limited("ai"))


def _role_or_404(db: Session, role_id: str) -> PerformanceRole:
    role = db.get(PerformanceRole, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Performance role not found.")
    return role


def _blank_detail(role: PerformanceRole) -> RoleVersionRead:
    jd = roles.validate_jd({"title": role.name, "department": role.department or "", "competencies": DEFAULT_COMPETENCIES}, [])
    return RoleVersionRead(
        role_id=role.id, role_key=role.key, role_name=role.name, version=0, is_latest=True, locked=False, jd=jd, kpis=[],
        signoff_chain=["staff", "manager", "rm_gm", "hr"], effective_signoff_chain=["staff", "manager", "rm_gm", "hr"],
        requires_ceo_signoff=False, weights={"responsibilities_total": 0.0, "kpis_total": 0.0}, configured=False,
    )


def _detail(db: Session, role: PerformanceRole, version: PerformanceRoleVersion, *, created_new: bool = False) -> RoleVersionRead:
    latest = roles.latest_version(db, role.id)
    kpis = roles.kpis_for(db, version.id)
    return RoleVersionRead(
        role_id=role.id, role_key=role.key, role_name=role.name, version=version.version,
        is_latest=latest is not None and latest.id == version.id, locked=version.locked_at is not None,
        note=version.note, created_by=version.created_by, created_at=version.created_at, jd=version.jd or {},
        kpis=[KpiRead.model_validate(k) for k in kpis], signoff_chain=list(version.signoff_chain or []),
        effective_signoff_chain=roles.effective_signoff_chain(version), requires_ceo_signoff=version.requires_ceo_signoff,
        weights=roles.weights_summary(version, kpis), configured=roles.is_configured(version, kpis), created_new_version=created_new,
    )


@router.get("/jd-options", response_model=JDOptions, dependencies=[manage_access])
def jd_options():
    return JDOptions(
        kpi_units=roles.KPI_UNITS, kpi_directions=roles.KPI_DIRECTIONS, kpi_sources=roles.KPI_SOURCES,
        kpi_frequencies=roles.KPI_FREQUENCIES, signoff_steps=roles.SIGNOFF_STEPS, default_competencies=DEFAULT_COMPETENCIES,
    )


@router.get("/roles/{role_id}/jd", response_model=RoleVersionRead, dependencies=[manage_access])
def current_jd(role_id: str, db: Session = Depends(get_db)):
    role = _role_or_404(db, role_id)
    version = roles.latest_version(db, role.id)
    return _detail(db, role, version) if version else _blank_detail(role)


@router.get("/roles/{role_id}/versions", response_model=VersionListResponse, dependencies=[manage_access])
def list_versions(role_id: str, db: Session = Depends(get_db)):
    role = _role_or_404(db, role_id)
    versions = db.query(PerformanceRoleVersion).filter_by(role_id=role.id).order_by(PerformanceRoleVersion.version.desc()).all()
    counts: dict[str, int] = {}
    for (vid,) in db.query(PerformanceKpi.role_version_id).filter(PerformanceKpi.role_version_id.in_([v.id for v in versions] or [""])):
        counts[vid] = counts.get(vid, 0) + 1
    top = versions[0].version if versions else None
    return VersionListResponse(items=[
        VersionSummary(version=v.version, is_latest=v.version == top, locked=v.locked_at is not None, note=v.note,
                       created_by=v.created_by, created_at=v.created_at, kpi_count=counts.get(v.id, 0))
        for v in versions
    ])


@router.get("/roles/{role_id}/versions/{version}", response_model=RoleVersionRead, dependencies=[manage_access])
def get_version(role_id: str, version: int, db: Session = Depends(get_db)):
    role = _role_or_404(db, role_id)
    found = roles.get_version(db, role.id, version)
    if found is None:
        raise HTTPException(status_code=404, detail="That version doesn't exist.")
    return _detail(db, role, found)


@router.put("/roles/{role_id}/jd", response_model=RoleVersionRead, dependencies=[admin_or_hr])
def save_jd(role_id: str, payload: RoleVersionSave, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """Saves the JD + KPIs + sign-off chain. Updates the current version while unused; creates the next once it is locked."""
    role = _role_or_404(db, role_id)
    try:
        cleaned = roles.validate_version_payload(payload.jd, payload.kpis, payload.signoff_chain, payload.requires_ceo_signoff)
    except roles.JDValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    version, created_new = roles.save_version(db, role, cleaned, current_admin.username, payload.note)
    log_audit(db, current_admin.username, "role_version_saved", "performance_role", role.id, {"version": version.version, "created_new": created_new})
    return _detail(db, role, version, created_new=created_new)


@router.post("/ai/extract-jd", response_model=JDExtractResponse, dependencies=[admin_or_hr, ai_rate_limit])
async def extract_jd(
    request: Request,
    filename: str = Query(..., min_length=1, max_length=200, description="Original file name; used only to check the extension."),
    regenerate: bool = False,
    force_ai: bool = Query(False, description="Skip the exact layout reader and always use AI (DOCX files in the house layout are otherwise read directly)."),
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
):
    """
    Body = the raw PDF/DOCX bytes. Returns a DRAFT JD; nothing is saved and the file itself is never
    stored. The type is decided from the content, not from Content-Type.

    A DOCX in the company's JD layout is read DIRECTLY from its tables (exact, free, no AI key needed,
    nothing leaves the server). Anything else - a PDF, a free-form document - goes to AI as before.
    """
    data = await upload.read_limited(request.stream(), declared_length=request.headers.get("content-length"))
    kind = upload.sniff_kind(data, filename)
    text = await run_in_threadpool(upload.extract_text, kind, data)
    if kind == "docx" and not force_ai:
        local = await run_in_threadpool(jd_import.read_docx, data)
        if local is not None:
            log_audit(db, current_admin.username, "jd_read_from_docx", "performance_role", None, {"reader": local["ai"]["prompt_version"]})
            return local
    redacted = upload.redact_for_ai(text)
    try:
        result = await run_in_threadpool(ai.extract_jd_draft, db, redacted, current_admin.username, regenerate=regenerate)
    except ai.AIUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (ai.AIFailed, ai.AIOutputInvalid) as exc:
        raise HTTPException(status_code=502, detail=f"{exc} Please enter the job description manually.") from exc
    log_audit(db, current_admin.username, "ai_jd_extracted", "performance_role", None, {"provider": result["ai"]["provider"], "cached": result["ai"]["cached"]})
    return result


@router.post("/ai/suggest-kpis", response_model=KpiSuggestResponse, dependencies=[admin_or_hr, ai_rate_limit])
def suggest_kpis(payload: KpiSuggestRequest, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)):
    """Proposed KPI rows (a draft). Weights come from the responsibilities, never from the AI; a rules proposal is used if AI is unavailable."""
    errors: list[str] = []
    jd = roles.validate_jd({"title": "x", "responsibilities": payload.responsibilities}, errors)
    resps = jd.get("responsibilities", [])
    hard = [e for e in errors if "add up to" not in e and "total 100" not in e]
    if hard:
        raise HTTPException(status_code=400, detail="; ".join(hard[:6]))
    if not resps:
        raise HTTPException(status_code=400, detail="Add at least one key responsibility first.")
    result = ai.suggest_kpis(db, resps, current_admin.username, regenerate=payload.regenerate)
    log_audit(db, current_admin.username, "ai_kpis_suggested", "performance_role", None, {"ai_used": result["ai_used"]})
    return result
