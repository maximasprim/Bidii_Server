"""
Isolated fixtures for the Staff Performance module tests.

SAFETY: these tests must never touch a real database, Supabase, an AI provider
or an SMTP server. Importing app.main runs migrations and seeds against
whatever DATABASE_URL is set, so this file FORCES a safe environment *before*
the app is imported, then refuses to continue if anything real slipped through
(for example a developer's .env). Run via performance_tests/run.sh.

PERF_TEST_FLAG=off runs the suite with PERFORMANCE_MODULE_ENABLED=false (the
"nothing changed" parity checks); anything else runs with it enabled.
"""

import os
import re

import pytest

FLAG_ON = os.environ.get("PERF_TEST_FLAG", "on").lower() != "off"

# Optional: run the same tests against a THROWAWAY Postgres. Accepted only when the URL points at
# localhost and a database whose name starts with "perf_test" - never at anything real.
PG_URL = os.environ.get("PERF_TEST_DB_URL")
if PG_URL and not re.fullmatch(r"postgresql(\+psycopg2?)?://[^@/]*@(localhost|127\.0\.0\.1)(:\d+)?/perf_test\w*", PG_URL):
    pytest.exit("PERF_TEST_DB_URL must be a local throwaway database named perf_test*.", returncode=2)

# Hard-set (not setdefault): a real shell variable or .env value must not win.
_FORCED = {
    "DATABASE_URL": PG_URL or "sqlite:///:memory:",
    "SUPABASE_URL": "https://dummy.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "dummy-key",
    "JWT_SECRET": "performance-tests-only-secret-0123456789",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "changeme123",
    "ENVIRONMENT": "test",
    "PERFORMANCE_MODULE_ENABLED": "true" if FLAG_ON else "false",
    # Blank values override anything a .env file would otherwise supply.
    "OPENAI_API_KEY": "",
    "GEMINI_API_KEY": "",
    "GOOGLE_SERVICE_ACCOUNT_JSON": "",
    "SMTP_HOST": "",
    "SMTP_LOANS_HOST": "",
    "PERFORMANCE_CRON_TOKEN": "",
}
os.environ.update(_FORCED)

from app.config import get_settings  # noqa: E402

_s = get_settings()
if not (
    _s.database_url == (PG_URL or "sqlite:///:memory:")
    and "dummy" in _s.supabase_url
    and not _s.openai_api_key
    and not _s.gemini_api_key
    and not _s.google_service_account_json
    and not _s.smtp_host
):
    pytest.exit("Refusing to run: the environment is not the isolated test configuration.", returncode=2)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.admin_user import AdminUser  # noqa: E402
from app.services.auth import create_access_token, hash_password  # noqa: E402

PASSWORD = "supersecret123"
# bcrypt is deliberately slow; hash once for the whole run instead of once per user per test.
_PASSWORD_HASH = hash_password(PASSWORD)


@pytest.fixture()
def session_factory():
    if PG_URL:
        engine = create_engine(PG_URL)
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
        engine.dispose()
        return
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    yield session
    session.close()


@pytest.fixture()
def client(session_factory):
    def _override():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def add_user(db, username, role, **extra):
    user = AdminUser(username=username, password_hash=_PASSWORD_HASH, role=role, **extra)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def login(client, username):
    r = client.post("/api/admin/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def token_headers(user):
    """Auth header for a user without the cost of a bcrypt login (a real login is tested separately)."""
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """AI/upload limits are per process; start every test with empty counters."""
    if FLAG_ON:
        from app.services.performance_ratelimit import limiter

        limiter.reset()
    yield
