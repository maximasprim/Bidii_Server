import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ContactSubject(str, enum.Enum):
    loan_inquiry = "loan-inquiry"
    existing_loan = "existing-loan"
    complaint = "complaint"
    other = "other"


class ContactMessage(Base):
    __tablename__ = "contact_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), index=True)
    phone: Mapped[str] = mapped_column(String(40))
    subject: Mapped[ContactSubject] = mapped_column(Enum(ContactSubject))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
