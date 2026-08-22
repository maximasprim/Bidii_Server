import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RolePermission(Base):
    """
    One row per admin role (other than "admin", which always has full
    dashboard access and is never stored here — see
    app/services/role_permissions.py). Lets an admin configure, from the
    dashboard, which menus each other role can see, instead of that
    mapping being fixed in frontend code.

    A role with no row here yet just means nobody has customized it —
    the effective permissions fall back to DEFAULT_MENU_ACCESS in
    app/services/role_permissions.py, so the app behaves exactly as it
    did before this table existed until an admin actually changes
    something.
    """

    __tablename__ = "role_permissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    role: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    allowed_menus: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )
    # Username, not a foreign key — a lightweight "who last changed this"
    # note for the settings page, same spirit as the ATS audit log; not
    # meant as a queryable relationship.
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
