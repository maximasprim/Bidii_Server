"""
Staff Performance - reminder channels (Phase 6a).  Prefix /api/admin/performance/notifications

  GET /prefs/me      my channels (email on by default if my login has an address; SMS off until I add a number)
  PUT /prefs/me      {email_on, sms_on, phone}
  POST /test         send myself a test message on my chosen channels (rate limited)
  GET /status        HR/admin: which channels are configured and how many sends succeeded or failed (no message text, no names)
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.performance import NotificationPref
from app.routers.admin_performance import admin_or_hr, my_access
from app.services import performance_channels as ch
from app.services.auth import get_current_admin
from app.services.performance_config import log_audit
from app.services.performance_ratelimit import rate_limited
from app.services.performance_time import today_nairobi

router = APIRouter(prefix="/api/admin/performance/notifications", tags=["admin-performance-notifications"])
test_limit = Depends(rate_limited("notify-test", limit=5, window_seconds=3600))


class PrefBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email_on: bool
    sms_on: bool
    phone: str | None = Field(default=None, max_length=30)


def _view(pref: NotificationPref, user: AdminUser) -> dict[str, Any]:
    return {"email_on": pref.email_on, "sms_on": pref.sms_on, "phone": pref.phone, "email_address_on_file": bool(user.email),
            "email_available": ch.email_configured(), "sms_available": ch.sms_configured()}


@router.get("/prefs/me", dependencies=[my_access])
def get_prefs(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _view(ch.prefs_for(db, admin.id), admin)


@router.put("/prefs/me", dependencies=[my_access])
def put_prefs(body: PrefBody, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    phone = ch.normalise_phone(body.phone) if (body.phone or "").strip() else None
    if (body.phone or "").strip() and phone is None:
        raise HTTPException(status_code=422, detail="That phone number doesn't look right. Use a Kenyan number such as 0712 345 678.")
    if body.sms_on and phone is None:
        raise HTTPException(status_code=422, detail="Add a phone number to receive SMS.")
    row = db.query(NotificationPref).filter_by(admin_user_id=admin.id).first()
    if row is None:
        row = NotificationPref(admin_user_id=admin.id)
        db.add(row)
    row.email_on, row.sms_on, row.phone = body.email_on, body.sms_on, phone
    db.commit()
    log_audit(db, admin.username, "notification_prefs.updated", "admin_user", admin.id, {"email": body.email_on, "sms": body.sms_on})
    return _view(row, admin)


@router.post("/test", dependencies=[my_access, test_limit])
def send_test(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    result = ch.deliver(db, admin, kind="test", day=today_nairobi(), subject="Bidii performance: test message",
                        email_body="This is a test of your performance reminders. Nothing needs doing.", sms_text="Bidii Performance: this is a test reminder.")
    return {"results": result}


@router.get("/status", dependencies=[admin_or_hr])
def status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return ch.delivery_report(db, today_nairobi() - timedelta(days=14))
