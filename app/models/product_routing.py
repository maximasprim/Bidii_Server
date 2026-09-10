import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Loan products that can be routed to one specific, admin-picked person
# instead of the normal branch-based routing in
# app/services/branch_assignment.py. Kept as an explicit list (matching
# PRODUCT_SLUGS in loan_tier.py) rather than "any product_slug works" so
# the "Loan Routing" admin page always has a fixed, predictable set of
# rows to show, even before any admin has assigned anyone to them yet.
#
# Covers every product in loan_tier.PRODUCT_SLUGS - sme-loans and
# mobile-loans were added alongside sme_loan_agent/mobile_loan_agent
# (see SUGGESTED_ROLE_FOR_PRODUCT below) to extend the same mechanism
# that already existed for check-off/logbook/rental-income loans.
ROUTABLE_PRODUCT_SLUGS = [
    "check-off-loans",
    "logbook-loans",
    "rental-income-loans",
    "sme-loans",
    "mobile-loans",
]

# The admin role each routable product's assignee is expected to hold -
# purely advisory (used to pre-filter/highlight candidates on the Loan
# Routing admin page and to pick a sensible default person if more than
# one admin has the matching role). The PUT endpoint in
# app/routers/admin_product_routing.py does NOT enforce this: an admin
# can assign any active admin user to any product if they have a good
# reason to (e.g. temporarily covering for someone), it's just not the
# suggested default.
SUGGESTED_ROLE_FOR_PRODUCT = {
    "check-off-loans": "check_off_agent",
    "logbook-loans": "logbook_agent",
    "rental-income-loans": "rental_loan_agent",
    "sme-loans": "sme_loan_agent",
    "mobile-loans": "mobile_loan_agent",
}


class ProductRoutingAssignment(Base):
    """
    TEMPORARY, admin-configurable routing override - see this repo's
    PRODUCT_BRANCH_RESTRICTIONS in app/services/branch_assignment.py for
    the sibling mechanism this complements. While a row here has a
    non-null assigned_admin_id, every *new* loan application for that
    product_slug (any of ROUTABLE_PRODUCT_SLUGS above - every loan
    product this business offers) is routed (assigned_loan_officer_id) directly to that
    one admin and they're emailed about it, instead of the normal
    branch-office-admin fan-out in
    app/services/internal_notifications.py's notify_branch_of_new_application.
    The application's branch is still computed and stored completely as
    normal by app/services/branch_assignment.py either way - this only
    changes who's actually handling it and who gets notified. See
    app/services/product_routing.py for exactly how the two interact.

    To revert a product back to normal routing, clear assigned_admin_id
    (or delete the row) from the "Loan Routing" admin page - nothing else
    needs to change, and it never affects applications already submitted.
    """

    __tablename__ = "product_routing_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    product_slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    assigned_admin_id: Mapped[str | None] = mapped_column(ForeignKey("admin_users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
