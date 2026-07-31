def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_list_admin_users_shows_seeded_admin(client, admin_token):
    response = client.get("/api/admin/users", headers=_auth(admin_token))
    assert response.status_code == 200
    usernames = [u["username"] for u in response.json()["items"]]
    assert "admin" in usernames


def test_create_admin_user_success(client, admin_token):
    response = client.post(
        "/api/admin/users",
        json={"username": "james", "password": "supersecret123"},
        headers=_auth(admin_token),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["data"]["username"] == "james"
    assert body["data"]["is_active"] is True
    # Password hash should never be exposed in the response.
    assert "password" not in body["data"]
    assert "password_hash" not in body["data"]


def test_new_admin_user_can_log_in(client, admin_token):
    client.post(
        "/api/admin/users",
        json={"username": "james", "password": "supersecret123"},
        headers=_auth(admin_token),
    )
    response = client.post("/api/admin/login", json={"username": "james", "password": "supersecret123"})
    assert response.status_code == 200
    assert "access_token" in response.json()


def test_create_admin_user_rejects_duplicate_username(client, admin_token):
    client.post("/api/admin/users", json={"username": "james", "password": "supersecret123"}, headers=_auth(admin_token))
    response = client.post("/api/admin/users", json={"username": "james", "password": "anotherpassword"}, headers=_auth(admin_token))
    assert response.status_code == 409


def test_create_admin_user_rejects_short_password(client, admin_token):
    response = client.post(
        "/api/admin/users",
        json={"username": "james", "password": "short"},
        headers=_auth(admin_token),
    )
    assert response.status_code == 422


def test_create_admin_user_requires_auth(client):
    response = client.post("/api/admin/users", json={"username": "james", "password": "supersecret123"})
    assert response.status_code == 401


def test_deactivate_admin_user_success(client, admin_token):
    create = client.post(
        "/api/admin/users",
        json={"username": "james", "password": "supersecret123"},
        headers=_auth(admin_token),
    )
    user_id = create.json()["data"]["id"]

    response = client.delete(f"/api/admin/users/{user_id}", headers=_auth(admin_token))
    assert response.status_code == 200
    assert response.json()["is_active"] is False


def test_deactivated_admin_user_cannot_log_in(client, admin_token):
    create = client.post(
        "/api/admin/users",
        json={"username": "james", "password": "supersecret123"},
        headers=_auth(admin_token),
    )
    user_id = create.json()["data"]["id"]
    client.delete(f"/api/admin/users/{user_id}", headers=_auth(admin_token))

    response = client.post("/api/admin/login", json={"username": "james", "password": "supersecret123"})
    assert response.status_code == 401


def test_admin_cannot_deactivate_self(client, admin_token):
    users = client.get("/api/admin/users", headers=_auth(admin_token)).json()["items"]
    self_id = next(u["id"] for u in users if u["username"] == "admin")

    response = client.delete(f"/api/admin/users/{self_id}", headers=_auth(admin_token))
    assert response.status_code == 400


def test_deactivating_one_of_two_admins_leaves_the_other_active(client, admin_token):
    """
    With two active admins, one deactivating the other is a normal,
    allowed action — it only leaves a single active admin, it doesn't
    remove the last one. The "last remaining admin" guard exists for the
    edge this test does NOT hit (see test_admin_cannot_deactivate_self for
    the actually-reachable lockout-prevention path).
    """
    create = client.post("/api/admin/users", json={"username": "second", "password": "supersecret123"}, headers=_auth(admin_token))
    second_id = create.json()["data"]["id"]

    response = client.delete(f"/api/admin/users/{second_id}", headers=_auth(admin_token))
    assert response.status_code == 200
    assert response.json()["is_active"] is False

    users = client.get("/api/admin/users", headers=_auth(admin_token)).json()["items"]
    active = [u for u in users if u["is_active"]]
    assert len(active) == 1
    assert active[0]["username"] == "admin"


def test_renaming_self_does_not_invalidate_current_session(client, admin_token):
    """
    The JWT subject is the user's stable ID, not their username — this
    confirms that guarantee: after renaming yourself, your existing token
    (issued before the rename) must still work on the very next request,
    since a token invalidated by your own rename would be a lockout bug.
    """
    users = client.get("/api/admin/users", headers=_auth(admin_token)).json()["items"]
    self_id = next(u["id"] for u in users if u["username"] == "admin")

    rename_response = client.patch(
        f"/api/admin/users/{self_id}",
        json={"username": "renamed-admin"},
        headers=_auth(admin_token),
    )
    assert rename_response.status_code == 200
    assert rename_response.json()["data"]["username"] == "renamed-admin"

    # Same token, issued before the rename — must still be valid.
    follow_up = client.get("/api/admin/stats", headers=_auth(admin_token))
    assert follow_up.status_code == 200

    # And the account can now log in under its new name.
    new_login = client.post("/api/admin/login", json={"username": "renamed-admin", "password": "changeme123"})
    assert new_login.status_code == 200


def test_admin_can_reset_own_password_via_patch(client, admin_token):
    users = client.get("/api/admin/users", headers=_auth(admin_token)).json()["items"]
    self_id = next(u["id"] for u in users if u["username"] == "admin")

    response = client.patch(
        f"/api/admin/users/{self_id}",
        json={"password": "brandnewpassword123"},
        headers=_auth(admin_token),
    )
    assert response.status_code == 200

    old_password_login = client.post("/api/admin/login", json={"username": "admin", "password": "changeme123"})
    assert old_password_login.status_code == 401

    new_password_login = client.post("/api/admin/login", json={"username": "admin", "password": "brandnewpassword123"})
    assert new_password_login.status_code == 200


def test_update_admin_user_rejects_duplicate_username(client, admin_token):
    client.post("/api/admin/users", json={"username": "second", "password": "supersecret123"}, headers=_auth(admin_token))
    users = client.get("/api/admin/users", headers=_auth(admin_token)).json()["items"]
    second_id = next(u["id"] for u in users if u["username"] == "second")

    response = client.patch(f"/api/admin/users/{second_id}", json={"username": "admin"}, headers=_auth(admin_token))
    assert response.status_code == 409


def test_reactivate_deactivated_admin_via_patch(client, admin_token):
    create = client.post("/api/admin/users", json={"username": "second", "password": "supersecret123"}, headers=_auth(admin_token))
    second_id = create.json()["data"]["id"]
    client.delete(f"/api/admin/users/{second_id}", headers=_auth(admin_token))

    response = client.patch(f"/api/admin/users/{second_id}", json={"is_active": True}, headers=_auth(admin_token))
    assert response.status_code == 200
    assert response.json()["data"]["is_active"] is True

    login = client.post("/api/admin/login", json={"username": "second", "password": "supersecret123"})
    assert login.status_code == 200
