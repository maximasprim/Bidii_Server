"""Phase 6a - email/SMS reminder channels: preferences, delivery, idempotence, failures, no sensitive content."""

from datetime import date

import pytest

import app.routers.admin_performance_ops as ops_router
from app.models.admin_user import AdminUser
from app.models.performance import PerformanceAction, ReminderDelivery
from app.services import email_sender
from app.services import performance_channels as ch
from performance_tests.org import build_world

N = "/api/admin/performance/notifications"
TODAY = date(2026, 11, 18)


@pytest.mark.parametrize("raw,expected", [
    ("0712 345 678", "+254712345678"), ("0112345678", "+254112345678"), ("+254712345678", "+254712345678"), ("254712345678", "+254712345678"),
    ("712345678", "+254712345678"), ("+14155550123", "+14155550123"), ("12345", None), ("", None), (None, None), ("0812345678", None), ("abc", None),
])
def test_normalise_phone(raw, expected):
    assert ch.normalise_phone(raw) == expected


@pytest.fixture()
def w(client, db, monkeypatch):
    monkeypatch.setattr(ops_router, "today_nairobi", lambda: TODAY)
    import app.routers.admin_performance_notifications as nr

    monkeypatch.setattr(nr, "today_nairobi", lambda: TODAY)
    return build_world(client, db)


@pytest.fixture()
def wire(monkeypatch):
    sent = {"email": [], "sms": []}
    monkeypatch.setattr(ch, "email_configured", lambda: True)
    monkeypatch.setattr(ch, "sms_configured", lambda: True)
    monkeypatch.setattr(email_sender, "send_email", lambda **kw: sent["email"].append(kw))
    monkeypatch.setattr(ch, "send_sms", lambda phone, text: sent["sms"].append((phone, text)))
    return sent


def test_prefs_validation(client, w):
    g = client.get(N + "/prefs/me", headers=w.h["bm1"]).json()
    assert g["email_on"] is True and g["sms_on"] is False
    assert client.put(N + "/prefs/me", json={"email_on": True, "sms_on": True}, headers=w.h["bm1"]).status_code == 422  # no number
    assert client.put(N + "/prefs/me", json={"email_on": True, "sms_on": True, "phone": "12"}, headers=w.h["bm1"]).status_code == 422
    assert client.put(N + "/prefs/me", json={"email_on": True, "sms_on": True, "phone": "0712 345 678", "x": 1}, headers=w.h["bm1"]).status_code == 422
    r = client.put(N + "/prefs/me", json={"email_on": False, "sms_on": True, "phone": "0712 345 678"}, headers=w.h["bm1"])
    assert r.status_code == 200 and r.json()["phone"] == "+254712345678"
    assert client.get(N + "/prefs/me", headers=w.h["lo1"]).json()["sms_on"] is False  # not shared between people


def test_digest_goes_out_on_chosen_channels_once_a_day(client, db, w, wire, monkeypatch):
    class S:
        performance_cron_token = "tok"

    monkeypatch.setattr(ops_router, "get_settings", lambda: S)
    bm1 = db.query(AdminUser).filter_by(username="bm1").one()
    bm1.email = "bm1@example.com"
    db.commit()
    client.put(N + "/prefs/me", json={"email_on": True, "sms_on": True, "phone": "0712345678"}, headers=w.h["bm1"])
    db.add(PerformanceAction(profile_id=w.p.lo1.id, action_type="pip", restricted=True, started_on=date(2026, 10, 1), review_on=date(2026, 11, 1), note="secret", raised_by_username="bm1"))
    db.commit()
    r = client.post("/api/internal/performance/daily-digest", headers={"X-Cron-Secret": "tok"}).json()
    assert r["channels"].get("email_sent", 0) >= 1 and r["channels"].get("sms_sent", 0) >= 1
    to = {m["to_email"]: m for m in wire["email"]}
    assert "bm1@example.com" in to and wire["sms"][0][0] == "+254712345678"
    text = (to["bm1@example.com"]["body_text"] + to["bm1@example.com"]["subject"] + wire["sms"][0][1]).lower()
    assert "pip" not in text and "warning" not in text and "officer" not in text and "secret" not in text
    again = client.post("/api/internal/performance/daily-digest", headers={"X-Cron-Secret": "tok"}).json()
    assert again["sent"] == 0 and len(wire["email"]) == 1 and len(wire["sms"]) == 1


