"""
Staff Performance - files: filled appraisal forms (upload -> extraction -> human review -> confirm), monthly-figure evidence,
and the editable blank appraisal template.  Prefix: /api/admin/performance

  POST   /appraisals/{id}/upload         stream a PDF/DOCX/PNG/JPG (type from content, size capped while streaming, stored under a generated name)
  POST   /appraisals/{id}/extract        read it: pseudonymised AI mapping -> a DRAFT (nothing counts yet)
  GET    /appraisals/{id}/extraction     the draft beside the document text and the current form (for the side-by-side review)
  POST   /appraisals/{id}/extraction/confirm   a person confirms the reviewed values -> only now are they written to the appraisal
  DELETE /appraisals/{id}/extraction     throw the draft away (the original file stays attached)
  GET    /appraisals/{id}/file           the original, as an attachment (audited)
  GET    /roles/{id}/appraisal-template.docx
  POST   /entries/{entry_id}/evidence    attach a PDF/PNG/JPG to a monthly figure     GET .../evidence  download it
Every route has the performance menu guard AND the appraisal/person must be in the caller's scope (404 otherwise).
Uploads and AI calls are rate-limited per user. Audit entries carry ids only.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import Appraisal, KpiEntry, PerformanceRole, StaffProfile
from app.routers.admin_performance import _profile_in_scope, manage_access
from app.routers.admin_performance_appraisals import _audit, _load
from app.services import performance_ai as ai
from app.services import performance_appraisal_docx as docx_form
from app.services import performance_appraisal_pdf as pdf
from app.services import performance_appraisals as pa
from app.services import performance_entries as pe
from app.services import performance_extraction as ex
from app.services import performance_ocr as ocr
from app.services import performance_storage as storage
from app.services import performance_upload as upload
from app.services.auth import get_current_admin
from app.services.performance_config import log_audit
from app.services.performance_ratelimit import rate_limited
from app.services.performance_roles import kpis_for, latest_version
from app.services.performance_scope import PerformanceScope, get_performance_scope

router = APIRouter(prefix="/api/admin/performance", tags=["admin-performance-files"])
upload_limit = Depends(rate_limited("perf-upload", limit=20, window_seconds=3600))
ai_limit = Depends(rate_limited("perf-extract", limit=6, window_seconds=60))
SOURCE_TEXT_CAP = 20000


def _attachment(data: bytes, name: str, kind: str) -> Response:
    return Response(content=data, media_type=upload.CONTENT_TYPES.get(kind, "application/octet-stream"),
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


def _may_record(scope: PerformanceScope, profile: StaffProfile, ap: Appraisal) -> bool:
    """Who may attach and confirm a filled form: HR/admin, the person themselves, or their direct manager. Never the read-only executive."""
    if scope.read_only or ap.status == "finalised":
        return False
    own = scope.own_profile_id
    return scope.is_hr or scope.is_admin or (own is not None and (own == profile.id or profile.manager_id == own))


def _need_record(scope: PerformanceScope, profile: StaffProfile, ap: Appraisal) -> None:
    if ap.status == "finalised":
        raise HTTPException(status_code=409, detail="This appraisal is finalised and read-only.")
    if not _may_record(scope, profile, ap):
        raise HTTPException(status_code=403, detail="You can't attach or confirm a form for this appraisal.")


_TEXT_CACHE: "OrderedDict[str, tuple[str, float | None]]" = OrderedDict()  # path -> (text, OCR confidence); a few entries, never persisted


def _source_text(ap: Appraisal) -> tuple[str, float | None]:
    """The document's text and, for a picture or scanned PDF, how well it was read (0-100). Reading a scan can take a while: call from a worker thread."""
    path = ap.uploaded_file_path or ""
    if path in _TEXT_CACHE:
        return _TEXT_CACHE[path]
    kind = path.rsplit(".", 1)[-1]
    if kind not in ("pdf", "docx", "png", "jpg"):
        raise HTTPException(status_code=422, detail="There is no readable form attached.")
    try:
        data = storage.get(path)
        conf: float | None = None
        if kind in ("png", "jpg"):
            res = ocr.read_image(data)
            text, conf = res.text, res.confidence
        else:
            try:
                text = upload.extract_text(kind, data)
            except HTTPException as exc:
                if kind != "pdf" or exc.status_code != 422 or "scan" not in str(exc.detail):
                    raise
                res = ocr.read_scanned_pdf(data)  # no text layer: a scanned PDF
                text, conf = res.text, res.confidence
    except storage.StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ocr.OcrUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"{exc} Please enter the appraisal by hand (the original stays attached).") from exc
    except ocr.OcrError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _TEXT_CACHE[path] = (text, conf)
    while len(_TEXT_CACHE) > 8:
        _TEXT_CACHE.popitem(last=False)
    return text, conf


