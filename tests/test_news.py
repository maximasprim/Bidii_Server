def _auth(token):
    return {"Authorization": f"Bearer {token}"}


VALID_ARTICLE = {
    "title": "How to Read a Loan Offer Before You Sign",
    "category": "Financial Literacy",
    "excerpt": "Four numbers to check before you accept any loan offer.",
    "body": ["Paragraph one.", "Paragraph two."],
}


def test_create_article_requires_auth(client):
    response = client.post("/api/admin/news", json=VALID_ARTICLE)
    assert response.status_code == 401


def test_create_and_fetch_published_article(client, admin_token):
    create = client.post("/api/admin/news", json=VALID_ARTICLE, headers=_auth(admin_token))
    assert create.status_code == 201
    slug = create.json()["data"]["slug"]
    assert slug == "how-to-read-a-loan-offer-before-you-sign"

    public = client.get(f"/api/news/{slug}")
    assert public.status_code == 200
    assert public.json()["title"] == VALID_ARTICLE["title"]


def test_unpublished_article_not_visible_publicly(client, admin_token):
    payload = {**VALID_ARTICLE, "is_published": False}
    create = client.post("/api/admin/news", json=payload, headers=_auth(admin_token))
    slug = create.json()["data"]["slug"]

    public = client.get(f"/api/news/{slug}")
    assert public.status_code == 404

    admin_list = client.get("/api/admin/news", headers=_auth(admin_token))
    assert any(a["slug"] == slug for a in admin_list.json()["items"])


def test_public_list_filters_by_category(client, admin_token):
    client.post("/api/admin/news", json=VALID_ARTICLE, headers=_auth(admin_token))
    client.post(
        "/api/admin/news",
        json={**VALID_ARTICLE, "title": "Bidii Crosses KES 4 Billion", "category": "Company News"},
        headers=_auth(admin_token),
    )

    response = client.get("/api/news?category=Company News")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["category"] == "Company News"


def test_duplicate_title_gets_unique_slug(client, admin_token):
    first = client.post("/api/admin/news", json=VALID_ARTICLE, headers=_auth(admin_token))
    second = client.post("/api/admin/news", json=VALID_ARTICLE, headers=_auth(admin_token))
    assert first.json()["data"]["slug"] != second.json()["data"]["slug"]
    assert second.json()["data"]["slug"] == "how-to-read-a-loan-offer-before-you-sign-2"


def test_update_article(client, admin_token):
    create = client.post("/api/admin/news", json=VALID_ARTICLE, headers=_auth(admin_token))
    article_id = create.json()["data"]["id"]

    response = client.patch(
        f"/api/admin/news/{article_id}",
        json={"title": "Updated Title", "is_published": False},
        headers=_auth(admin_token),
    )
    assert response.status_code == 200
    assert response.json()["data"]["title"] == "Updated Title"
    assert response.json()["data"]["is_published"] is False


def test_delete_article(client, admin_token):
    create = client.post("/api/admin/news", json=VALID_ARTICLE, headers=_auth(admin_token))
    article_id = create.json()["data"]["id"]

    response = client.delete(f"/api/admin/news/{article_id}", headers=_auth(admin_token))
    assert response.status_code == 204

    admin_list = client.get("/api/admin/news", headers=_auth(admin_token))
    assert not any(a["id"] == article_id for a in admin_list.json()["items"])


def test_create_article_rejects_invalid_category(client, admin_token):
    payload = {**VALID_ARTICLE, "category": "Not A Real Category"}
    response = client.post("/api/admin/news", json=payload, headers=_auth(admin_token))
    assert response.status_code == 422


def test_get_nonexistent_article_returns_404(client):
    response = client.get("/api/news/not-a-real-slug")
    assert response.status_code == 404