def test_failures_are_recorded_not_raised(client, db, w, wire, monkeypatch):
    def boom(**kw):
        raise email_sender.EmailError("SMTP down")

    monkeypatch.setattr(email_sender, "send_email", boom)
    monkeypatch.setattr(ch, "send_sms", lambda p, t: (_ for _ in ()).throw(ch.ChannelError("gateway 500")))
    bm1 = db.query(AdminUser).filter_by(username="bm1").one()
    bm1.email = "bm1@example.com"
    db.commit()
    client.put(N + "/prefs/me", json={"email_on": True, "sms_on": True, "phone": "0712345678"}, headers=w.h["bm1"])
    r = client.post(N + "/test", headers=w.h["bm1"])
    assert r.status_code == 200 and r.json()["results"] == {"email": "failed", "sms": "failed"}
    rows = {(d.channel, d.status) for d in db.query(ReminderDelivery)}
    assert rows == {("email", "failed"), ("sms", "failed")}


def test_skipped_without_address_or_number(client, db, w, wire):
    client.put(N + "/prefs/me", json={"email_on": True, "sms_on": False}, headers=w.h["bm2"])
    assert client.post(N + "/test", headers=w.h["bm2"]).json()["results"] == {"email": "skipped"}  # no email on the login
    assert wire["email"] == []


def test_opt_out_sends_nothing(client, db, w, wire):
    bm1 = db.query(AdminUser).filter_by(username="bm1").one()
    bm1.email = "bm1@example.com"
    db.commit()
    client.put(N + "/prefs/me", json={"email_on": False, "sms_on": False}, headers=w.h["bm1"])
    assert client.post(N + "/test", headers=w.h["bm1"]).json()["results"] == {}


def test_sms_request_shape(monkeypatch):
    class S:
        performance_sms_username = "bidii"
        performance_sms_api_key = "key123"
        performance_sms_sender_id = "BIDII"

    seen = {}
    monkeypatch.setattr(ch, "get_settings", lambda: S)
    monkeypatch.setattr(ch, "_post_sms", lambda url, headers, data: seen.update(url=url, headers=headers, data=data) or '{"SMSMessageData":{"Recipients":[{"status":"Success"}]}}')
    ch.send_sms("+254712345678", "hello")
    assert seen["url"].startswith("https://") and seen["headers"]["apiKey"] == "key123" and b"username=bidii" in seen["data"] and b"to=%2B254712345678" in seen["data"]
    monkeypatch.setattr(ch, "_post_sms", lambda *a: '{"SMSMessageData":{"Recipients":[]}}')
    with pytest.raises(ch.ChannelError):
        ch.send_sms("+254712345678", "hello")


def test_status_is_hr_only_and_has_no_text(client, db, w, wire):
    bm1 = db.query(AdminUser).filter_by(username="bm1").one()
    bm1.email = "bm1@example.com"
    db.commit()
    client.post(N + "/test", headers=w.h["bm1"])
    assert client.get(N + "/status", headers=w.h["bm1"]).status_code == 403
    r = client.get(N + "/status", headers=w.h["hr1"]).json()
    assert {"channel": "email", "status": "sent", "count": 1} in r["counts"] and "bm1" not in str(r)


def test_test_message_is_rate_limited_and_needs_login(client, w, wire):
    for _ in range(5):
        client.post(N + "/test", headers=w.h["lo1"])
    assert client.post(N + "/test", headers=w.h["lo1"]).status_code == 429
    for m, p in (("get", "/prefs/me"), ("put", "/prefs/me"), ("post", "/test"), ("get", "/status")):
        assert getattr(client, m)(N + p).status_code in (401, 403)
