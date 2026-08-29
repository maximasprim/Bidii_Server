"""
In-app notifications for admins - e.g. "a new loan application was routed
to your branch." Distinct from app/models/notification.py, which is the
*candidate-facing email* system (templates, automation rules, SMTP). This
one never sends an email; it's purely a bell-icon inbox inside the admin
dashboard itself.

Each row belongs to exactly one recipient admin - a broadcast to multiple
admins (e.g. every regional manager covering a branch) creates one row per
recipient, rather than one shared row with per-recipient read-tracking.
Simpler to reason about at the scale this runs at, and it means
`is_read` can live directly on this table with no join.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class InternalNotification(Base):
    __tablename__ = "internal_notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recipient_admin_id: Mapped[str] = mapped_column(ForeignKey("admin_users.id"), index=True)
    message: Mapped[str] = mapped_column(Text)
    # Frontend route to send the admin to when they click the notification,
    # e.g. "/admin/loan-applications" - optional, purely a UX convenience.
    link_path: Mapped[str | None] = mapped_column(String(200), nullable=True)
    related_loan_application_id: Mapped[str | None] = mapped_column(
        ForeignKey("loan_applications.id"), nullable=True
    )
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
