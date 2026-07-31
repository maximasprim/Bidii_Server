import io


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


VALID_JOB = {
    "title": "Loan Officer",
    "department": "Credit",
    "location": "Nairobi CBD",
    "type": "Full-time",
    "description": "Manage a portfolio of business and SME loan applications end to end.",
}


def _pdf_bytes():
    return b"%PDF-1.4\n%mock pdf content\n"


def test_create_job_requires_auth(client):
    response = client.post("/api/admin/jobs", json=VALID_JOB)
    assert response.status_code == 401


def test_create_and_list_open_job(client, admin_token):
    create = client.post("/api/admin/jobs", json=VALID_JOB, headers=_auth(admin_token))
    assert create.status_code == 201

    public = client.get("/api/jobs")
    assert public.status_code == 200
    titles = [j["title"] for j in public.json()["items"]]
    assert "Loan Officer" in titles


def test_closed_job_not_visible_publicly(client, admin_token):
    create = client.post("/api/admin/jobs", json={**VALID_JOB, "is_open": False}, headers=_auth(admin_token))
    job_id = create.json()["data"]["id"]

    public = client.get("/api/jobs")
    assert not any(j["id"] == job_id for j in public.json()["items"])

    admin_list = client.get("/api/admin/jobs", headers=_auth(admin_token))
    assert any(j["id"] == job_id for j in admin_list.json()["items"])


def test_admin_job_list_includes_application_count(client, admin_token):
    create = client.post("/api/admin/jobs", json=VALID_JOB, headers=_auth(admin_token))
    job_id = create.json()["data"]["id"]

    files = {"cv": ("cv.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")}
    fields = {
        "full_name": "Grace Wanjiru",
        "email": "grace@example.com",
        "phone": "+254700000000",
        "role": "Loan Officer",
        "cover_note": "I have relevant experience in microfinance lending.",
        "job_id": job_id,
    }
    client.post("/api/careers/applications", data=fields, files=files)

    admin_list = client.get("/api/admin/jobs", headers=_auth(admin_token))
    job = next(j for j in admin_list.json()["items"] if j["id"] == job_id)
    assert job["application_count"] == 1


def test_application_with_job_id_uses_current_job_title(client, admin_token):
    create = client.post("/api/admin/jobs", json=VALID_JOB, headers=_auth(admin_token))
    job_id = create.json()["data"]["id"]

    files = {"cv": ("cv.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")}
    fields = {
        "full_name": "Grace Wanjiru",
        "email": "grace@example.com",
        "phone": "+254700000000",
        "role": "Some Stale Title",  # deliberately wrong — job_id should win
        "cover_note": "I have relevant experience in microfinance lending.",
        "job_id": job_id,
    }
    response = client.post("/api/careers/applications", data=fields, files=files)
    assert response.status_code == 201
    assert response.json()["data"]["role"] == "Loan Officer"
    assert response.json()["data"]["job_id"] == job_id


def test_application_with_invalid_job_id_rejected(client):
    files = {"cv": ("cv.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")}
    fields = {
        "full_name": "Grace Wanjiru",
        "email": "grace@example.com",
        "phone": "+254700000000",
        "role": "Loan Officer",
        "cover_note": "I have relevant experience in microfinance lending.",
        "job_id": "not-a-real-job-id",
    }
    response = client.post("/api/careers/applications", data=fields, files=files)
    assert response.status_code == 422


def test_general_application_without_job_id_still_works(client):
    files = {"cv": ("cv.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")}
    fields = {
        "full_name": "Grace Wanjiru",
        "email": "grace@example.com",
        "phone": "+254700000000",
        "role": "General application",
        "cover_note": "I have relevant experience in microfinance lending.",
    }
    response = client.post("/api/careers/applications", data=fields, files=files)
    assert response.status_code == 201
    assert response.json()["data"]["job_id"] is None


def test_vetting_view_filters_applications_by_job(client, admin_token):
    job1 = client.post("/api/admin/jobs", json=VALID_JOB, headers=_auth(admin_token)).json()["data"]["id"]
    job2 = client.post(
        "/api/admin/jobs",
        json={**VALID_JOB, "title": "Credit Risk Analyst"},
        headers=_auth(admin_token),
    ).json()["data"]["id"]

    for job_id, name in [(job1, "Applicant One"), (job2, "Applicant Two")]:
        files = {"cv": ("cv.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")}
        fields = {
            "full_name": name,
            "email": f"{name.replace(' ', '.').lower()}@example.com",
            "phone": "+254700000000",
            "role": "placeholder",
            "cover_note": "I have relevant experience in microfinance lending.",
            "job_id": job_id,
        }
        client.post("/api/careers/applications", data=fields, files=files)

    response = client.get(f"/api/admin/career-applications?job_id={job1}", headers=_auth(admin_token))
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["full_name"] == "Applicant One"


def test_delete_job_blocked_when_applications_exist(client, admin_token):
    job_id = client.post("/api/admin/jobs", json=VALID_JOB, headers=_auth(admin_token)).json()["data"]["id"]
    files = {"cv": ("cv.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")}
    fields = {
        "full_name": "Grace Wanjiru",
        "email": "grace@example.com",
        "phone": "+254700000000",
        "role": "placeholder",
        "cover_note": "I have relevant experience in microfinance lending.",
        "job_id": job_id,
    }
    client.post("/api/careers/applications", data=fields, files=files)

    response = client.delete(f"/api/admin/jobs/{job_id}", headers=_auth(admin_token))
    assert response.status_code == 400


def test_delete_job_without_applications_succeeds(client, admin_token):
    job_id = client.post("/api/admin/jobs", json=VALID_JOB, headers=_auth(admin_token)).json()["data"]["id"]
    response = client.delete(f"/api/admin/jobs/{job_id}", headers=_auth(admin_token))
    assert response.status_code == 204
