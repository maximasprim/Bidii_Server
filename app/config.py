from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """
    Central app configuration. Values can be overridden via environment
    variables or a .env file (see .env.example) without touching code —
    e.g. CORS_ORIGINS for a production frontend domain, DATABASE_URL to
    switch off SQLite.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Bidii Credit API"
    environment: str = "development"

    database_url: str = f"sqlite:///{BASE_DIR / 'bidii.db'}"

    # Comma-separated list of allowed frontend origins. Defaults cover the
    # Vite dev server and preview server ports used by the Bidii Credit
    # frontend during local development.
    cors_origins: str = "http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173"

    upload_dir: Path = BASE_DIR / "uploads"
    max_upload_size_mb: int = 5

    # Admin dashboard auth. Change these in production via .env — the
    # defaults here are only so the app runs out of the box in dev.
    admin_username: str = "admin"
    admin_password: str = "changeme123"
    jwt_secret: str = "dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 480  # 8 hour admin session

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
