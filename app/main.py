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
    admin_ai,
    admin_ats_config,
    admin_ats_screening,
    admin_ats_vetting,
    admin_auth,
    admin_branches,
    admin_jobs,
    admin_internal_notifications,
    admin_loan_tiers,
    admin_news,
    admin_notifications,
    admin_role_permissions,
    branches,
    careers,
    contact,
    jobs,
    loan_applications,
    loan_tiers,
    news,
    sitemap,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger("bidii.startup")
settings = get_settings()

# Creates tables if they don't exist yet. Fine for a SQLite-backed project
# like this one; swap for Alembic migrations if you move to Postgres/MySQL
# and need versioned schema changes in production.
Base.metadata.create_all(bind=engine)


def _migrate_schema() -> None:
    """
    Adds columns introduced after a database already existed (role on
    admin_users, requirements/responsibilities on job_openings, image_urls
    on news_articles). Base.metadata.create_all above only creates missing
    TABLES, never missing COLUMNS on tables that already exist, so brand
    new columns need this instead. Safe to run on every startup - each
    statement is skipped once its column is present. Swap for Alembic if
    this project needs more than the occasional additive column.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    statements: list[str] = []

    def _rows_exist(sql: str) -> bool:
        """One-off read used only by guarded data-migrations below (as opposed to the
        ALTER TABLE statements, which are guarded by column-existence instead)."""
        with engine.connect() as check_conn:
            return check_conn.execute(text(sql)).first() is not None

    if "admin_users" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("admin_users")}
        if "role" not in columns:
            statements.append("ALTER TABLE admin_users ADD COLUMN role VARCHAR(20) DEFAULT 'admin'")
        if "branch_id" not in columns:
            statements.append("ALTER TABLE admin_users ADD COLUMN branch_id VARCHAR(36)")
        if "managed_branch_ids" not in columns:
            statements.append("ALTER TABLE admin_users ADD COLUMN managed_branch_ids JSON")
        if "email" not in columns:
            statements.append("ALTER TABLE admin_users ADD COLUMN email VARCHAR(320)")
        if "role" in columns and _rows_exist("SELECT 1 FROM admin_users WHERE role = 'regional_manager' LIMIT 1"):
            statements.append("UPDATE admin_users SET role = 'branch_office_admin' WHERE role = 'regional_manager'")

    if "role_permissions" in existing_tables:
        # Same rename, applied to a saved menu-access override too - a
        # customization saved under the old role name would otherwise
        # become orphaned (referring to a role that no longer exists) and
        # silently stop applying.
        if _rows_exist("SELECT 1 FROM role_permissions WHERE role = 'regional_manager' LIMIT 1"):
            statements.append("UPDATE role_permissions SET role = 'branch_office_admin' WHERE role = 'regional_manager'")

    if "loan_applications" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("loan_applications")}
        if "location" not in columns:
            statements.append("ALTER TABLE loan_applications ADD COLUMN location VARCHAR(200)")
        if "assigned_branch_id" not in columns:
            statements.append("ALTER TABLE loan_applications ADD COLUMN assigned_branch_id VARCHAR(36)")
        if "branch_assignment_method" not in columns:
            statements.append("ALTER TABLE loan_applications ADD COLUMN branch_assignment_method VARCHAR(20)")
        if "assigned_loan_officer_id" not in columns:
            statements.append("ALTER TABLE loan_applications ADD COLUMN assigned_loan_officer_id VARCHAR(36)")
        if "county" not in columns:
            statements.append("ALTER TABLE loan_applications ADD COLUMN county VARCHAR(50)")

    if "loan_applications" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("loan_applications")}
        if "location" not in columns:
            statements.append("ALTER TABLE loan_applications ADD COLUMN location VARCHAR(200)")
        if "assigned_branch_id" not in columns:
            statements.append("ALTER TABLE loan_applications ADD COLUMN assigned_branch_id VARCHAR(36)")
        if "branch_assignment_method" not in columns:
            statements.append("ALTER TABLE loan_applications ADD COLUMN branch_assignment_method VARCHAR(20)")
        if "assigned_loan_officer_id" not in columns:
            statements.append("ALTER TABLE loan_applications ADD COLUMN assigned_loan_officer_id VARCHAR(36)")
        if "county" not in columns:
            statements.append("ALTER TABLE loan_applications ADD COLUMN county VARCHAR(50)")


    if "branches" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("branches")}
        if "county" not in columns:
            statements.append("ALTER TABLE branches ADD COLUMN county VARCHAR(50)")

    if "job_openings" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("job_openings")}
        if "requirements" not in columns:
            statements.append("ALTER TABLE job_openings ADD COLUMN requirements JSON DEFAULT '[]'")
        if "responsibilities" not in columns:
            statements.append("ALTER TABLE job_openings ADD COLUMN responsibilities JSON DEFAULT '[]'")
        if "application_deadline" not in columns:
            statements.append("ALTER TABLE job_openings ADD COLUMN application_deadline DATE")
        if "jd_content" not in columns:
            statements.append("ALTER TABLE job_openings ADD COLUMN jd_content JSON")

    if "news_articles" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("news_articles")}
        if "image_urls" not in columns:
            statements.append("ALTER TABLE news_articles ADD COLUMN image_urls JSON DEFAULT '[]'")

    # AI ATS Evaluation columns - added after ats_configurations /
    # ats_screening_results already existed on some deployments. All
    # default to the weighted-scoring behavior (evaluation_mode='weighted',
    # everything AI-related NULL/empty), so this is a no-op for anyone not
    # opting into AI evaluation.
    if "ats_configurations" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("ats_configurations")}
        if "evaluation_mode" not in columns:
            statements.append("ALTER TABLE ats_configurations ADD COLUMN evaluation_mode VARCHAR(20) DEFAULT 'weighted'")
        if "ai_provider" not in columns:
            statements.append("ALTER TABLE ats_configurations ADD COLUMN ai_provider VARCHAR(20)")
        if "ai_model" not in columns:
            statements.append("ALTER TABLE ats_configurations ADD COLUMN ai_model VARCHAR(100)")

    if "ats_screening_results" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("ats_screening_results")}
        if "evaluation_method" not in columns:
            statements.append("ALTER TABLE ats_screening_results ADD COLUMN evaluation_method VARCHAR(20) DEFAULT 'weighted'")
        if "ai_provider" not in columns:
            statements.append("ALTER TABLE ats_screening_results ADD COLUMN ai_provider VARCHAR(20)")
        if "ai_model" not in columns:
            statements.append("ALTER TABLE ats_screening_results ADD COLUMN ai_model VARCHAR(100)")
        if "ai_strengths" not in columns:
            statements.append("ALTER TABLE ats_screening_results ADD COLUMN ai_strengths JSON DEFAULT '[]'")
        if "ai_weaknesses" not in columns:
            statements.append("ALTER TABLE ats_screening_results ADD COLUMN ai_weaknesses JSON DEFAULT '[]'")
        if "ai_explanation" not in columns:
            statements.append("ALTER TABLE ats_screening_results ADD COLUMN ai_explanation TEXT")
        if "ai_fallback_reason" not in columns:
            statements.append("ALTER TABLE ats_screening_results ADD COLUMN ai_fallback_reason TEXT")

    if not statements:
        return

    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    logger.info("Applied %d schema migration statement(s): %s", len(statements), statements)


_migrate_schema()


def _bootstrap_first_admin() -> None:
    """
    Seeds one admin user from ADMIN_USERNAME/ADMIN_PASSWORD in .env, but
    only if the admin_users table is completely empty - after the first
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
        db.add(
            AdminUser(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                role="admin",
            )
        )
        db.commit()
        logger.info("Seeded first admin user %r from .env - change this password after logging in.", settings.admin_username)
    finally:
        db.close()


