"""Phase 6e - monthly board pack. Admin, HR and executives only (the unrestricted scope); everything is company/branch level."""

from __future__ import annotations

import hmac
import re
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import BoardPack, StaffProfile
from app.routers.admin_performance import admin_or_hr, my_access
from app.services import performance_channels as channels
from app.services import performance_pack as pack
from app.services import performance_storage as storage
from app.services.auth import get_current_admin
from app.services.internal_notifications import notify
from app.services.performance_config import log_audit
from app.services.performance_scope import get_performance_scope
from app.services.performance_time import today_nairobi

router = APIRouter(prefix="/api/admin/performance/pack", tags=["performance-pack"])
internal = APIRouter(prefix="/api/internal/performance", tags=["performance-internal"])
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _month(key: str | None) -> date:
    if key is None:
        return pack.last_complete_month(today_nairobi())
    if not _MONTH.match(key):
        raise HTTPException(status_code=422, detail="Month must look like 2026-10.")
    y, m = key.split("-")
    return date(int(y), int(m), 1)


def _allowed(admin: AdminUser, db: Session) -> None:
    s = get_performance_scope(admin, db)
    if not (s.is_admin or s.is_hr or s.is_executive):
        raise HTTPException(status_code=404, detail="Not found.")


class Generate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str | None = None


def _row(r: BoardPack) -> dict[str, Any]:
    return {"id": r.id, "month": r.month.strftime("%Y-%m"), "label": f"{r.month:%B %Y}", "generated_at": r.generated_at.isoformat() if r.generated_at else None, "generated_by": r.generated_by, "summary": r.summary or {}}


@router.get("/preview", dependencies=[my_access])
def preview(month: str | None = None, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    _allowed(admin, db)
    return pack.data(db, _month(month), today_nairobi())


@router.get("/list", dependencies=[my_access])
def list_packs(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    _allowed(admin, db)
    return [_row(r) for r in db.query(BoardPack).order_by(BoardPack.month.desc()).limit(36)]


@router.post("/generate", dependencies=[admin_or_hr])
def generate(body: Generate, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    month = _month(body.month)
    if month > today_nairobi().replace(day=1):
        raise HTTPException(status_code=422, detail="That month hasn't started yet.")
    try:
        row = pack.generate(db, month, admin.username, today_nairobi())
    except storage.StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    log_audit(db, admin.username, "performance_pack_generate", "board_pack", row.id, {"month": month.strftime("%Y-%m")})
    return _row(row)


@router.get("/download/{pack_id}", dependencies=[my_access])
def download(pack_id: str, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> Response:
    _allowed(admin, db)
    row = db.get(BoardPack, pack_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found.")
    try:
        data = storage.get(row.file_path)
    except storage.StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    log_audit(db, admin.username, "performance_pack_download", "board_pack", row.id, None)
    return Response(content=data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="staff-performance-{row.month:%Y-%m}.pdf"', "Cache-Control": "no-store"})


@internal.post("/monthly-pack")
def monthly_pack(x_cron_secret: str | None = Header(default=None), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Cron, 1st-3rd of the month: builds last month's pack if it doesn't exist yet and tells admin, HR and executives (counts only)."""
    secret = get_settings().performance_cron_token
    if not secret:
        raise HTTPException(status_code=404, detail="Not found.")
    if not x_cron_secret or not hmac.compare_digest(x_cron_secret.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="Invalid secret.")
    today = today_nairobi()
    month = pack.last_complete_month(today)
    if db.query(BoardPack).filter_by(month=month).first():
        return {"generated": False, "month": month.strftime("%Y-%m"), "notified": 0}
    row = pack.generate(db, month, "system", today)
    exec_ids = {uid for (uid,) in db.query(StaffProfile.admin_user_id).filter(StaffProfile.is_executive.is_(True), StaffProfile.admin_user_id.isnot(None))}
    users = [u for u in db.query(AdminUser).filter(AdminUser.is_active.is_(True)).all() if u.role in ("admin", "hr") or u.id in exec_ids]
    msg = f"The {month:%B %Y} staff performance board pack is ready. Sign in to download it."
    for u in users:
        notify(db, recipient_admin_id=u.id, message=msg, link_path="/admin/performance/pack")
        channels.deliver(db, u, kind="pack", day=today, subject=f"Board pack ready: {month:%B %Y}", email_body=msg, sms_text=msg)
    return {"generated": True, "month": month.strftime("%Y-%m"), "notified": len(users), "id": row.id}
