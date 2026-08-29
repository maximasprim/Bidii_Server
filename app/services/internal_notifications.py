"""
Creates in-app notifications for admins. See app/models/internal_notification.py
for why this is a separate system from the candidate-facing email one
(app/services/notifications.py).
"""

import logging

from sqlalchemy.orm import Session

from app.models.admin_user import AdminUser
from app.models.internal_notification import InternalNotification

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
    Fans out one notification per recipient: every regional_manager whose
    managed_branch_ids includes this branch. If none are configured for
    this branch yet, notifies every "admin"-role user instead, so a new
    application is never silently invisible to everyone. Called from
    app/routers/loan_applications.py right after a new application is
    assigned a branch — never raises, since a notification failure must
    never break the applicant's actual submission.
    """
    try:
        regional_managers = db.query(AdminUser).filter(AdminUser.role == "regional_manager", AdminUser.is_active.is_(True)).all()
        recipients = [rm for rm in regional_managers if rm.managed_branch_ids and branch_id in rm.managed_branch_ids]

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
