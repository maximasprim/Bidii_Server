"""
Configurable candidate-email notification system. Three concerns, three
tables:
- NotificationTemplate: reusable subject/body text with {{placeholders}}.
- NotificationAutomationRule: at most one rule per CareerApplicationStatus,
  optionally wired to a template - when enabled, a status change to that
  value automatically sends that template (see app/services/notifications.py,
  called from wherever CareerApplication.status changes).
- NotificationLog: a record of every email actually attempted (sent,
  failed, or skipped because email isn't configured), automatic or manual,
  so admins can see what a candidate was actually told and when.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class NotificationTrigger(str, enum.Enum):
    """
    Mirrors CareerApplicationStatus's values exactly, plus "manual" for a
    template that's only ever sent by hand (never wired to automation) -
    kept as a separate enum (rather than reusing CareerApplicationStatus
    directly) so this module doesn't need to change if that one ever
    does, and so "manual" has somewhere valid to live.
    """

    received = "received"
    reviewing = "reviewing"
    shortlisted = "shortlisted"
    rejected = "rejected"
    hired = "hired"
    manual = "manual"


class NotificationLogStatus(str, enum.Enum):
    sent = "sent"
    failed = "failed"
    skipped_not_configured = "skipped_not_configured"


class NotificationTemplate(Base):
    __tablename__ = "notification_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(150))
    trigger: Mapped[NotificationTrigger] = mapped_column(Enum(NotificationTrigger), default=NotificationTrigger.manual)
    subject: Mapped[str] = mapped_column(String(255))
    # Plain text with {{candidate_name}} / {{job_title}} / {{company_name}}
    # / {{status}} placeholders - see app/services/notifications.py for the
    # exact substitution rules. Rendered into a simple styled HTML wrapper
    # at send time (see app/services/email_sender.py), not stored as HTML.
    body: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class NotificationAutomationRule(Base):
    __tablename__ = "notification_automation_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # One row per real status (received/reviewing/shortlisted/rejected/hired)
    # - "manual" never gets a row here, since it's not a status a candidate
    # transitions into automatically. Enforced at the application layer
    # (see admin_notifications.py's get-or-create pattern), not a DB
    # constraint, to match how ATSConfiguration/RolePermission etc. already
    # do their own one-row-per-key upserts in this codebase.
    trigger: Mapped[NotificationTrigger] = mapped_column(Enum(NotificationTrigger), unique=True)
    template_id: Mapped[str | None] = mapped_column(ForeignKey("notification_templates.id"), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class NotificationLog(Base):
    __tablename__ = "notification_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    application_id: Mapped[str] = mapped_column(ForeignKey("career_applications.id"), index=True)
    # Nullable: a fully custom one-off email (no template selected) still
    # gets logged, just with no template to point back to.
    template_id: Mapped[str | None] = mapped_column(ForeignKey("notification_templates.id"), nullable=True)
    trigger: Mapped[NotificationTrigger] = mapped_column(Enum(NotificationTrigger), default=NotificationTrigger.manual)
    recipient_email: Mapped[str] = mapped_column(String(320))
    # Snapshots of what was actually sent, so a later template edit never
    # rewrites what this log says a candidate was told.
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[NotificationLogStatus] = mapped_column(Enum(NotificationLogStatus))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Null = sent automatically by the system (an automation rule), not by
    # a specific admin.
    sent_by_admin_id: Mapped[str | None] = mapped_column(ForeignKey("admin_users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
