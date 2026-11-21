"""Phase 2c part 2 - filled-form upload, extraction, review, confirmation, evidence and the Word template."""

import io
import json
from datetime import date

import pytest
from docx import Document
from PIL import Image, ImageDraw, ImageFont

from app.models.performance import Appraisal, KpiEntry, PerformanceAuditLog
from app.routers import admin_performance_files as files_router
from app.services import performance_ai as ai
from app.services import performance_ocr as ocr
from app.services import performance_storage as storage
from performance_tests.org import build_world

BASE = "/api/admin/performance"
PDF = b"%PDF-1.4\n" + b"x" * 200


class FakeBucket:
    def __init__(self):
        self.files = {}

    def upload(self, path, data, opts=None):
        assert path not in self.files
        self.files[path] = bytes(data)

    def download(self, path):
        return self.files[path]

    def remove(self, paths):
        for p in paths:
            self.files.pop(p, None)


def docx_bytes(lines):
    d = Document()
    for ln in lines:
        d.add_paragraph(ln)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


@pytest.fixture()
def bucket(monkeypatch):
    b = FakeBucket()
    monkeypatch.setattr(storage, "_client", lambda: b)
    return b


@pytest.fixture()
def world(client, db):
    return build_world(client, db)


@pytest.fixture()
def appraisal(client, world):
    r = client.post(f"{BASE}/staff/{world.p.lo1.id}/appraisals", json={"period": "2026-Q1"}, headers=world.h["hr1"])
    assert r.status_code == 201, r.text
    return r.json()["data"]


def up(client, world, who, aid, data, name="form.docx"):
    return client.post(f"{BASE}/appraisals/{aid}/upload", params={"filename": name}, content=data, headers=world.h[who])


@pytest.fixture()
def fake_ai(monkeypatch):
    calls = []

    def make(reply):
        def f(system, user):
            calls.append((system, user))
            return (reply(user) if callable(reply) else reply), "fake", "fake-model"

        monkeypatch.setattr(ai, "complete_json", f)
        return calls

    return make


def good_reply(appraisal, rating=4):
    return json.dumps({
        "items": [{"kpi_key": i["kpi_key"], "self_text": f"Did {i['label']}", "self_rating": rating, "manager_rating": 3, "confidence": 0.9 if n else 0.4} for n, i in enumerate(appraisal["items"])],
        "competencies": [{"key": c["key"], "self_rating": 4, "manager_rating": 3, "confidence": 0.8} for c in appraisal["competencies"]],
        "objectives": [], "development_needs": [{"slot": 1, "text": "Excel training", "confidence": 0.9}], "comments": {"manager": "Good quarter."},
    })


FORM = docx_bytes(["Appraisal for Officer One. Reports to Branch Manager One. Phone 0712345678 email officer.one@example.com", "Sales rated 4"])


def test_upload_stores_under_generated_path_and_audits(client, world, appraisal, bucket, db):
    r = up(client, world, "lo1", appraisal["id"], FORM, "../../etc/passwd.docx")
    assert r.status_code == 200 and r.json()["can_extract"]
    ap = db.get(Appraisal, appraisal["id"])
    assert storage.is_ours(ap.uploaded_file_path) and ".." not in ap.uploaded_file_path and ap.uploaded_file_path in bucket.files
    assert "etc" not in ap.uploaded_file_path
    assert db.query(PerformanceAuditLog).filter_by(action="appraisal_form_uploaded").count() == 1


@pytest.mark.parametrize("data,name,code", [
    (b"MZ\x90\x00 not a doc", "x.docx", 415),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 50, "x.doc", 415),
    (PDF, "x.docx", 415),
    (b"", "x.pdf", 400),
])
def test_upload_rejects_wrong_type_legacy_doc_and_empty(client, world, appraisal, bucket, data, name, code):
    assert up(client, world, "lo1", appraisal["id"], data, name).status_code == code
    assert not bucket.files


