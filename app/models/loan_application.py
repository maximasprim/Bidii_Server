import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LoanApplicationStatus(str, enum.Enum):
    pending = "pending"
    contacted = "contacted"
    approved = "approved"
    declined = "declined"


class LoanApplication(Base):
    __tablename__ = "loan_applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    product_slug: Mapped[str] = mapped_column(String(60), index=True)
    product_name: Mapped[str] = mapped_column(String(120))
    tier_id: Mapped[str] = mapped_column(String(60))
    tier_label: Mapped[str] = mapped_column(String(120))

    amount: Mapped[float] = mapped_column(Float)
    term_value: Mapped[int] = mapped_column(Integer)
    term_unit: Mapped[str] = mapped_column(String(10))
    estimated_installment: Mapped[float] = mapped_column(Float)

    full_name: Mapped[str] = mapped_column(String(200))
    id_number: Mapped[str] = mapped_column(String(20))
    phone: Mapped[str] = mapped_column(String(40))
    email: Mapped[str] = mapped_column(String(320), index=True)
    monthly_income: Mapped[str] = mapped_column(String(100))
    # Free-text location the applicant typed (e.g. a town, estate, or
    # neighborhood) - not validated against real places, since applicants
    # phrase locations in all sorts of ways. Nullable only so existing rows
    # from before this column existed don't break; every new submission is
    # required to include one (enforced in the Create schema, not here).
    location: Mapped[str | None] = mapped_column(String(200), nullable=True, default=None)

    # Which branch this application is routed to, and how that was decided
    # - see app/services/branch_assignment.py. Every application always
    # ends up assigned to some real, active branch (never left blank) -
    # branch_assignment_method records whether that was a confident direct
    # match, an AI-assisted nearest-branch guess, or the last-resort
    # default, so admins can tell at a glance which assignments are worth
    # double-checking.
    assigned_branch_id: Mapped[str | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    branch_assignment_method: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "exact"|"ai"|"fallback"
    # Set by a regional manager (or admin) assigning the application to a
    # specific loan officer within the assigned branch - null until then.
    assigned_loan_officer_id: Mapped[str | None] = mapped_column(ForeignKey("admin_users.id"), nullable=True)

    status: Mapped[LoanApplicationStatus] = mapped_column(
        Enum(LoanApplicationStatus), default=LoanApplicationStatus.pending
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
