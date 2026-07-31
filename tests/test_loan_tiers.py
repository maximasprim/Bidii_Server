def _auth(token):
    return {"Authorization": f"Bearer {token}"}


NEW_TIER = {
    "product_slug": "sme-loans",
    "tier_key": "new-tier",
    "label": "New Tier",
    "min_amount": 5000,
    "max_amount": 20000,
    "term_unit": "weeks",
    "min_term": 4,
    "max_term": 8,
    "repayment_frequency": "weekly",
    "interest_rate": 0.1,
    "interest_basis": "flat_over_term",
    "registration_fee": 500,
    "processing_fee_rate": 0.03,
    "life_insurance_fee_rate": 0.01,
    "guarantors": 1,
}


def test_public_loan_tiers_list_is_prepopulated_from_seed(client):
    response = client.get("/api/loan-tiers")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 11  # matches SEED_LOAN_TIERS count
    assert any(t["tier_key"] == "hustle-yangu" for t in items)


def test_public_loan_tiers_filters_by_product(client):
    response = client.get("/api/loan-tiers?product_slug=mobile-loans")
    items = response.json()["items"]
    assert len(items) == 2
    assert all(t["product_slug"] == "mobile-loans" for t in items)


def test_create_loan_tier_requires_auth(client):
    response = client.post("/api/admin/loan-tiers", json=NEW_TIER)
    assert response.status_code == 401


def test_create_loan_tier_success(client, admin_token):
    response = client.post("/api/admin/loan-tiers", json=NEW_TIER, headers=_auth(admin_token))
    assert response.status_code == 201
    assert response.json()["data"]["tier_key"] == "new-tier"

    public = client.get("/api/loan-tiers?product_slug=sme-loans")
    assert any(t["tier_key"] == "new-tier" for t in public.json()["items"])


def test_create_loan_tier_rejects_duplicate_key_for_same_product(client, admin_token):
    client.post("/api/admin/loan-tiers", json=NEW_TIER, headers=_auth(admin_token))
    response = client.post("/api/admin/loan-tiers", json=NEW_TIER, headers=_auth(admin_token))
    assert response.status_code == 409


def test_create_loan_tier_rejects_unknown_product(client, admin_token):
    payload = {**NEW_TIER, "product_slug": "not-a-real-product"}
    response = client.post("/api/admin/loan-tiers", json=payload, headers=_auth(admin_token))
    assert response.status_code == 422


def test_create_loan_tier_rejects_min_greater_than_max_amount(client, admin_token):
    payload = {**NEW_TIER, "min_amount": 50000, "max_amount": 10000}
    response = client.post("/api/admin/loan-tiers", json=payload, headers=_auth(admin_token))
    assert response.status_code == 422


def test_update_loan_tier_rate_takes_effect_on_public_endpoint(client, admin_token):
    tiers = client.get("/api/admin/loan-tiers?product_slug=sme-loans", headers=_auth(admin_token)).json()["items"]
    tier_id = next(t["id"] for t in tiers if t["tier_key"] == "hustle-yangu")

    response = client.patch(
        f"/api/admin/loan-tiers/{tier_id}",
        json={"interest_rate": 0.20},
        headers=_auth(admin_token),
    )
    assert response.status_code == 200
    assert response.json()["data"]["interest_rate"] == 0.20

    public = client.get("/api/loan-tiers?product_slug=sme-loans")
    updated = next(t for t in public.json()["items"] if t["tier_key"] == "hustle-yangu")
    assert updated["interest_rate"] == 0.20


def test_update_loan_tier_rejects_invalid_bounds(client, admin_token):
    tiers = client.get("/api/admin/loan-tiers?product_slug=sme-loans", headers=_auth(admin_token)).json()["items"]
    tier_id = tiers[0]["id"]

    response = client.patch(
        f"/api/admin/loan-tiers/{tier_id}",
        json={"min_term": 100, "max_term": 5},
        headers=_auth(admin_token),
    )
    assert response.status_code == 422


def test_deactivating_tier_removes_it_from_public_endpoint(client, admin_token):
    tiers = client.get("/api/admin/loan-tiers?product_slug=sme-loans", headers=_auth(admin_token)).json()["items"]
    tier_id = next(t["id"] for t in tiers if t["tier_key"] == "hustle-yangu")

    client.patch(f"/api/admin/loan-tiers/{tier_id}", json={"is_active": False}, headers=_auth(admin_token))

    public = client.get("/api/loan-tiers?product_slug=sme-loans")
    assert not any(t["tier_key"] == "hustle-yangu" for t in public.json()["items"])

    admin_list = client.get("/api/admin/loan-tiers?product_slug=sme-loans", headers=_auth(admin_token))
    assert any(t["tier_key"] == "hustle-yangu" for t in admin_list.json()["items"])


def test_loan_application_rejected_after_tier_deactivated(client, admin_token):
    tiers = client.get("/api/admin/loan-tiers?product_slug=sme-loans", headers=_auth(admin_token)).json()["items"]
    tier_id = next(t["id"] for t in tiers if t["tier_key"] == "hustle-yangu")
    client.patch(f"/api/admin/loan-tiers/{tier_id}", json={"is_active": False}, headers=_auth(admin_token))

    response = client.post(
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
    assert response.status_code == 422


def test_delete_loan_tier(client, admin_token):
    create = client.post("/api/admin/loan-tiers", json=NEW_TIER, headers=_auth(admin_token))
    tier_id = create.json()["data"]["id"]

    response = client.delete(f"/api/admin/loan-tiers/{tier_id}", headers=_auth(admin_token))
    assert response.status_code == 204

    public = client.get("/api/loan-tiers?product_slug=sme-loans")
    assert not any(t["tier_key"] == "new-tier" for t in public.json()["items"])