def test_upload_oversize_is_refused_while_streaming(client, world, appraisal, bucket):
    r = up(client, world, "lo1", appraisal["id"], b"%PDF-" + b"0" * (6 * 1024 * 1024), "big.pdf")
    assert r.status_code == 413 and not bucket.files


def test_extraction_is_pseudonymised_and_only_a_draft(client, world, appraisal, bucket, fake_ai, db):
    calls = fake_ai(good_reply(appraisal))
    up(client, world, "lo1", appraisal["id"], FORM)
    r = client.post(f"{BASE}/appraisals/{appraisal['id']}/extract", headers=world.h["lo1"])
    assert r.status_code == 200, r.text
    sent = calls[0][1]
    for secret in ("Officer One", "Branch Manager One", "0712345678", "officer.one@example.com"):
        assert secret not in sent
    assert "Staff A" in sent
    draft = r.json()["draft"]
    assert draft["ai"]["label"].startswith("AI-generated") and draft["ai"]["prompt_version"] and draft["low_confidence_count"] >= 1
    d = client.get(f"{BASE}/appraisals/{appraisal['id']}", headers=world.h["lo1"]).json()
    assert all(i["self_rating"] is None for i in d["items"])  # nothing counts until confirmed
    assert d["extraction_status"] == "extracted"


def test_bad_ai_output_is_rejected_and_file_stays(client, world, appraisal, bucket, fake_ai):
    fake_ai("not json")
    up(client, world, "lo1", appraisal["id"], FORM)
    r = client.post(f"{BASE}/appraisals/{appraisal['id']}/extract", headers=world.h["lo1"])
    assert r.status_code == 502 and "manually" in r.json()["detail"]
    assert client.get(f"{BASE}/appraisals/{appraisal['id']}/file", headers=world.h["lo1"]).status_code == 200


def test_unknown_keys_and_out_of_range_ratings_are_dropped(client, world, appraisal, bucket, fake_ai):
    k = appraisal["items"][0]["kpi_key"]
    fake_ai(json.dumps({"items": [{"kpi_key": k, "self_rating": 9, "confidence": 1}, {"kpi_key": "invented", "self_rating": 3, "confidence": 1}]}))
    up(client, world, "lo1", appraisal["id"], FORM)
    d = client.post(f"{BASE}/appraisals/{appraisal['id']}/extract", headers=world.h["lo1"]).json()["draft"]
    assert [i["kpi_key"] for i in d["items"]] == [k] and d["items"][0]["self_rating"] is None and d["warnings"]


def test_ai_not_configured_is_a_clear_503(client, world, appraisal, bucket):
    up(client, world, "lo1", appraisal["id"], FORM)
    assert client.post(f"{BASE}/appraisals/{appraisal['id']}/extract", headers=world.h["lo1"]).status_code == 503


def prepared(client, world, appraisal, fake_ai, rating=4):
    fake_ai(good_reply(appraisal, rating))
    up(client, world, "lo1", appraisal["id"], FORM)
    client.post(f"{BASE}/appraisals/{appraisal['id']}/extract", headers=world.h["lo1"])


def test_confirm_applies_reviewed_values_and_staff_cannot_set_manager_column(client, world, appraisal, bucket, fake_ai, db):
    prepared(client, world, appraisal, fake_ai)
    items = [{"kpi_key": i["kpi_key"], "self_rating": 5, "manager_rating": 1, "self_text": "Edited"} for i in appraisal["items"]]
    assert client.post(f"{BASE}/appraisals/{appraisal['id']}/extraction/confirm", json={"confirm": False, "items": items}, headers=world.h["lo1"]).status_code == 422
    r = client.post(f"{BASE}/appraisals/{appraisal['id']}/extraction/confirm", json={"confirm": True, "items": items}, headers=world.h["lo1"])
    assert r.status_code == 200
    d = client.get(f"{BASE}/appraisals/{appraisal['id']}", headers=world.h["lo1"]).json()
    assert all(i["self_rating"] == 5 and i["self_text"] == "Edited" and i["manager_rating"] is None for i in d["items"])
    assert d["extraction_status"] == "confirmed"
    assert client.post(f"{BASE}/appraisals/{appraisal['id']}/extraction/confirm", json={"confirm": True}, headers=world.h["lo1"]).status_code == 409


