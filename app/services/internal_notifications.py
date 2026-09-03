"""
Creates in-app notifications for admins. See app/models/internal_notification.py
for why this is a separate system from the candidate-facing email one
(app/services/notifications.py).

New loan applications ALSO get emailed to whichever admins receive the
in-app notification below, if they have a work email on file and SMTP is
configured - see _email_recipients. This reuses the same
app/services/email_sender.py used for candidate emails, but isn't part of
that template/automation system - it's a fixed, internal ops
notification, not a candidate-facing communication.
"""

import logging

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.admin_user import AdminUser
from app.models.internal_notification import InternalNotification
from app.services.email_sender import EmailError, is_email_configured, send_email

logger = logging.getLogger("bidii.internal_notifications")


def notify(db: Session, *, recipient_admin_id: str, message: str, link_path: str | None = None, related_loan_application_id: str | None = None) -> None:
    db.add(
        InternalNotification(
            recipient_admin_id=recipient_admin_id,
            message=message,
            link_path=link_path,
            related_loan_application_id=related_loan_application_id,
        )
    )


def notify_branch_of_new_application(db: Session, *, branch_id: str, branch_name: str, application) -> None:
    """
    Fans out one notification per recipient: every branch_office_admin
    whose managed_branch_ids includes this branch. If none are configured
    for this branch yet, notifies every "admin"-role user instead, so a
    new application is never silently invisible to everyone. Called from
    app/routers/loan_applications.py right after a new application is
    assigned a branch - never raises, since a notification failure must
    never break the applicant's actual submission.

    Email is a second, independent step after the in-app notifications are
    committed - an email failure (or SMTP not being configured at all)
    never rolls back or affects the in-app notifications, and one
    recipient's failed email never stops the others from being attempted.
    """
    recipients: list[AdminUser] = []
    try:
        branch_admins = db.query(AdminUser).filter(AdminUser.role == "branch_office_admin", AdminUser.is_active.is_(True)).all()
        recipients = [ba for ba in branch_admins if ba.managed_branch_ids and branch_id in ba.managed_branch_ids]

        if not recipients:
            recipients = db.query(AdminUser).filter(AdminUser.role == "admin", AdminUser.is_active.is_(True)).all()

        message = f"New loan application from {application.full_name} routed to {branch_name}."
        for admin in recipients:
            notify(
                db,
                recipient_admin_id=admin.id,
                message=message,
                link_path="/admin/loan-applications",
                related_loan_application_id=application.id,
            )
        db.commit()
    except Exception:  # noqa: BLE001 - must never break the loan application submission that triggered this
        db.rollback()
        logger.exception("Failed to create internal notifications for new loan application %r", getattr(application, "id", None))
        return #don't attemp email off the back of a failed/unkown recipient list

    _email_recipients(recipients, branch_name=branch_name, application=application)


def _email_recipients(recipients: list[AdminUser], *, branch_name: str, application) -> None:
    if not is_email_configured():
        return

    settings = get_settings()
    subject = f"New loan application — {branch_name}"
    body = (
        f"Hi,\n\n"
        f"A new loan application has been routed to {branch_name}.\n\n"
        f"Applicant: {application.full_name}\n"
        f"Phone: {application.phone}\n"
        f"Product: {application.product_name} ({application.tier_label})\n"
        f"Amount requested: KES {application.amount:,.0f}\n"
        f"Location: {application.location or 'Not provided'}\n\n"
        f"Log in to the admin dashboard to review and assign it to a loan officer:\n"
        f"{settings.site_url}/admin/loan-applications\n\n"
        f"— {settings.company_name} System"
    )

    for admin in recipients:
        if not admin.email:
            continue
        try:
            send_email(to_email=admin.email, subject=subject, body_text=body)
        except EmailError as exc:
            logger.warning("Failed to email branch-notification to %r: %s", admin.email, exc)
        except Exception:  # noqa: BLE001 - one recipient's failure must never stop the rest, or the caller
            logger.exception("Unexpected error emailing branch-notification to %r", admin.email)
