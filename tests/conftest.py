import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.data.seed_loan_tiers import SEED_LOAN_TIERS
from app.database import Base, get_db
from app.main import app
from app.models.admin_user import AdminUser
from app.models.loan_tier import LoanTier
from app.services.auth import hash_password

TEST_DATABASE_URL = "sqlite:///:memory:"


@pytest.fixture()
def client():
    engine = create_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    # The app's real startup bootstraps (app/main.py) only seed the
    # production database at import time — they never run against this
    # isolated per-test DB, so seed the same defaults here instead.
    settings = get_settings()
    seed_db = TestingSessionLocal()
    seed_db.add(AdminUser(username=settings.admin_username, password_hash=hash_password(settings.admin_password)))
    for tier_data in SEED_LOAN_TIERS:
        seed_db.add(LoanTier(**tier_data))
    seed_db.commit()
    seed_db.close()

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()

    # Careers tests write real CV files under uploads/careers (the router
    # binds settings.upload_dir at import time, so it isn't easily swapped
    # for a temp dir via dependency override) — sweep them up so repeated
    # test runs don't leave test artifacts in the shipped uploads/ folder.
    careers_uploads = get_settings().upload_dir / "careers"
    if careers_uploads.exists():
        shutil.rmtree(careers_uploads)


@pytest.fixture()
def admin_token(client):
    """Logs in with the seeded default dev admin credentials and returns a Bearer token."""
    settings = get_settings()
    response = client.post(
        "/api/admin/login",
        json={"username": settings.admin_username, "password": settings.admin_password},
    )
    assert response.status_code == 200
    return response.json()["access_token"]

