VALID_PAYLOAD = {
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
}


def test_submit_loan_application_success(client):
    response = client.post("/api/loan-applications", json=VALID_PAYLOAD)
    assert response.status_code == 201

    body = response.json()
    assert body["success"] is True
    assert body["data"]["product_name"] == "SME Loans"
    assert body["data"]["tier_label"] == "Hustle Yangu"
    assert body["data"]["status"] == "pending"
    assert "id" in body["data"]


def test_submit_loan_application_rejects_unknown_product(client):
    payload = {**VALID_PAYLOAD, "product_slug": "not-a-real-product"}
    response = client.post("/api/loan-applications", json=payload)
    assert response.status_code == 422


def test_submit_loan_application_rejects_unknown_tier(client):
    payload = {**VALID_PAYLOAD, "tier_id": "not-a-real-tier"}
    response = client.post("/api/loan-applications", json=payload)
    assert response.status_code == 422


def test_submit_loan_application_rejects_amount_above_tier_max(client):
    # Hustle Yangu tops out at 15,000
    payload = {**VALID_PAYLOAD, "amount": 500_000}
    response = client.post("/api/loan-applications", json=payload)
    assert response.status_code == 422


def test_submit_loan_application_rejects_amount_below_tier_min(client):
    # Hustle Yangu starts at 2,000
    payload = {**VALID_PAYLOAD, "amount": 100}
    response = client.post("/api/loan-applications", json=payload)
    assert response.status_code == 422


def test_submit_loan_application_rejects_term_out_of_range(client):
    # Hustle Yangu is a fixed 4-week term
    payload = {**VALID_PAYLOAD, "term_value": 52}
    response = client.post("/api/loan-applications", json=payload)
    assert response.status_code == 422


def test_submit_loan_application_rejects_wrong_term_unit(client):
    # Hustle Yangu is weekly, not monthly
    payload = {**VALID_PAYLOAD, "term_unit": "months", "term_value": 1}
    response = client.post("/api/loan-applications", json=payload)
    assert response.status_code == 422


def test_submit_loan_application_rejects_short_name(client):
    payload = {**VALID_PAYLOAD, "full_name": "G"}
    response = client.post("/api/loan-applications", json=payload)
    assert response.status_code == 422


def test_submit_loan_application_rejects_invalid_email(client):
    payload = {**VALID_PAYLOAD, "email": "not-an-email"}
    response = client.post("/api/loan-applications", json=payload)
    assert response.status_code == 422


def test_submit_loan_application_accepts_check_off_affordability_tier(client):
    payload = {
        "product_slug": "check-off-loans",
        "tier_id": "standard",
        "amount": 409_091,
        "term_value": 60,
        "term_unit": "months",
        "estimated_installment": 15_000,
        "full_name": "James Kariuki",
        "id_number": "87654321",
        "phone": "+254711000000",
        "email": "james@example.com",
        "monthly_income": "35000",
    }
    response = client.post("/api/loan-applications", json=payload)
    assert response.status_code == 201
