import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

CATEGORIES = ["Financial Literacy", "Product Updates", "Company News", "Customer Stories"]


class NewsArticle(Base):
    __tablename__ = "news_articles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(60))
    excerpt: Mapped[str] = mapped_column(Text)
    # List of paragraph strings — matches the frontend's Article.body: string[].
    body: Mapped[list] = mapped_column(JSON)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
