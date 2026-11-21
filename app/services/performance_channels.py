"""
Reminder delivery beyond the in-app bell: email (existing "loans" SMTP identity) and SMS (Africa's Talking).
Both are off until configured. Messages are generic (counts and plain wording, no names, no PIP or warning detail).
A failed send never raises into the caller; it is recorded and the in-app notification still exists.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.admin_user import AdminUser
from app.models.performance import NotificationPref, ReminderDelivery
from app.services import email_sender

SMS_URL = "https://api.africastalking.com/version1/messaging"
SMS_MAX = 300


def normalise_phone(raw: str | None) -> str | None:
    """Kenyan numbers to +2547XXXXXXXX / +2541XXXXXXXX. Anything else that looks international is kept; junk gives None."""
    if not raw:
        return None
    s = re.sub(r"[\s\-().]", "", raw.strip())
    if re.fullmatch(r"\+?254[17]\d{8}", s):
        return "+" + s.lstrip("+")
    if re.fullmatch(r"0[17]\d{8}", s):
        return "+254" + s[1:]
    if re.fullmatch(r"[17]\d{8}", s):
        return "+254" + s
    if re.fullmatch(r"\+[1-9]\d{8,13}", s):
        return s
    return None


def email_configured() -> bool:
    return email_sender.is_email_configured("loans")


def sms_configured() -> bool:
    s = get_settings()
    return bool(s.performance_sms_username and s.performance_sms_api_key)


def _post_sms(url: str, headers: dict[str, str], data: bytes) -> str:
    """The one network call for SMS; tests replace it."""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed https URL
        return resp.read().decode("utf-8", "replace")


class ChannelError(Exception):
    pass


def send_sms(phone: str, text: str) -> None:
    s = get_settings()
    if not sms_configured():
        raise ChannelError("SMS isn't configured.")
    fields = {"username": s.performance_sms_username, "to": phone, "message": text[:SMS_MAX]}
    if s.performance_sms_sender_id:
        fields["from"] = s.performance_sms_sender_id
    try:
        body = _post_sms(SMS_URL, {"apiKey": s.performance_sms_api_key or "", "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                         urllib.parse.urlencode(fields).encode())
    except (urllib.error.URLError, OSError) as exc:
        raise ChannelError(f"Couldn't reach the SMS service: {exc}") from exc
    if '"status":"Success"' not in body.replace(" ", "") and "Sent" not in body:
        raise ChannelError("The SMS service didn't accept the message.")


def prefs_for(db: Session, user_id: str) -> NotificationPref:
    row = db.query(NotificationPref).filter_by(admin_user_id=user_id).first()
    return row or NotificationPref(admin_user_id=user_id, email_on=True, sms_on=False, phone=None)


def _record(db: Session, user_id: str, channel: str, kind: str, day: date, status: str, error: str | None = None) -> None:
    db.add(ReminderDelivery(admin_user_id=user_id, channel=channel, kind=kind, day=day, status=status, error=(error or None) and error[:200]))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()


def deliver(db: Session, user: AdminUser, *, kind: str, day: date, subject: str, email_body: str, sms_text: str) -> dict[str, str]:
    """Sends on the channels the person chose, once per channel/kind/day. Returns {channel: status}."""
    pref = prefs_for(db, user.id)
    out: dict[str, str] = {}
    done = {(c, k) for c, k in db.query(ReminderDelivery.channel, ReminderDelivery.kind).filter(ReminderDelivery.admin_user_id == user.id, ReminderDelivery.day == day, ReminderDelivery.status == "sent")}
    if pref.email_on:
        if ("email", kind) in done:
            out["email"] = "already_sent"
        elif not user.email or not email_configured():
            out["email"] = "skipped"
            _record(db, user.id, "email", kind, day, "skipped", "no address or email not configured")
        else:
            try:
                email_sender.send_email(to_email=user.email, subject=subject, body_text=email_body, kind="loans")
                out["email"] = "sent"
                _record(db, user.id, "email", kind, day, "sent")
            except email_sender.EmailError as exc:
                out["email"] = "failed"
                _record(db, user.id, "email", kind, day, "failed", str(exc))
    if pref.sms_on:
        phone = normalise_phone(pref.phone)
        if ("sms", kind) in done:
            out["sms"] = "already_sent"
        elif phone is None or not sms_configured():
            out["sms"] = "skipped"
            _record(db, user.id, "sms", kind, day, "skipped", "no valid number or SMS not configured")
        else:
            try:
                send_sms(phone, sms_text)
                out["sms"] = "sent"
                _record(db, user.id, "sms", kind, day, "sent")
            except ChannelError as exc:
                out["sms"] = "failed"
                _record(db, user.id, "sms", kind, day, "failed", str(exc))
    db.commit()
    return out


def delivery_report(db: Session, since: date) -> dict[str, Any]:
    rows: dict[tuple[str, str], int] = {}
    for ch, st in db.query(ReminderDelivery.channel, ReminderDelivery.status).filter(ReminderDelivery.day >= since):
        rows[(ch, st)] = rows.get((ch, st), 0) + 1
    return {"since": since.isoformat(), "email_configured": email_configured(), "sms_configured": sms_configured(),
            "counts": [{"channel": c, "status": s, "count": n} for (c, s), n in sorted(rows.items())]}
