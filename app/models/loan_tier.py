import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

PRODUCT_SLUGS = ["sme-loans", "mobile-loans", "logbook-loans", "rental-income-loans", "check-off-loans"]

PRODUCT_NAMES = {
    "sme-loans": "SME Loans",
    "mobile-loans": "Mobile Loans",
    "logbook-loans": "Logbook Loans",
    "rental-income-loans": "Rental Income Loans",
    "check-off-loans": "Check Off Loans",
}


class LoanTier(Base):
    """
    The editable "terms" for a loan product plan — rate, fees, amount/term
    bounds. This is the source of truth for both the public Loan Calculator
    and the loan application validator; admin edits here take effect
    everywhere immediately.

    Loan products themselves (name, marketing copy, FAQs) stay static on
    the frontend — only the financial terms are database-backed.
    """

    __tablename__ = "loan_tiers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    product_slug: Mapped[str] = mapped_column(String(60), index=True)
    tier_key: Mapped[str] = mapped_column(String(60), index=True)  # e.g. "hustle-yangu"
    label: Mapped[str] = mapped_column(String(150))

    min_amount: Mapped[float] = mapped_column(Float)
    max_amount: Mapped[float] = mapped_column(Float)
    term_unit: Mapped[str] = mapped_column(String(10))  # "weeks" | "months"
    min_term: Mapped[int] = mapped_column(Integer)
    max_term: Mapped[int] = mapped_column(Integer)
    repayment_frequency: Mapped[str] = mapped_column(String(10))  # "weekly" | "monthly"

    interest_rate: Mapped[float] = mapped_column(Float)
    interest_basis: Mapped[str] = mapped_column(String(20))  # "flat_over_term" | "per_month"

    registration_fee: Mapped[float] = mapped_column(Float, default=0)
    processing_fee_rate: Mapped[float] = mapped_column(Float, default=0)
    life_insurance_fee_rate: Mapped[float] = mapped_column(Float, default=0)
    chattel_fee: Mapped[float] = mapped_column(Float, default=0)
    incharge_fee: Mapped[float] = mapped_column(Float, default=0)
    tracking_fee_per_month: Mapped[float] = mapped_column(Float, default=0)
    excise_duty_on_fees_rate: Mapped[float] = mapped_column(Float, default=0)
    guarantors: Mapped[int | None] = mapped_column(Integer, nullable=True)

    display_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
