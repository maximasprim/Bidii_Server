def test_health_check(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_submit_contact_message_success(client):
    payload = {
        "name": "Grace Wanjiru",
        "email": "grace@example.com",
        "phone": "+254700000000",
        "subject": "loan-inquiry",
        "message": "I'd like to ask about the business loan requirements.",
    }
    response = client.post("/api/contact", json=payload)
    assert response.status_code == 201

    body = response.json()
    assert body["success"] is True
    assert body["data"]["name"] == payload["name"]
    assert body["data"]["email"] == payload["email"]
    assert body["data"]["subject"] == payload["subject"]
    assert "id" in body["data"]
    assert "created_at" in body["data"]


def test_submit_contact_message_rejects_short_name(client):
    payload = {
        "name": "G",
        "email": "grace@example.com",
        "phone": "+254700000000",
        "subject": "loan-inquiry",
        "message": "I'd like to ask about the business loan requirements.",
    }
    response = client.post("/api/contact", json=payload)
    assert response.status_code == 422
    assert response.json()["success"] is False


def test_submit_contact_message_rejects_invalid_email(client):
    payload = {
        "name": "Grace Wanjiru",
        "email": "not-an-email",
        "phone": "+254700000000",
        "subject": "loan-inquiry",
        "message": "I'd like to ask about the business loan requirements.",
    }
    response = client.post("/api/contact", json=payload)
    assert response.status_code == 422


def test_submit_contact_message_rejects_invalid_subject(client):
    payload = {
        "name": "Grace Wanjiru",
        "email": "grace@example.com",
        "phone": "+254700000000",
        "subject": "not-a-real-subject",
        "message": "I'd like to ask about the business loan requirements.",
    }
    response = client.post("/api/contact", json=payload)
    assert response.status_code == 422


def test_submit_contact_message_rejects_short_message(client):
    payload = {
        "name": "Grace Wanjiru",
        "email": "grace@example.com",
        "phone": "+254700000000",
        "subject": "loan-inquiry",
        "message": "too short",
    }
    response = client.post("/api/contact", json=payload)
    assert response.status_code == 422
