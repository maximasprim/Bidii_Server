import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, Integer, String
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

    status: Mapped[LoanApplicationStatus] = mapped_column(
        Enum(LoanApplicationStatus), default=LoanApplicationStatus.pending
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
