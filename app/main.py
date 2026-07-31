import logging
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.routers import (
    admin,
    admin_auth,
    admin_jobs,
    admin_loan_tiers,
    admin_news,
    careers,
    contact,
    jobs,
    loan_applications,
    loan_tiers,
    news,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger("bidii.startup")
settings = get_settings()

# Creates tables if they don't exist yet. Fine for a SQLite-backed project
# like this one; swap for Alembic migrations if you move to Postgres/MySQL
# and need versioned schema changes in production.
Base.metadata.create_all(bind=engine)


def _bootstrap_first_admin() -> None:
    """
    Seeds one admin user from ADMIN_USERNAME/ADMIN_PASSWORD in .env, but
    only if the admin_users table is completely empty — after the first
    admin exists, further admins are created from the dashboard itself
    (POST /api/admin/users), not by editing .env and restarting.
    """
    # Imported here rather than at module level to avoid a circular import
    # at import time (app.models.admin_user -> app.database -> app.main).
    from app.models.admin_user import AdminUser
    from app.services.auth import hash_password

    db = SessionLocal()
    try:
        if db.query(AdminUser).first() is not None:
            return
        db.add(AdminUser(username=settings.admin_username, password_hash=hash_password(settings.admin_password)))
        db.commit()
        logger.info("Seeded first admin user %r from .env — change this password after logging in.", settings.admin_username)
    finally:
        db.close()


_bootstrap_first_admin()


def _bootstrap_loan_tiers() -> None:
    """
    Seeds the original static tier data into the loan_tiers table, but only
    if that table is completely empty — this is a one-time migration from
    "tiers hardcoded in Python" to "tiers configured by an admin," not a
    sync that runs on every startup. Once seeded, all further changes go
    through the admin Loan Terms page, not this function or a restart.
    """
    from app.data.seed_loan_tiers import SEED_LOAN_TIERS
    from app.models.loan_tier import LoanTier

    db = SessionLocal()
    try:
        if db.query(LoanTier).first() is not None:
            return
        for tier_data in SEED_LOAN_TIERS:
            db.add(LoanTier(**tier_data))
        db.commit()
        logger.info("Seeded %d initial loan tiers.", len(SEED_LOAN_TIERS))
    finally:
        db.close()


_bootstrap_loan_tiers()

app = FastAPI(
    title=settings.app_name,
    description="Backend API for the Bidii Credit website (contact, loan applications, careers).",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sanitize_errors(errors: list[dict]) -> list[dict]:
    """
    Pydantic can embed the raw exception instance in an error's ctx.error
    field (e.g. when a @model_validator raises ValueError) — that's not
    JSON-serializable, so stringify it before this ever reaches json.dumps.
    """
    cleaned = []
    for error in errors:
        error = dict(error)
        ctx = error.get("ctx")
        if isinstance(ctx, dict) and "error" in ctx:
            ctx = dict(ctx)
            ctx["error"] = str(ctx["error"])
            error["ctx"] = ctx
        cleaned.append(error)
    return cleaned


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    Returns a consistent { success, message, errors } shape on validation
    failure instead of FastAPI's default body, so any frontend error
    handling only has to understand one error format across this API.
    """
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "success": False,
            "message": "Validation failed.",
            "errors": _sanitize_errors(exc.errors()),
        },
    )


@app.get("/api/health", tags=["health"])
def health_check() -> dict:
    return {"status": "ok", "service": settings.app_name}


app.include_router(contact.router)
app.include_router(loan_applications.router)
app.include_router(careers.router)
app.include_router(admin_auth.router)
app.include_router(admin.router)
app.include_router(news.router)
app.include_router(admin_news.router)
app.include_router(jobs.router)
app.include_router(admin_jobs.router)
app.include_router(loan_tiers.router)
app.include_router(admin_loan_tiers.router)

# Self-contained admin dashboard (login, stats, submissions) — a static
# HTML/CSS/JS page with no build step, served at /admin/. It talks to the
# /api/admin/* endpoints above using a bearer token from /api/admin/login.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/admin", StaticFiles(directory=_STATIC_DIR / "admin", html=True), name="admin")
