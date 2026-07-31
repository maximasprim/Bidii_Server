import io


def _pdf_bytes() -> bytes:
    # Minimal valid-enough PDF header; content.py only checks content-type
    # and non-empty bytes, not that it's a fully valid PDF structure.
    return b"%PDF-1.4\n%mock pdf content for tests\n"


VALID_FIELDS = {
    "full_name": "Grace Wanjiru",
    "email": "grace@example.com",
    "phone": "+254700000000",
    "role": "Loan Officer",
    "cover_note": "I have three years of experience in microfinance lending.",
}


def test_submit_career_application_success(client, tmp_path, monkeypatch):
    files = {"cv": ("cv.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")}
    response = client.post("/api/careers/applications", data=VALID_FIELDS, files=files)
    assert response.status_code == 201

    body = response.json()
    assert body["success"] is True
    assert body["data"]["full_name"] == "Grace Wanjiru"
    assert body["data"]["cv_original_filename"] == "cv.pdf"
    assert body["data"]["status"] == "received"


def test_submit_career_application_rejects_non_pdf(client):
    files = {"cv": ("cv.txt", io.BytesIO(b"not a pdf"), "text/plain")}
    response = client.post("/api/careers/applications", data=VALID_FIELDS, files=files)
    assert response.status_code == 422


def test_submit_career_application_rejects_empty_file(client):
    files = {"cv": ("cv.pdf", io.BytesIO(b""), "application/pdf")}
    response = client.post("/api/careers/applications", data=VALID_FIELDS, files=files)
    assert response.status_code == 422


def test_submit_career_application_rejects_short_cover_note(client):
    fields = {**VALID_FIELDS, "cover_note": "too short"}
    files = {"cv": ("cv.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")}
    response = client.post("/api/careers/applications", data=fields, files=files)
    assert response.status_code == 422


def test_submit_career_application_rejects_invalid_email(client):
    fields = {**VALID_FIELDS, "email": "not-an-email"}
    files = {"cv": ("cv.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")}
    response = client.post("/api/careers/applications", data=fields, files=files)
    assert response.status_code == 422


def test_submit_career_application_accepts_general_application_role(client):
    fields = {**VALID_FIELDS, "role": "General application"}
    files = {"cv": ("cv.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")}
    response = client.post("/api/careers/applications", data=fields, files=files)
    assert response.status_code == 201


def test_submit_career_application_accepts_unlisted_role_without_rejecting(client):
    # Role lists go stale faster than loan tiers — this should log, not reject.
    fields = {**VALID_FIELDS, "role": "Some Brand New Role Not In The List"}
    files = {"cv": ("cv.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")}
    response = client.post("/api/careers/applications", data=fields, files=files)
    assert response.status_code == 201