def test_hr_confirm_can_record_the_manager_column(client, world, appraisal, bucket, fake_ai):
    prepared(client, world, appraisal, fake_ai)
    items = [{"kpi_key": i["kpi_key"], "manager_rating": 3} for i in appraisal["items"]]
    assert client.post(f"{BASE}/appraisals/{appraisal['id']}/extraction/confirm", json={"confirm": True, "items": items}, headers=world.h["hr1"]).status_code == 200
    d = client.get(f"{BASE}/appraisals/{appraisal['id']}", headers=world.h["hr1"]).json()
    assert all(i["manager_rating"] == 3 for i in d["items"])


def test_confirm_validates_payload(client, world, appraisal, bucket, fake_ai):
    prepared(client, world, appraisal, fake_ai)
    url = f"{BASE}/appraisals/{appraisal['id']}/extraction/confirm"
    k = appraisal["items"][0]["kpi_key"]
    assert client.post(url, json={"confirm": True, "items": [{"kpi_key": k, "self_rating": 7}]}, headers=world.h["lo1"]).status_code == 422
    assert client.post(url, json={"confirm": True, "items": [{"kpi_key": k, "is_admin": True}]}, headers=world.h["lo1"]).status_code == 422


def test_review_screen_data_and_discard(client, world, appraisal, bucket, fake_ai):
    prepared(client, world, appraisal, fake_ai)
    r = client.get(f"{BASE}/appraisals/{appraisal['id']}/extraction", headers=world.h["lo1"]).json()
    assert r["draft"]["items"] and r["rows"]["items"] and "Sales rated 4" in r["source_text"] and r["can_confirm"] and not r["can_confirm_manager_column"]
    assert client.delete(f"{BASE}/appraisals/{appraisal['id']}/extraction", headers=world.h["lo1"]).status_code == 200
    d = client.get(f"{BASE}/appraisals/{appraisal['id']}", headers=world.h["lo1"]).json()
    assert d["extraction_status"] == "none"
    assert client.get(f"{BASE}/appraisals/{appraisal['id']}/file", headers=world.h["lo1"]).status_code == 200


def test_idor_and_rights(client, world, appraisal, bucket, fake_ai):
    aid = appraisal["id"]
    for who in ("lo2", "lo3", "bm2"):
        assert up(client, world, who, aid, FORM).status_code == 404
        assert client.get(f"{BASE}/appraisals/{aid}/file", headers=world.h[who]).status_code == 404
    up(client, world, "lo1", aid, FORM)
    assert up(client, world, "ceo", aid, FORM).status_code in (403, 404)  # read-only executive
    assert client.get(f"{BASE}/appraisals/{aid}/extraction", headers=world.h["lo2"]).status_code == 404
    assert client.get(f"{BASE}/appraisals/nope/file", headers=world.h["lo1"]).status_code == 404


def test_download_is_an_attachment_with_nosniff_and_audited(client, world, appraisal, bucket, db):
    up(client, world, "lo1", appraisal["id"], FORM)
    r = client.get(f"{BASE}/appraisals/{appraisal['id']}/file", headers=world.h["bm1"])
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and r.headers["x-content-type-options"] == "nosniff"
    assert db.query(PerformanceAuditLog).filter_by(action="appraisal_file_downloaded").count() == 1
    assert all("Officer One" not in json.dumps(a.details or {}) for a in db.query(PerformanceAuditLog))


def test_finalised_appraisal_rejects_uploads(client, world, appraisal, bucket, db):
    ap = db.get(Appraisal, appraisal["id"])
    ap.status = "finalised"
    db.commit()
    assert up(client, world, "hr1", appraisal["id"], FORM).status_code == 409


