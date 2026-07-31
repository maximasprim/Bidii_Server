from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db

settings = get_settings()
_bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expiry_minutes)
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> str:
    """Returns the token's subject (the admin user's ID), or raises 401."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        subject = payload.get("sub")
        if not subject:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")
        return subject
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired. Log in again.") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token.") from exc


def get_current_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
):
    """
    FastAPI dependency — protects admin routes. Use as: Depends(get_current_admin).
    Returns the current AdminUser ORM object (not just a username string) —
    the JWT subject is the user's stable ID, not their username, precisely
    so that an admin renaming themselves doesn't invalidate their own
    still-valid token on their very next request.
    Re-checks the DB on every request (not just at login) so deactivating a
    user revokes access immediately, rather than only once their existing
    token happens to expire.
    """
    # Imported here, not at module level, to avoid a circular import
    # (app.models.admin_user -> app.database -> ... -> app.services.auth).
    from app.models.admin_user import AdminUser

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = decode_access_token(credentials.credentials)

    user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This account no longer has access. Log in again or contact another admin.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
