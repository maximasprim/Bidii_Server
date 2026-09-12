"""
Creates in-app notifications for admins. See app/models/internal_notification.py
for why this is a separate system from the candidate-facing email one
(app/services/notifications.py).

New loan applications ALSO get emailed to whichever admins receive the
in-app notification below, if they have a work email on file and SMTP is
configured - see _email_recipients. This reuses the same
app/services/email_sender.py used for candidate emails, but sends via
its "loans" identity (kind="loans") rather than the candidate one, so it
can go out from a separate mailbox - see app/config.py's SMTP_LOANS_*
settings. It also isn't part of the template/automation system - it's a
fixed, internal ops notification, not a candidate-facing communication.

notify_officer_of_manual_assignment below covers the other side of this:
an admin manually (re)assigning an *existing* application to an agent via
the Loan Applications page, rather than the system routing a *new* one -
see app/routers/admin.py's assign_loan_application.
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


def notify_officer_of_manual_assignment(db: Session, *, officer: AdminUser, application, assigned_by: AdminUser) -> None:
    """
    In-app notification + email to whichever agent gets (re)assigned a
    loan application via PATCH /api/admin/loan-applications/{id}/assign
    (see app/routers/admin.py's assign_loan_application). Fires on every
    (re)assignment made through that endpoint - including handing an
    application that already had a different officer to someone else -
    since the newly assigned person always needs to know it's now
    theirs, regardless of whether it's their first time seeing it or a
    handover. Same "in-app first, commit, then a best-effort email
    second - never raises" pattern as notify_branch_of_new_application
    above and notify_routed_admin_of_new_application in
    app/services/product_routing.py (which this mirrors closely - kept
    separate because this one is about a specific admin's manual action
    on an application that already exists, not a product's fixed
    auto-routing target).
    """
    try:
        notify(
            db,
            recipient_admin_id=officer.id,
            message=f"{application.product_name} application from {application.full_name} was assigned to you by {assigned_by.username}.",
            link_path="/admin/loan-applications",
            related_loan_application_id=application.id,
        )
        db.commit()
    except Exception:  # noqa: BLE001 - must never break the assignment that triggered this
        db.rollback()
        logger.exception(
            "Failed to create in-app notification for manual assignment of application %r -> officer %r",
            getattr(application, "id", None),
            officer.id,
        )
        return  # don't attempt email off the back of a failed in-app notification

    if not officer.email or not is_email_configured(kind="loans"):
        return

    settings = get_settings()
    subject = f"{application.product_name} application assigned to you"
    body = (
        f"Hi {officer.username},\n\n"
        f"{assigned_by.username} has assigned you a {application.product_name} application.\n\n"
        f"Applicant: {application.full_name}\n"
        f"Phone: {application.phone}\n"
        f"Plan: {application.tier_label}\n"
        f"Amount requested: KES {application.amount:,.0f}\n"
        f"Location: {application.location or 'Not provided'}\n\n"
        f"Log in to the admin dashboard to review it:\n"
        f"{settings.site_url}/admin/loan-applications\n\n"
        f"Best regards,\n"
        f"{settings.company_name} System."
    )
    try:
        send_email(to_email=officer.email, subject=subject, body_text=body, kind="loans")
    except EmailError as exc:
        logger.warning("Failed to email manual-assignment notice to %r: %s", officer.email, exc)
    except Exception:  # noqa: BLE001 - one recipient's failure must never stop the rest, or the caller
        logger.exception("Unexpected error emailing manual-assignment notice to %r", officer.email)

        
def _email_recipients(recipients: list[AdminUser], *, branch_name: str, application) -> None:
    if not is_email_configured(kind="loans"):
        return

    settings = get_settings()
    subject = f"New loan application - {branch_name}"
    body = (
        f"Hi,\n\n"
        f"A new loan application has been routed to {branch_name} branch.\n\n"
        f"Applicant: {application.full_name}\n"
        f"Phone: {application.phone}\n"
        f"Product: {application.product_name} ({application.tier_label})\n"
        f"Amount requested: KES {application.amount:,.0f}\n"
        f"Location: {application.location or 'Not provided'}\n\n"
        f"Log in to the admin dashboard to review and assign it to a loan officer:\n"
        f"{settings.site_url}/admin/loan-applications\n\n"
        f"Best regards,\n"
        f"{settings.company_name} System."
    )

    for admin in recipients:
        if not admin.email:
            continue
        try:
            send_email(to_email=admin.email, subject=subject, body_text=body, kind="loans")
        except EmailError as exc:
            logger.warning("Failed to email branch-notification to %r: %s", admin.email, exc)
        except Exception:  # noqa: BLE001 - one recipient's failure must never stop the rest, or the caller
            logger.exception("Unexpected error emailing branch-notification to %r", admin.email)