def test_storage_refuses_foreign_paths():
    for bad in ("performance/appraisals/../x/" + "a" * 32 + ".pdf", "other/appraisals/x/" + "a" * 32 + ".pdf", "performance/appraisals/x/y.pdf", None, ""):
        assert not storage.is_ours(bad)
    with pytest.raises(storage.StorageError):
        storage.new_path("appraisals", "../x", "pdf")
    with pytest.raises(storage.StorageError):
        storage.new_path("appraisals", "abc", "exe")


# ---- evidence -----------------------------------------------------------------------------------------------------------


@pytest.fixture()
def entry(db, world):
    e = KpiEntry(profile_id=world.p.lo1.id, kpi_key="sales", month=date(2026, 1, 1), value=100, status="draft", source="manual", entered_by_username="lo1")
    db.add(e)
    db.commit()
    return e


def ev(client, world, who, eid, data, name="s.pdf"):
    return client.post(f"{BASE}/entries/{eid}/evidence", params={"filename": name}, content=data, headers=world.h[who])


def test_evidence_upload_download_and_rules(client, world, entry, bucket, db):
    assert ev(client, world, "lo1", entry.id, PDF).status_code == 200
    r = client.get(f"{BASE}/entries/{entry.id}/evidence", headers=world.h["bm1"])
    assert r.status_code == 200 and r.content == PDF and "attachment" in r.headers["content-disposition"]
    assert ev(client, world, "lo1", entry.id, b"<html><script>", "x.pdf").status_code == 415
    assert ev(client, world, "lo1", entry.id, PDF, "x.png").status_code == 415
    assert ev(client, world, "lo1", entry.id, b"%PDF-" + b"0" * (6 * 1024 * 1024)).status_code == 413
    for who in ("lo2", "lo3", "bm2"):
        assert ev(client, world, who, entry.id, PDF).status_code == 404
        assert client.get(f"{BASE}/entries/{entry.id}/evidence", headers=world.h[who]).status_code == 404
    assert len(bucket.files) == 1  # replaced, not accumulated
    entry.status = "verified"
    db.commit()
    assert ev(client, world, "lo1", entry.id, PDF).status_code == 409


# ---- Word template ---------------------------------------------------------------------------------------------------


def test_blank_docx_template_has_the_paper_columns(client, world):
    r = client.get(f"{BASE}/roles/{world.role.id}/appraisal-template.docx", headers=world.h["hr1"])
    assert r.status_code == 200 and r.content[:2] == b"PK"
    doc = Document(io.BytesIO(r.content))
    heads = [c.text for c in doc.tables[1].rows[0].cells]
    assert heads[0].startswith("A. Job objectives") and "G. Manager assessment" in heads[-1] and len(doc.tables[1].rows) > 2
    assert client.get(f"{BASE}/roles/missing/appraisal-template.docx", headers=world.h["hr1"]).status_code == 404


# ---- photos and scans (read on this server with Tesseract) ---------------------------------------------------------------

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
needs_ocr = pytest.mark.skipif(not ocr.available(), reason="tesseract is not installed")


@pytest.fixture(autouse=True)
def _clear_text_cache():
    files_router._TEXT_CACHE.clear()


def form_image(lines, fmt="PNG", size=(1600, 900)):
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT, 44)
    for n, ln in enumerate(lines):
        d.text((60, 60 + n * 90), ln, fill="black", font=f)
    b = io.BytesIO()
    img.save(b, fmt)
    return b.getvalue()


PRINTED = ["Appraisal for Officer One", "Sales objective self rating 4", "Training need: Excel course"]


@needs_ocr
def test_ocr_reads_a_printed_image_and_scanned_pdf():
    res = ocr.read_image(form_image(PRINTED))
    assert "Sales" in res.text and "Excel" in res.text and res.confidence > 60
    img = Image.open(io.BytesIO(form_image(PRINTED)))
    pdf = io.BytesIO()
    img.save(pdf, "PDF")
    assert "Sales" in ocr.read_scanned_pdf(pdf.getvalue()).text


