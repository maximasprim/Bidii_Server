"""
Resolves and notifies the admin-configured "individual person" a
check-off/logbook/rental-income loan application should go to instead of
the normal branch-based routing - see ProductRoutingAssignment's
docstring in app/models/product_routing.py for the full picture of how
this interacts with app/services/branch_assignment.py.

Called from app/routers/loan_applications.py right after a new
application is saved and branch-assigned. Like every other
submission-time side effect in this codebase (notify_branch_of_new_application,
assign_branch), a failure here must never break the applicant's actual
submission - every public function below is safe to call unconditionally
and never raises.
"""

import logging

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.admin_user import AdminUser
from app.models.product_routing import ProductRoutingAssignment
from app.services.email_sender import EmailError, is_email_configured, send_email
from app.services.internal_notifications import notify

logger = logging.getLogger("bidii.product_routing")


def get_routed_admin(db: Session, product_slug: str) -> AdminUser | None:
    """
    None if this product has no routing row, no assigned_admin_id, or the
    assigned admin has since been deactivated (deactivating an admin
    account must never leave applications silently routed to someone who
    can no longer act on them - the product falls back to normal branch
    routing automatically until an admin picks someone else).
    """
    row = (
        db.query(ProductRoutingAssignment)
        .filter(ProductRoutingAssignment.product_slug == product_slug)
        .first()
    )
    if row is None or row.assigned_admin_id is None:
        return None

    admin = db.query(AdminUser).filter(AdminUser.id == row.assigned_admin_id).first()
    if admin is None or not admin.is_active:
        logger.warning(
            "Product %r is routed to admin_id %r, but that admin is missing or inactive - "
            "falling back to normal branch routing for this application. Pick a new person on "
            "the Loan Routing admin page to fix this going forward.",
            product_slug,
            row.assigned_admin_id,
        )
        return None
    return admin


def notify_routed_admin_of_new_application(db: Session, *, admin: AdminUser, application) -> None:
    """
    In-app notification + email to the one person a product is routed
    to - the single-recipient equivalent of
    notify_branch_of_new_application, reusing the same
    InternalNotification/email_sender plumbing. Never raises.
    """
    try:
        notify(
            db,
            recipient_admin_id=admin.id,
            message=f"New {application.product_name} application from {application.full_name} was routed directly to you.",
            link_path="/admin/loan-applications",
            related_loan_application_id=application.id,
        )
        db.commit()
    except Exception:  # noqa: BLE001 - must never break the loan application submission that triggered this
        db.rollback()
        logger.exception(
            "Failed to create in-app notification for routed application %r -> admin %r",
            getattr(application, "id", None),
            admin.id,
        )
        return  # don't attempt email off the back of a failed in-app notification

    if not admin.email or not is_email_configured():
        return

    settings = get_settings()
    subject = f"New {application.product_name} application routed to you"
    body = (
        f"Hi {admin.username},\n\n"
        f"A new {application.product_name} application has been routed directly to you.\n\n"
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
        send_email(to_email=admin.email, subject=subject, body_text=body)
    except EmailError as exc:
        logger.warning("Failed to email routed-application notice to %r: %s", admin.email, exc)
    except Exception:  # noqa: BLE001 - must never break the submission
        logger.exception("Unexpected error emailing routed-application notice to %r", admin.email)
