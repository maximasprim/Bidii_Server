from app.config import get_settings


def test_admin_login_success(client):
    settings = get_settings()
    response = client.post(
        "/api/admin/login",
        json={"username": settings.admin_username, "password": settings.admin_password},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert "access_token" in body
    assert body["token_type"] == "bearer"


def test_admin_login_rejects_wrong_password(client):
    settings = get_settings()
    response = client.post(
        "/api/admin/login",
        json={"username": settings.admin_username, "password": "definitely-wrong"},
    )
    assert response.status_code == 401


def test_admin_login_rejects_wrong_username(client):
    settings = get_settings()
    response = client.post(
        "/api/admin/login",
        json={"username": "not-the-admin", "password": settings.admin_password},
    )
    assert response.status_code == 401


def test_admin_routes_reject_no_token(client):
    response = client.get("/api/admin/stats")
    assert response.status_code == 401


def test_admin_routes_reject_bad_token(client):
    response = client.get("/api/admin/stats", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401


def test_admin_stats_with_valid_token(client, admin_token):
    response = client.get("/api/admin/stats", headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["total_contacts"] == 0
    assert body["total_loan_applications"] == 0
    assert body["total_career_applications"] == 0
    assert set(body["loan_applications_by_status"].keys()) == {"pending", "contacted", "approved", "declined"}


def test_admin_stats_reflects_real_submissions(client, admin_token):
    client.post(
        "/api/contact",
        json={
            "name": "Grace Wanjiru",
            "email": "grace@example.com",
            "phone": "+254700000000",
            "subject": "loan-inquiry",
            "message": "Asking about business loan requirements please.",
        },
    )
    client.post(
        "/api/loan-applications",
        json={
            "product_slug": "sme-loans",
            "tier_id": "hustle-yangu",
            "amount": 10000,
            "term_value": 4,
            "term_unit": "weeks",
            "estimated_installment": 2875.0,
            "full_name": "Grace Wanjiru",
            "id_number": "12345678",
            "phone": "+254700000000",
            "email": "grace@example.com",
            "monthly_income": "45000",
        },
    )

    headers = {"Authorization": f"Bearer {admin_token}"}
    response = client.get("/api/admin/stats", headers=headers)
    body = response.json()
    assert body["total_contacts"] == 1
    assert body["total_loan_applications"] == 1
    assert body["loan_applications_by_status"]["pending"] == 1
    assert body["loan_applications_by_product"][0]["product_slug"] == "sme-loans"
    assert body["loan_applications_by_product"][0]["total_amount_requested"] == 10000


def test_admin_list_contacts(client, admin_token):
    client.post(
        "/api/contact",
        json={
            "name": "Grace Wanjiru",
            "email": "grace@example.com",
            "phone": "+254700000000",
            "subject": "loan-inquiry",
            "message": "Asking about business loan requirements please.",
        },
    )
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = client.get("/api/admin/contacts", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["total"] == 1
    assert body["items"][0]["name"] == "Grace Wanjiru"


def test_admin_update_loan_application_status(client, admin_token):
    submit = client.post(
        "/api/loan-applications",
        json={
            "product_slug": "sme-loans",
            "tier_id": "hustle-yangu",
            "amount": 10000,
            "term_value": 4,
            "term_unit": "weeks",
            "estimated_installment": 2875.0,
            "full_name": "Grace Wanjiru",
            "id_number": "12345678",
            "phone": "+254700000000",
            "email": "grace@example.com",
            "monthly_income": "45000",
        },
    )
    application_id = submit.json()["data"]["id"]

    headers = {"Authorization": f"Bearer {admin_token}"}
    response = client.patch(
        f"/api/admin/loan-applications/{application_id}",
        json={"status": "approved"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"


def test_admin_update_loan_application_status_rejects_invalid_status(client, admin_token):
    submit = client.post(
        "/api/loan-applications",
        json={
            "product_slug": "sme-loans",
            "tier_id": "hustle-yangu",
            "amount": 10000,
            "term_value": 4,
            "term_unit": "weeks",
            "estimated_installment": 2875.0,
            "full_name": "Grace Wanjiru",
            "id_number": "12345678",
            "phone": "+254700000000",
            "email": "grace@example.com",
            "monthly_income": "45000",
        },
    )
    application_id = submit.json()["data"]["id"]

    headers = {"Authorization": f"Bearer {admin_token}"}
    response = client.patch(
        f"/api/admin/loan-applications/{application_id}",
        json={"status": "not-a-real-status"},
        headers=headers,
    )
    assert response.status_code == 422


def test_admin_update_nonexistent_loan_application_returns_404(client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = client.patch(
        "/api/admin/loan-applications/not-a-real-id",
        json={"status": "approved"},
        headers=headers,
    )
    assert response.status_code == 404
