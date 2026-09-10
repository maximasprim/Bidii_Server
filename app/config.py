from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """
    Central app configuration. Values can be overridden via environment
    variables or a .env file (see .env.example) without touching code -
    e.g. CORS_ORIGINS for a production frontend domain, DATABASE_URL to
    switch off SQLite.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Bidii Credit API"
    environment: str = "development"

    database_url: str = f"sqlite:///{BASE_DIR / 'bidii.db'}"

    #Recently added supabase bucket for file uploads - Supabase configuration
    supabase_url: str
    supabase_service_role_key: str
    supabase_bucket: str = "Bidii-Uploads"

    # Comma-separated list of allowed frontend origins. Defaults cover the
    # Vite dev server and preview server ports used by the Bidii Credit
    # frontend during local development.
    cors_origins: str = "http://localhost:5173, https://www.bidiicreditkenya.co.ke, http://localhost:5174, http://127.0.0.1:5173, http://localhost:4173, http://127.0.0.1:4173"

    upload_dir: Path = BASE_DIR / "uploads"
    max_upload_size_mb: int = 5

    # The live public frontend's canonical URL (no trailing slash) - used
    # only by app/routers/sitemap.py to build absolute <loc> URLs. Override
    # via .env if you ever point this backend at a staging frontend.
    site_url: str = "https://www.bidiicreditkenya.co.ke"

    # Admin dashboard auth. Change these in production via .env - the
    # defaults here are only so the app runs out of the box in dev.
    admin_username: str = "admin"
    admin_password: str = "changeme123"
    jwt_secret: str = "dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 480  # 8 hour admin session

    # --- AI ATS Evaluation / AI Job Generation ---------------------------
    # All optional. Weighted scoring (the original ATS) works with none of
    # these set. AI Evaluation only becomes usable once the relevant key is
    # present here - see app/services/ai_providers/factory.py, the single
    # place that reads these. Never sent to the frontend; only a per-provider
    # `configured: true/false` boolean is exposed via GET /api/admin/ai/providers.
    openai_api_key: str | None = None
    openai_default_model: str = "gpt-4o-mini"
    gemini_api_key: str | None = None
    gemini_default_model: str = "gemini-1.5-flash"
    ai_request_timeout_seconds: int = 30

    # --- Outbound candidate email notifications --------------------------
    # All optional. When smtp_host is unset, the notification system skips
    # sending and logs a "skipped_not_configured" entry instead of raising
    # - see app/services/email_sender.py. Never sent to the frontend.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    smtp_from_email: str | None = None
    smtp_from_name: str = "Bidii Credit HR"
    company_name: str = "Bidii Credit"

    # --- Outbound loan-application email notifications (optional) --------
    # Lets loan-application emails (new application -> branch/ops admins,
    # or a single routed loan officer) go out from a separate mailbox -
    # e.g. loans@bidiicreditkenya.co.ke - instead of the recruitment inbox
    # above. Every field here is optional and falls back independently to
    # its SMTP_* counterpart when unset (see
    # app/services/email_sender.py::_resolve_profile), so:
    #   - leaving all of these unset keeps today's behaviour exactly as-is
    #     (loan emails still go out via the SMTP_* candidate identity), and
    #   - setting just SMTP_LOANS_FROM_EMAIL/SMTP_LOANS_FROM_NAME sends
    #     loan emails with a different From: address/name while still
    #     authenticating with the same SMTP_* account, if your mail server
    #     allows sending as an alias of that account, or
    #   - setting SMTP_LOANS_HOST/PORT/USERNAME/PASSWORD too logs into a
    #     fully separate mailbox for loan emails.
    smtp_loans_host: str | None = None
    smtp_loans_port: int | None = None
    smtp_loans_username: str | None = None
    smtp_loans_password: str | None = None
    smtp_loans_use_tls: bool | None = None
    smtp_loans_from_email: str | None = None
    smtp_loans_from_name: str = "Bidii Credit Loans"
    
    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