@needs_ocr
@pytest.mark.parametrize("fmt,name", [("PNG", "scan.png"), ("JPEG", "photo.jpg")])
def test_photo_upload_extract_is_pseudonymised_and_flagged_as_scanned(client, world, appraisal, bucket, fake_ai, fmt, name):
    calls = fake_ai(good_reply(appraisal))
    r = up(client, world, "lo1", appraisal["id"], form_image(PRINTED, fmt), name)
    assert r.status_code == 200 and r.json()["can_extract"] and r.json()["extraction_status"] == "none"
    x = client.post(f"{BASE}/appraisals/{appraisal['id']}/extract", headers=world.h["lo1"])
    assert x.status_code == 200, x.text
    assert "Officer One" not in calls[0][1] and "Staff A" in calls[0][1]
    d = x.json()["draft"]
    assert d["ocr_confidence"] and any("read from a picture" in w for w in d["warnings"])
    rv = client.get(f"{BASE}/appraisals/{appraisal['id']}/extraction", headers=world.h["lo1"]).json()
    assert rv["ocr_confidence"] and "Sales" in rv["source_text"]
    d2 = client.get(f"{BASE}/appraisals/{appraisal['id']}", headers=world.h["lo1"]).json()
    assert all(i["self_rating"] is None for i in d2["items"])  # still only a draft


def test_poor_scan_lowers_every_confidence():
    draft = {"items": [{"confidence": 0.9}], "competencies": [{"confidence": 0.8}], "objectives": [], "development_needs": []}
    from app.services.performance_extraction import flag_low_confidence, scale_for_ocr

    scale_for_ocr(draft, 42.5)
    assert draft["items"][0]["confidence"] == 0.45 and draft["competencies"][0]["confidence"] == 0.4
    assert flag_low_confidence(draft) == 2
    good = {"items": [{"confidence": 0.9}], "competencies": [], "objectives": [], "development_needs": []}
    scale_for_ocr(good, 95)
    assert good["items"][0]["confidence"] == 0.9


@needs_ocr
def test_bad_pictures_get_friendly_errors(client, world, appraisal, bucket, fake_ai):
    fake_ai(good_reply(appraisal))
    url = f"{BASE}/appraisals/{appraisal['id']}/extract"
    up(client, world, "lo1", appraisal["id"], b"\x89PNG\r\n\x1a\n" + b"corrupt" * 50, "x.png")
    r = client.post(url, headers=world.h["lo1"])
    assert r.status_code == 422 and "couldn't be opened" in r.json()["detail"]
    files_router._TEXT_CACHE.clear()
    blank = io.BytesIO()
    Image.new("RGB", (800, 800), "white").save(blank, "PNG")
    up(client, world, "lo1", appraisal["id"], blank.getvalue(), "blank.png")
    r = client.post(url, headers=world.h["lo1"])
    assert r.status_code == 422 and "No readable text" in r.json()["detail"]
    files_router._TEXT_CACHE.clear()
    huge = io.BytesIO()
    Image.new("L", (6000, 6000), 255).save(huge, "PNG")
    up(client, world, "lo1", appraisal["id"], huge.getvalue(), "huge.png")
    r = client.post(url, headers=world.h["lo1"])
    assert r.status_code == 422 and "too large" in r.json()["detail"]


def test_without_tesseract_the_photo_is_attached_and_the_message_is_clear(client, world, appraisal, bucket, monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: False)
    r = up(client, world, "lo1", appraisal["id"], form_image(PRINTED), "scan.png")
    assert r.status_code == 200 and r.json()["can_extract"] is False and r.json()["extraction_status"] == "failed"
    x = client.post(f"{BASE}/appraisals/{appraisal['id']}/extract", headers=world.h["lo1"])
    assert x.status_code == 503 and "by hand" in x.json()["detail"]
    assert client.get(f"{BASE}/appraisals/{appraisal['id']}/file", headers=world.h["lo1"]).status_code == 200
