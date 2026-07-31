from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["Financial Literacy", "Product Updates", "Company News", "Customer Stories"]


class NewsArticleCreate(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    category: Category
    excerpt: str = Field(min_length=10, max_length=500)
    body: list[str] = Field(min_length=1)
    is_published: bool = True
    # Optional — auto-generated from the title if not provided.
    slug: str | None = Field(default=None, max_length=200)


class NewsArticleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=300)
    category: Category | None = None
    excerpt: str | None = Field(default=None, min_length=10, max_length=500)
    body: list[str] | None = Field(default=None, min_length=1)
    is_published: bool | None = None
    slug: str | None = Field(default=None, max_length=200)


class NewsArticleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    slug: str
    title: str
    category: str
    excerpt: str
    body: list[str]
    is_published: bool
    published_at: datetime
    created_at: datetime
    updated_at: datetime


class NewsArticleCreateResponse(BaseModel):
    success: bool = True
    message: str = "Article created."
    data: NewsArticleRead


class NewsArticleUpdateResponse(BaseModel):
    success: bool = True
    message: str = "Article updated."
    data: NewsArticleRead


class NewsArticleListResponse(BaseModel):
    meta: dict
    items: list[NewsArticleRead]
