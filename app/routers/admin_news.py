import logging
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.news_article import NewsArticle
from app.schemas.news_article import (
    NewsArticleCreate,
    NewsArticleCreateResponse,
    NewsArticleListResponse,
    NewsArticleRead,
    NewsArticleUpdate,
    NewsArticleUpdateResponse,
    NewsImageUploadResponse,
)
from app.services.auth import get_current_admin
from app.services.pagination import page_meta
from app.services.slugify import slugify
from app.services.storage import BUCKET, supabase

logger = logging.getLogger("bidii.admin_news")
settings = get_settings()

_ALLOWED_IMAGE_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}

router = APIRouter(prefix="/api/admin/news", tags=["admin-news"], dependencies=[Depends(get_current_admin)])


def _safe_filename(original: str) -> str:
    name = Path(original).name  # drops any directory components
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name or "image"


@router.post("/upload-image", response_model=NewsImageUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_article_image(image: UploadFile = File(...)) -> NewsImageUploadResponse:
    """
    Uploads one image for use in a news article's image_urls list. Returns
    a path (not a raw Supabase URL) that the frontend requests back through
    GET /api/news/images/{filename} - this backend proxies image bytes the
    same way it already proxies career-application CV downloads, so this
    doesn't depend on the Supabase bucket being publicly readable.
    """
    if image.content_type not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=422, detail="Image must be a JPEG, PNG, WEBP, or GIF file.")

    contents = await image.read()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(contents) > max_bytes:
        raise HTTPException(
            status_code=422,
            detail=f"Image is too large. Maximum size is {settings.max_upload_size_mb}MB.",
        )
    if len(contents) == 0:
        raise HTTPException(status_code=422, detail="The uploaded image is empty.")

    original_name = _safe_filename(image.filename or f"image.{_ALLOWED_IMAGE_TYPES[image.content_type]}")
    stored_name = f"{uuid.uuid4()}_{original_name}"
    storage_path = f"news/{stored_name}"

    try:
        supabase.storage.from_(BUCKET).upload(
            storage_path,
            contents,
            {"content-type": image.content_type, "upsert": False},
        )
        logger.info("News image uploaded to Supabase Storage: %s", storage_path)
    except Exception as exc:
        logger.exception("Failed to upload news image to Supabase Storage")
        raise HTTPException(status_code=500, detail="Failed to store image. Please try again.") from exc

    return NewsImageUploadResponse(url=storage_path)


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
        image_urls=payload.image_urls,
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
    if payload.image_urls is not None:
        article.image_urls = payload.image_urls
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


# from fastapi import APIRouter, Depends, HTTPException, Query, status
# from sqlalchemy.orm import Session

# from app.database import get_db
# from app.models.news_article import NewsArticle
# from app.schemas.news_article import (
#     NewsArticleCreate,
#     NewsArticleCreateResponse,
#     NewsArticleListResponse,
#     NewsArticleRead,
#     NewsArticleUpdate,
#     NewsArticleUpdateResponse,
# )
# from app.services.auth import get_current_admin
# from app.services.pagination import page_meta
# from app.services.slugify import slugify

# router = APIRouter(prefix="/api/admin/news", tags=["admin-news"], dependencies=[Depends(get_current_admin)])


# def _unique_slug(db: Session, base_slug: str, exclude_id: str | None = None) -> str:
#     slug = base_slug
#     suffix = 2
#     while True:
#         query = db.query(NewsArticle).filter(NewsArticle.slug == slug)
#         if exclude_id:
#             query = query.filter(NewsArticle.id != exclude_id)
#         if query.first() is None:
#             return slug
#         slug = f"{base_slug}-{suffix}"
#         suffix += 1


# @router.get("", response_model=NewsArticleListResponse)
# def list_all_articles(
#     page: int = Query(1, ge=1),
#     page_size: int = Query(20, ge=1, le=100),
#     category: str | None = None,
#     db: Session = Depends(get_db),
# ) -> NewsArticleListResponse:
#     """Unlike the public endpoint, this includes unpublished (draft) articles."""
#     query = db.query(NewsArticle)
#     if category:
#         query = query.filter(NewsArticle.category == category)
#     total = query.count()
#     items = (
#         query.order_by(NewsArticle.created_at.desc())
#         .offset((page - 1) * page_size)
#         .limit(page_size)
#         .all()
#     )
#     return NewsArticleListResponse(
#         meta=page_meta(page, page_size, total),
#         items=[NewsArticleRead.model_validate(i) for i in items],
#     )


# @router.post("", response_model=NewsArticleCreateResponse, status_code=status.HTTP_201_CREATED)
# def create_article(payload: NewsArticleCreate, db: Session = Depends(get_db)) -> NewsArticleCreateResponse:
#     base_slug = slugify(payload.slug or payload.title)
#     slug = _unique_slug(db, base_slug)

#     article = NewsArticle(
#         slug=slug,
#         title=payload.title,
#         category=payload.category,
#         excerpt=payload.excerpt,
#         body=payload.body,
#         is_published=payload.is_published,
#     )
#     db.add(article)
#     db.commit()
#     db.refresh(article)
#     return NewsArticleCreateResponse(data=NewsArticleRead.model_validate(article))


# @router.patch("/{article_id}", response_model=NewsArticleUpdateResponse)
# def update_article(
#     article_id: str, payload: NewsArticleUpdate, db: Session = Depends(get_db)
# ) -> NewsArticleUpdateResponse:
#     article = db.query(NewsArticle).filter(NewsArticle.id == article_id).first()
#     if article is None:
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found.")

#     if payload.title is not None:
#         article.title = payload.title
#     if payload.category is not None:
#         article.category = payload.category
#     if payload.excerpt is not None:
#         article.excerpt = payload.excerpt
#     if payload.body is not None:
#         article.body = payload.body
#     if payload.is_published is not None:
#         article.is_published = payload.is_published
#     if payload.slug is not None:
#         article.slug = _unique_slug(db, slugify(payload.slug), exclude_id=article.id)

#     db.commit()
#     db.refresh(article)
#     return NewsArticleUpdateResponse(data=NewsArticleRead.model_validate(article))


# @router.delete("/{article_id}", status_code=status.HTTP_204_NO_CONTENT)
# def delete_article(article_id: str, db: Session = Depends(get_db)) -> None:
#     article = db.query(NewsArticle).filter(NewsArticle.id == article_id).first()
#     if article is None:
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found.")
#     db.delete(article)
#     db.commit()
