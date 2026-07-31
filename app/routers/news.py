from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.news_article import NewsArticle
from app.schemas.news_article import NewsArticleListResponse, NewsArticleRead
from app.services.pagination import page_meta

router = APIRouter(prefix="/api/news", tags=["news"])


@router.get("", response_model=NewsArticleListResponse)
def list_published_articles(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category: str | None = None,
    db: Session = Depends(get_db),
) -> NewsArticleListResponse:
    query = db.query(NewsArticle).filter(NewsArticle.is_published.is_(True))
    if category:
        query = query.filter(NewsArticle.category == category)
    total = query.count()
    items = (
        query.order_by(NewsArticle.published_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return NewsArticleListResponse(
        meta=page_meta(page, page_size, total),
        items=[NewsArticleRead.model_validate(i) for i in items],
    )


@router.get("/{slug}", response_model=NewsArticleRead)
def get_article_by_slug(slug: str, db: Session = Depends(get_db)) -> NewsArticleRead:
    article = (
        db.query(NewsArticle)
        .filter(NewsArticle.slug == slug, NewsArticle.is_published.is_(True))
        .first()
    )
    if article is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found.")
    return NewsArticleRead.model_validate(article)
