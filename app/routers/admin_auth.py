import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.admin_user import AdminUser
from app.schemas.auth import AdminLoginRequest, AdminLoginResponse
from app.services.auth import create_access_token, verify_password

logger = logging.getLogger("bidii.admin_auth")
settings = get_settings()

router = APIRouter(prefix="/api/admin", tags=["admin-auth"])


@router.post("/login", response_model=AdminLoginResponse)
def admin_login(payload: AdminLoginRequest, db: Session = Depends(get_db)) -> AdminLoginResponse:
    """
    DB-backed admin login. The first admin user is seeded automatically on
    startup from ADMIN_USERNAME/ADMIN_PASSWORD in .env (see main.py) - after
    that, additional admins are created from the dashboard itself
    (POST /api/admin/users), not through environment variables.
    """
    user = db.query(AdminUser).filter(AdminUser.username == payload.username).first()

    # Always run verify_password, even when no user was found, using a dummy
    # hash - otherwise a "no such user" response returns faster than a
    # "wrong password" response, letting an attacker enumerate usernames by
    # timing alone.
    password_hash = user.password_hash if user else "$2b$12$" + "0" * 53
    password_ok = verify_password(payload.password, password_hash)

    if not user or not user.is_active or not password_ok:
        logger.warning("Failed admin login attempt for username=%r", payload.username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password.")

    token = create_access_token(subject=user.id, role=user.role)
    return AdminLoginResponse(access_token=token, expires_in_minutes=settings.jwt_expiry_minutes, role=user.role)
