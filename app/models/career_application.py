import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CareerApplicationStatus(str, enum.Enum):
    received = "received"
    reviewing = "reviewing"
    shortlisted = "shortlisted"
    rejected = "rejected"
    hired = "hired"


class CareerApplication(Base):
    __tablename__ = "career_applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Nullable — "General application" has no specific posting. Kept as a
    # loose reference (not enforced with ON DELETE CASCADE) since `role`
    # below is what actually gets displayed; if the posting is later edited
    # or removed, the application still shows what the applicant applied
    # for at the time.
    job_id: Mapped[str | None] = mapped_column(ForeignKey("job_openings.id"), nullable=True, index=True)

    full_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), index=True)
    phone: Mapped[str] = mapped_column(String(40))
    role: Mapped[str] = mapped_column(String(150))
    cover_note: Mapped[str] = mapped_column(Text)

    # The original filename (for display) vs. the sanitized/uuid-prefixed
    # name it's actually stored under on disk (to avoid collisions and path
    # traversal from a hostile filename).
    cv_original_filename: Mapped[str] = mapped_column(String(255))
    cv_stored_filename: Mapped[str] = mapped_column(String(255))

    status: Mapped[CareerApplicationStatus] = mapped_column(
        Enum(CareerApplicationStatus), default=CareerApplicationStatus.received
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