# ---- appraisal form upload / extraction / review ---------------------------------------------------------------------


@router.post("/appraisals/{appraisal_id}/upload", dependencies=[manage_access, upload_limit])
async def upload_form(appraisal_id: str, request: Request, filename: str = Query(default="form.pdf", max_length=150), db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    ap, profile, _ = _load(db, scope, appraisal_id)
    _need_record(scope, profile, ap)
    data = await upload.read_limited(request.stream(), max_bytes=upload.MAX_UPLOAD_BYTES, declared_length=request.headers.get("content-length"))
    scan = upload.sniff_form_scan(data, filename)
    kind = scan or upload.sniff_kind(data, filename)
    if kind == "docx":
        upload._docx_text(data)  # noqa: SLF001 - zip-bomb and structure check before anything is stored
    path = storage.new_path("appraisals", ap.id, kind)
    try:
        storage.put(path, data, upload.CONTENT_TYPES[kind])
    except storage.StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if ap.uploaded_file_path:
        storage.remove(ap.uploaded_file_path)
    ap.uploaded_file_path, ap.uploaded_original_name, ap.source = path, "".join(ch for ch in filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if ch.isprintable())[:150] or f"form.{kind}", "upload"
    can_read = not scan or ocr.available()
    ap.extraction, ap.extraction_status = None, "none" if can_read else "failed"
    db.commit()
    _audit(db, admin, "appraisal_form_uploaded", ap, kind=kind)
    note = "Uploaded. Next: read it to prepare a draft for review." if can_read else "That's a photo or scan and reading pictures isn't set up on this server. It is attached; please enter the appraisal by hand."
    return {"message": note, "extraction_status": ap.extraction_status, "can_extract": can_read, "file_name": ap.uploaded_original_name}


@router.post("/appraisals/{appraisal_id}/extract", dependencies=[manage_access, ai_limit])
async def extract_form(appraisal_id: str, regenerate: bool = False, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    ap, profile, resolver = _load(db, scope, appraisal_id)
    _need_record(scope, profile, ap)
    if not ap.uploaded_file_path:
        raise HTTPException(status_code=409, detail="Upload the filled form first.")
    text, conf = await run_in_threadpool(_source_text, ap)
    try:
        draft = await run_in_threadpool(ex.propose, db, ap, profile, resolver, text, admin.username, regenerate=regenerate, ocr_confidence=conf)
    except ai.AIUnavailable as exc:
        ap.extraction_status = "failed"
        db.commit()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (ai.AIFailed, ai.AIOutputInvalid) as exc:
        ap.extraction_status = "failed"
        db.commit()
        raise HTTPException(status_code=502, detail=f"{exc} The original file is still attached - you can enter the appraisal manually.") from exc
    ap.extraction, ap.extraction_status = draft, "extracted"
    db.commit()
    _audit(db, admin, "appraisal_form_extracted", ap, low_confidence=draft["low_confidence_count"], cached=draft["ai"]["cached"])
    return {"extraction_status": ap.extraction_status, "draft": draft}


@router.get("/appraisals/{appraisal_id}/extraction", dependencies=[manage_access])
def review_extraction(appraisal_id: str, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    ap, profile, _ = _load(db, scope, appraisal_id)
    if not _may_record(scope, profile, ap) and not (scope.is_hr or scope.is_admin):
        raise HTTPException(status_code=404, detail="Appraisal not found.")
    parts = pa.load_parts(db, ap)
    try:
        text_full, ocr_conf = _source_text(ap) if ap.uploaded_file_path else (None, None)
        text, text_note = (text_full[:SOURCE_TEXT_CAP] if text_full else None), None
    except HTTPException as exc:
        text, text_note, ocr_conf = None, exc.detail, None
    _audit(db, admin, "appraisal_extraction_viewed", ap)
    return {
        "extraction_status": ap.extraction_status, "file_name": ap.uploaded_original_name, "file_kind": (ap.uploaded_file_path or "").rsplit(".", 1)[-1] or None,
        "draft": ap.extraction, "ocr_confidence": round(ocr_conf, 1) if ocr_conf is not None else None, "source_text": text, "source_text_note": text_note, "can_confirm": _may_record(scope, profile, ap),
        "can_confirm_manager_column": _may_record(scope, profile, ap) and scope.own_profile_id != profile.id,
        "rows": {
            "items": [{"kpi_key": i.kpi_key, "label": i.label, "weight": float(i.weight), "self_text": i.self_text, "self_rating": i.self_rating, "manager_rating": i.manager_rating} for i in parts.items],
            "competencies": [{"key": c.competency_key, "title": c.title, "self_rating": c.self_rating, "manager_rating": c.manager_rating} for c in parts.competencies],
            "objectives": [{"label": o.label, "values": list(o.values or [])} for o in parts.objectives],
            "development_needs": [{"slot": n.slot, "text": n.text} for n in parts.needs],
        },
    }


class ReviewedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kpi_key: str = Field(max_length=60)
    self_text: str | None = Field(default=None, max_length=4000)
    self_rating: int | None = Field(default=None, ge=1, le=5)
    manager_rating: int | None = Field(default=None, ge=1, le=5)


class ReviewedCompetency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(max_length=60)
    self_rating: int | None = Field(default=None, ge=1, le=5)
    manager_rating: int | None = Field(default=None, ge=1, le=5)


class ReviewedObjective(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=200)
    values: list[str] = Field(min_length=3, max_length=3)


class ReviewedNeed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: int = Field(ge=1, le=2)
    text: str | None = Field(default=None, max_length=1000)


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool
    items: list[ReviewedItem] = Field(default_factory=list, max_length=60)
    competencies: list[ReviewedCompetency] = Field(default_factory=list, max_length=30)
    objectives: list[ReviewedObjective] = Field(default_factory=list, max_length=30)
    development_needs: list[ReviewedNeed] = Field(default_factory=list, max_length=2)


@router.post("/appraisals/{appraisal_id}/extraction/confirm", dependencies=[manage_access])
def confirm_extraction(appraisal_id: str, body: ConfirmRequest, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    ap, profile, _ = _load(db, scope, appraisal_id)
    _need_record(scope, profile, ap)
    if not body.confirm:
        raise HTTPException(status_code=422, detail="Please tick the confirmation: only values a person has checked are saved.")
    if ap.extraction_status != "extracted":
        raise HTTPException(status_code=409, detail="There is no draft waiting for review.")
    counts = ex.apply_confirmed(db, ap, body.model_dump(exclude_unset=True), include_manager=scope.own_profile_id != profile.id)
    ap.extraction_status = "confirmed"
    db.commit()
    _audit(db, admin, "appraisal_extraction_confirmed", ap, **counts)
    return {"message": "Confirmed. The reviewed values are now on the appraisal; the normal sign-off steps continue from here.", "applied": counts}


@router.delete("/appraisals/{appraisal_id}/extraction", dependencies=[manage_access])
def discard_extraction(appraisal_id: str, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    ap, profile, _ = _load(db, scope, appraisal_id)
    _need_record(scope, profile, ap)
    if ap.extraction_status == "confirmed":
        raise HTTPException(status_code=409, detail="That draft was already confirmed.")
    ap.extraction, ap.extraction_status = None, "none"
    db.commit()
    _audit(db, admin, "appraisal_extraction_discarded", ap)
    return {"message": "Draft discarded. The original file is still attached."}


@router.get("/appraisals/{appraisal_id}/file", dependencies=[manage_access])
def download_form(appraisal_id: str, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> Response:
    scope = get_performance_scope(admin, db)
    ap, profile, _ = _load(db, scope, appraisal_id)
    if not ap.uploaded_file_path or not storage.is_ours(ap.uploaded_file_path):
        raise HTTPException(status_code=404, detail="There is no uploaded form on this appraisal.")
    try:
        data = storage.get(ap.uploaded_file_path)
    except storage.StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    _audit(db, admin, "appraisal_file_downloaded", ap)
    kind = ap.uploaded_file_path.rsplit(".", 1)[-1]
    return _attachment(data, f"appraisal-{ap.id[:8]}.{kind}", kind)


@router.get("/roles/{role_id}/appraisal-template.docx", dependencies=[manage_access])
def blank_template_docx(role_id: str, db: Session = Depends(get_db)) -> Response:
    role = db.get(PerformanceRole, role_id)
    version = latest_version(db, role_id) if role else None
    if role is None or version is None:
        raise HTTPException(status_code=404, detail="That job role has no job description yet.")
    data = docx_form.render(pdf.blank_detail(role.name, version, kpis_for(db, version.id)), pdf.RATING_GUIDE)
    return _attachment(data, f"appraisal-template-{role.key}.docx", "docx")


# ---- evidence for a monthly figure ---------------------------------------------------------------------------------


def _entry_and_profile(db: Session, scope: PerformanceScope, entry_id: str) -> tuple[KpiEntry, StaffProfile]:
    entry = db.get(KpiEntry, entry_id)
    profile = db.get(StaffProfile, entry.profile_id) if entry else None
    if entry is None or profile is None or not scope.can_see(profile.id):
        raise HTTPException(status_code=404, detail="That figure wasn't found.")  # same for missing and out of scope
    return entry, profile


@router.post("/entries/{entry_id}/evidence", dependencies=[manage_access, upload_limit])
async def upload_evidence(entry_id: str, request: Request, filename: str = Query(default="evidence.pdf", max_length=150), db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> dict[str, Any]:
    scope = get_performance_scope(admin, db)
    entry, profile = _entry_and_profile(db, scope, entry_id)
    if scope.read_only or not pe.can_enter_for(scope, profile, pe.verifier_unit_ids(db, scope)):
        raise HTTPException(status_code=403, detail="You can't attach evidence for this person.")
    if entry.status in ("verified", "locked"):
        raise HTTPException(status_code=409, detail="A verified figure can't change. Ask HR to reopen it first.")
    data = await upload.read_limited(request.stream(), max_bytes=upload.EVIDENCE_MAX_BYTES, declared_length=request.headers.get("content-length"))
    kind = upload.sniff_evidence(data, filename)
    path = storage.new_path("evidence", entry.id, kind)
    try:
        storage.put(path, data, upload.CONTENT_TYPES[kind])
    except storage.StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if entry.evidence_path:
        storage.remove(entry.evidence_path)
    entry.evidence_path = path
    entry.evidence_name = "".join(ch for ch in filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if ch.isprintable())[:150] or f"evidence.{kind}"
    db.commit()
    log_audit(db, admin.username, "entry_evidence_uploaded", "kpi_entry", entry.id, {"profile_id": entry.profile_id, "kind": kind})
    return {"message": "Evidence attached.", "evidence_name": entry.evidence_name}


@router.get("/entries/{entry_id}/evidence", dependencies=[manage_access])
def download_evidence(entry_id: str, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)) -> Response:
    scope = get_performance_scope(admin, db)
    entry, _profile = _entry_and_profile(db, scope, entry_id)
    if not entry.evidence_path or not storage.is_ours(entry.evidence_path):
        raise HTTPException(status_code=404, detail="There is no evidence on this figure.")
    try:
        data = storage.get(entry.evidence_path)
    except storage.StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    log_audit(db, admin.username, "entry_evidence_downloaded", "kpi_entry", entry.id, {"profile_id": entry.profile_id})
    kind = entry.evidence_path.rsplit(".", 1)[-1]
    return _attachment(data, f"evidence-{entry.id[:8]}.{kind}", kind)
