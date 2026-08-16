import logging
import mimetypes

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.news_article import NewsArticle
from app.schemas.news_article import NewsArticleListResponse, NewsArticleRead
from app.services.pagination import page_meta
from app.services.storage import BUCKET, supabase

logger = logging.getLogger("bidii.news")

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


@router.get("/images/{path:path}")
def get_article_image(path: str):
    """
    Publicly proxies an image previously uploaded via
    POST /api/admin/news/upload-image (path is the value stored in an
    article's image_urls, e.g. "news/<uuid>_cover.jpg"). Proxying through
    the backend — rather than linking straight to Supabase Storage — means
    news images work regardless of the storage bucket's public-read
    setting, the same pattern already used for career-application CV
    downloads.
    """
    if not path.startswith("news/"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found.")

    try:
        file_data = supabase.storage.from_(BUCKET).download(path)
    except Exception:
        logger.exception("Failed to download news image from Supabase Storage: %s", path)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found.")

    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return Response(content=file_data, media_type=content_type)


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

# from fastapi import APIRouter, Depends, HTTPException, Query, status
# from sqlalchemy.orm import Session

# from app.database import get_db
# from app.models.news_article import NewsArticle
# from app.schemas.news_article import NewsArticleListResponse, NewsArticleRead
# from app.services.pagination import page_meta

# router = APIRouter(prefix="/api/news", tags=["news"])


# @router.get("", response_model=NewsArticleListResponse)
# def list_published_articles(
#     page: int = Query(1, ge=1),
#     page_size: int = Query(20, ge=1, le=100),
#     category: str | None = None,
#     db: Session = Depends(get_db),
# ) -> NewsArticleListResponse:
#     query = db.query(NewsArticle).filter(NewsArticle.is_published.is_(True))
#     if category:
#         query = query.filter(NewsArticle.category == category)
#     total = query.count()
#     items = (
#         query.order_by(NewsArticle.published_at.desc())
#         .offset((page - 1) * page_size)
#         .limit(page_size)
#         .all()
#     )
#     return NewsArticleListResponse(
#         meta=page_meta(page, page_size, total),
#         items=[NewsArticleRead.model_validate(i) for i in items],
#     )


# @router.get("/{slug}", response_model=NewsArticleRead)
# def get_article_by_slug(slug: str, db: Session = Depends(get_db)) -> NewsArticleRead:
#     article = (
#         db.query(NewsArticle)
#         .filter(NewsArticle.slug == slug, NewsArticle.is_published.is_(True))
#         .first()
#     )
#     if article is None:
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found.")
#     return NewsArticleRead.model_validate(article)