_bootstrap_first_admin()


def _bootstrap_loan_tiers() -> None:
    """
    Seeds the original static tier data into the loan_tiers table, but only
    if that table is completely empty - this is a one-time migration from
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


def _bootstrap_branches() -> None:
    """
    Same one-time migration pattern as _bootstrap_loan_tiers above, for
    the `branches` table - seeds the original hardcoded branch list (that
    used to live in src/data/content.ts on the frontend) only if the
    table is completely empty. Once seeded, further changes go through
    the admin Branches page, not this function or a restart.
    """
    from app.data.seed_branches import SEED_BRANCHES
    from app.models.branch import Branch

    db = SessionLocal()
    try:
        if db.query(Branch).first() is not None:
            return
        for branch_data in SEED_BRANCHES:
            db.add(Branch(**branch_data))
        db.commit()
        logger.info("Seeded %d initial branches.", len(SEED_BRANCHES))
    finally:
        db.close()


_bootstrap_branches()

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
    field (e.g. when a @model_validator raises ValueError) - that's not
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
app.include_router(sitemap.router)
app.include_router(admin_news.router)
app.include_router(jobs.router)
app.include_router(admin_jobs.router)
app.include_router(admin_internal_notifications.router)
app.include_router(loan_tiers.router)
app.include_router(admin_loan_tiers.router)
app.include_router(admin_ats_config.router)
app.include_router(admin_ats_screening.router)
app.include_router(admin_ats_vetting.router)
app.include_router(admin_ai.router)
app.include_router(admin_role_permissions.router)
app.include_router(admin_notifications.router)
app.include_router(branches.router)
app.include_router(admin_branches.router)

# Self-contained admin dashboard (login, stats, submissions) - a static
# HTML/CSS/JS page with no build step, served at /admin/. It talks to the
# /api/admin/* endpoints above using a bearer token from /api/admin/login.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/admin", StaticFiles(directory=_STATIC_DIR / "admin", html=True), name="admin")
