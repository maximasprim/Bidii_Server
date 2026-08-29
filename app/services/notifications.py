"""
Orchestrates candidate email notifications:
- render_template(): fills {{placeholders}} into a template's subject/body.
- send_notification(): renders, sends via app/services/email_sender.py,
  and always logs the outcome (sent/failed/skipped) - never raises, so it
  is always safe to call from code that has other, more important work to
  finish (like actually saving a status change).
- maybe_auto_notify(): the hook other routers call right after changing a
  CareerApplication's status. Looks up whether that status has an enabled
  NotificationAutomationRule with a template, and if so sends it.

Called from: app/routers/admin.py (manual status update) and
app/routers/admin_ats_screening.py (both auto-reject paths) - see each
call site for why it's always called *after* the status-changing commit,
never before.
"""

import logging

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.career_application import CareerApplication
from app.models.notification import (
    NotificationAutomationRule,
    NotificationLog,
    NotificationLogStatus,
    NotificationTemplate,
    NotificationTrigger,
)
from app.services.email_sender import EmailError, EmailNotConfiguredError, send_email

logger = logging.getLogger("bidii.notifications")


def render_template(text: str, *, application: CareerApplication, status_label: str | None = None) -> str:
    """
    Fills the placeholders a template author can use:
    {{candidate_name}}, {{job_title}}, {{company_name}}, {{status}}.
    Unknown {{placeholders}} are left as-is rather than raising, so a typo
    in a template shows up as visibly-wrong output an admin will notice
    and fix, not a failed send.
    """
    settings = get_settings()
    replacements = {
        "{{candidate_name}}": application.full_name,
        "{{job_title}}": application.role,
        "{{company_name}}": settings.company_name,
        "{{status}}": status_label or application.status.value,
    }
    rendered = text
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def send_notification(
    db: Session,
    application: CareerApplication,
    *,
    subject_template: str,
    body_template: str,
    trigger: NotificationTrigger,
    template_id: str | None = None,
    admin_id: str | None = None,
) -> NotificationLog:
    """
    Renders and sends one email to `application.email`, then writes exactly
    one NotificationLog row regardless of outcome. Never raises - a failed
    or unconfigured send is recorded, not propagated, since a notification
    problem should never be mistaken for (or block) the caller's own work.
    """
    subject = render_template(subject_template, application=application)
    body = render_template(body_template, application=application)

    log = NotificationLog(
        application_id=application.id,
        template_id=template_id,
        trigger=trigger,
        recipient_email=application.email,
        subject=subject,
        body=body,
        status=NotificationLogStatus.sent,
        sent_by_admin_id=admin_id,
    )

    try:
        send_email(to_email=application.email, subject=subject, body_text=body)
        log.status = NotificationLogStatus.sent
    except EmailNotConfiguredError as exc:
        log.status = NotificationLogStatus.skipped_not_configured
        log.error_message = str(exc)
    except EmailError as exc:
        log.status = NotificationLogStatus.failed
        log.error_message = str(exc)
        logger.warning("Failed to send notification email for application %r: %s", application.id, exc)

    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def maybe_auto_notify(db: Session, application: CareerApplication, new_status: str) -> None:
    """
    Called right after a CareerApplication's status is committed to a new
    value. Never raises - a broken automation rule or an SMTP outage must
    never surface as a failure of the status-change request that triggered
    it; the failure (if any) is still visible afterwards via
    GET /api/admin/notifications/logs.
    """
    try:
        trigger = NotificationTrigger(new_status)
    except ValueError:
        return  # not a recognised status - nothing to automate

    try:
        rule = db.query(NotificationAutomationRule).filter(NotificationAutomationRule.trigger == trigger).first()
        if rule is None or not rule.is_enabled or not rule.template_id:
            return
        template = db.query(NotificationTemplate).filter(NotificationTemplate.id == rule.template_id).first()
        if template is None or not template.is_active:
            return

        send_notification(
            db,
            application,
            subject_template=template.subject,
            body_template=template.body,
            trigger=trigger,
            template_id=template.id,
            admin_id=None,  # None = sent automatically by the system, not a specific admin
        )
    except Exception:  # noqa: BLE001 - deliberately broad: see docstring above
        logger.exception("Auto-notification failed for application %r (status=%r)", application.id, new_status)
