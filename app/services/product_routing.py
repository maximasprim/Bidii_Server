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

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.admin_user import AdminUser
from app.models.loan_application import LoanApplication
from app.models.product_routing import ProductRoutingAgent, ProductRoutingAssignment
from app.services.email_sender import EmailError, is_email_configured, send_email
from app.services.internal_notifications import notify

logger = logging.getLogger("bidii.product_routing")


def get_routing_agent_ids(db: Session, product_slug: str) -> list[str]:
    """
    Every agent id a product is routed to, in the order they were added. Rows in
    ProductRoutingAgent win; if a product has none, the legacy single
    ProductRoutingAssignment.assigned_admin_id is its only agent (so routing set up
    before multi-agent support existed keeps working unchanged).
    """
    ids = [
        r.admin_id
        for r in db.query(ProductRoutingAgent)
        .filter(ProductRoutingAgent.product_slug == product_slug)
        .order_by(ProductRoutingAgent.created_at.asc(), ProductRoutingAgent.id.asc())
        .all()
    ]
    if ids:
        return ids
    row = (
        db.query(ProductRoutingAssignment)
        .filter(ProductRoutingAssignment.product_slug == product_slug)
        .first()
    )
    return [row.assigned_admin_id] if row is not None and row.assigned_admin_id else []


def get_routed_admin(db: Session, product_slug: str) -> AdminUser | None:
    """
    The agent the next new application for this product should go to, or None for
    normal branch routing (no agents configured, or every configured agent has been
    deactivated or deleted - deactivating an account must never leave applications
    silently routed to someone who can no longer act on them).

    With one agent that is simply that agent (unchanged behaviour). With several, the
    active ones take turns: the agent who was assigned an application for this product
    longest ago (or never) goes next. No counter is stored - it is worked out from the
    existing assignments, so adding, removing or deactivating agents just works.
    """
    ids = get_routing_agent_ids(db, product_slug)
    if not ids:
        return None

    by_id = {a.id: a for a in db.query(AdminUser).filter(AdminUser.id.in_(ids)).all()}
    active = [by_id[i] for i in ids if i in by_id and by_id[i].is_active]
    if not active:
        logger.warning(
            "Product %r is routed to admin_ids %r, but none of them are active - "
            "falling back to normal branch routing for this application. Pick new people on "
            "the Loan Routing admin page to fix this going forward.",
            product_slug,
            ids,
        )
        return None
    if len(active) == 1:
        return active[0]

    last_assigned = dict(
        db.query(LoanApplication.assigned_loan_officer_id, func.max(LoanApplication.created_at))
        .filter(
            LoanApplication.product_slug == product_slug,
            LoanApplication.assigned_loan_officer_id.in_([a.id for a in active]),
        )
        .group_by(LoanApplication.assigned_loan_officer_id)
        .all()
    )

    def turn_key(agent: AdminUser):
        ts = last_assigned.get(agent.id)
        return (0, 0) if ts is None else (1, ts)  # never assigned goes first, then oldest

    return min(active, key=turn_key)  # min() keeps list order on ties


def get_active_routing_agents(db: Session, product_slug: str) -> list[AdminUser]:
    """Every currently active agent the product is routed to (the whole ticked team)."""
    ids = get_routing_agent_ids(db, product_slug)
    if not ids:
        return []
    by_id = {a.id: a for a in db.query(AdminUser).filter(AdminUser.id.in_(ids)).all()}
    return [by_id[i] for i in ids if i in by_id and by_id[i].is_active]


def notify_routed_team_of_new_application(db: Session, *, others: list[AdminUser], owner: AdminUser, application) -> None:
    """
    When a product is routed to several agents, the one whose turn it is (``owner``) is
    assigned the application and notified by notify_routed_admin_of_new_application. This
    tells everyone else ticked for the product as well - in-app + email, same "loans" SMTP
    identity - saying who it was assigned to. Never raises, and one person's failure never
    stops the rest.
    """
    settings = get_settings()
    for admin in others:
        try:
            notify(
                db,
                recipient_admin_id=admin.id,
                message=(
                    f"New {application.product_name} application from {application.full_name} "
                    f"was routed to your team and assigned to {owner.username}."
                ),
                link_path="/admin/loan-applications",
                related_loan_application_id=application.id,
            )
            db.commit()
        except Exception:  # noqa: BLE001 - must never break the submission
            db.rollback()
            logger.exception("Failed to create in-app team notification for application %r -> admin %r", getattr(application, "id", None), admin.id)
            continue

        if not admin.email or not is_email_configured(kind="loans"):
            continue
        subject = f"New {application.product_name} application routed to your team"
        body = (
            f"Hi {admin.username},\n\n"
            f"A new {application.product_name} application has been routed to your team and assigned to {owner.username}.\n\n"
            f"Applicant: {application.full_name}\n"
            f"Phone: {application.phone}\n"
            f"Plan: {application.tier_label}\n"
            f"Amount requested: KES {application.amount:,.0f}\n"
            f"Location: {application.location or 'Not provided'}\n\n"
            f"Log in to the admin dashboard:\n"
            f"{settings.site_url}/admin/loan-applications\n\n"
            f"Best regards,\n"
            f"{settings.company_name} System."
        )
        try:
            send_email(to_email=admin.email, subject=subject, body_text=body, kind="loans")
        except EmailError as exc:
            logger.warning("Failed to email team routing notice to %r: %s", admin.email, exc)
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected error emailing team routing notice to %r", admin.email)


def notify_routed_admin_of_new_application(db: Session, *, admin: AdminUser, application) -> None:
    """
    In-app notification + email to the one person a product is routed
    to - the single-recipient equivalent of
    notify_branch_of_new_application, reusing the same
    InternalNotification/email_sender plumbing (including its "loans"
    SMTP identity, kind="loans" - see app/config.py's SMTP_LOANS_*
    settings). Never raises.
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

    if not admin.email or not is_email_configured(kind="loans"):
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
        send_email(to_email=admin.email, subject=subject, body_text=body, kind="loans")
    except EmailError as exc:
        logger.warning("Failed to email routed-application notice to %r: %s", admin.email, exc)
    except Exception:  # noqa: BLE001 - must never break the submission
        logger.exception("Unexpected error emailing routed-application notice to %r", admin.email)
