from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.news_article import NewsArticle
from app.schemas.news_article import (
    NewsArticleCreate,
    NewsArticleCreateResponse,
    NewsArticleListResponse,
    NewsArticleRead,
    NewsArticleUpdate,
    NewsArticleUpdateResponse,
)
from app.services.auth import get_current_admin
from app.services.pagination import page_meta
from app.services.slugify import slugify

router = APIRouter(prefix="/api/admin/news", tags=["admin-news"], dependencies=[Depends(get_current_admin)])


def _unique_slug(db: Session, base_slug: str, exclude_id: str | None = None) -> str:
    slug = base_slug
    suffix = 2
    while True:
        query = db.query(NewsArticle).filter(NewsArticle.slug == slug)
        if exclude_id:
            query = query.filter(NewsArticle.id != exclude_id)
        if query.first() is None:
            return slug
        slug = f"{base_slug}-{suffix}"
        suffix += 1


@router.get("", response_model=NewsArticleListResponse)
def list_all_articles(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category: str | None = None,
    db: Session = Depends(get_db),
) -> NewsArticleListResponse:
    """Unlike the public endpoint, this includes unpublished (draft) articles."""
    query = db.query(NewsArticle)
    if category:
        query = query.filter(NewsArticle.category == category)
    total = query.count()
    items = (
        query.order_by(NewsArticle.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return NewsArticleListResponse(
        meta=page_meta(page, page_size, total),
        items=[NewsArticleRead.model_validate(i) for i in items],
    )


@router.post("", response_model=NewsArticleCreateResponse, status_code=status.HTTP_201_CREATED)
def create_article(payload: NewsArticleCreate, db: Session = Depends(get_db)) -> NewsArticleCreateResponse:
    base_slug = slugify(payload.slug or payload.title)
    slug = _unique_slug(db, base_slug)

    article = NewsArticle(
        slug=slug,
        title=payload.title,
        category=payload.category,
        excerpt=payload.excerpt,
        body=payload.body,
        is_published=payload.is_published,
    )
    db.add(article)
    db.commit()
    db.refresh(article)
    return NewsArticleCreateResponse(data=NewsArticleRead.model_validate(article))


@router.patch("/{article_id}", response_model=NewsArticleUpdateResponse)
def update_article(
    article_id: str, payload: NewsArticleUpdate, db: Session = Depends(get_db)
) -> NewsArticleUpdateResponse:
    article = db.query(NewsArticle).filter(NewsArticle.id == article_id).first()
    if article is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found.")

    if payload.title is not None:
        article.title = payload.title
    if payload.category is not None:
        article.category = payload.category
    if payload.excerpt is not None:
        article.excerpt = payload.excerpt
    if payload.body is not None:
        article.body = payload.body
    if payload.is_published is not None:
        article.is_published = payload.is_published
    if payload.slug is not None:
        article.slug = _unique_slug(db, slugify(payload.slug), exclude_id=article.id)

    db.commit()
    db.refresh(article)
    return NewsArticleUpdateResponse(data=NewsArticleRead.model_validate(article))


@router.delete("/{article_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_article(article_id: str, db: Session = Depends(get_db)) -> None:
    article = db.query(NewsArticle).filter(NewsArticle.id == article_id).first()
    if article is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found.")
    db.delete(article)
    db.commit()
