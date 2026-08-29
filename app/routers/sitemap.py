"""
Serves a dynamically-generated sitemap.xml - replaces the old static file
that lived at bidii-credit-main/public/sitemap.xml. That file went stale
the moment News became a database-backed, admin-managed CMS: nothing kept
its hardcoded article URLs in sync with what admins actually published.

This endpoint has no such problem for news, since it queries NewsArticle
directly on every request. It's plain text/XML generation with no
external dependencies (no templating library needed).

STATIC_ROUTES and PRODUCT_SLUGS below are the two lists that still can't
be derived from the database, because that content genuinely lives in
the frontend's static data, not this backend:
- STATIC_ROUTES: pages that aren't backed by any model (About, Services,
  Calculator, etc.) - see bidii-credit-main/src/App.tsx for the full
  route list this mirrors.
- PRODUCT_SLUGS: the loan products shown on /products and
  /products/<slug> - see bidii-credit-main/src/data/content.ts's
  `loanProducts` array. If a product is ever added, renamed, or removed
  there, update the list below to match - this is now the ONLY other
  place that needs updating (previously the static sitemap.xml also had
  to be hand-edited in parallel and the two drifted apart, which is
  exactly what caused the mismatch this replaces).

No auth on this route - a sitemap is public by definition, same as
robots.txt.
"""

from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.news_article import NewsArticle

router = APIRouter(tags=["sitemap"])

# (path, changefreq, priority) - mirrors bidii-credit-main/src/App.tsx's
# public routes. Keep in sync if a page is added/removed there.
STATIC_ROUTES = [
    ("/", "weekly", "1.0"),
    ("/about", "monthly", "0.6"),
    ("/services", "monthly", "0.6"),
    ("/products", "weekly", "0.9"),
    ("/calculator", "monthly", "0.7"),
    ("/branches", "monthly", "0.7"),
    ("/downloads", "monthly", "0.5"),
    ("/news", "weekly", "0.6"),
    ("/careers", "weekly", "0.5"),
    ("/contact", "yearly", "0.6"),
    ("/apply", "monthly", "0.9"),
    ("/faq", "monthly", "0.5"),
]

# Keep in sync with the `slug` field of every entry in
# bidii-credit-main/src/data/content.ts's `loanProducts` array.
PRODUCT_SLUGS = ["sme-loans", "mobile-loans", "logbook-loans", "rental-income-loans", "check-off-loans"]


def _url_entry(loc: str, *, changefreq: str, priority: str, lastmod: str | None = None) -> str:
    lastmod_tag = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
    return f"<url><loc>{escape(loc)}</loc>{lastmod_tag}<changefreq>{changefreq}</changefreq><priority>{priority}</priority></url>"


@router.get("/sitemap.xml")
def get_sitemap(db: Session = Depends(get_db)) -> Response:
    settings = get_settings()
    base = settings.site_url.rstrip("/")

    entries = [_url_entry(f"{base}{path}", changefreq=freq, priority=pri) for path, freq, pri in STATIC_ROUTES]
    entries += [_url_entry(f"{base}/products/{slug}", changefreq="monthly", priority="0.8") for slug in PRODUCT_SLUGS]

    articles = db.query(NewsArticle).filter(NewsArticle.is_published.is_(True)).all()
    entries += [
        _url_entry(
            f"{base}/news/{article.slug}",
            changefreq="yearly",
            priority="0.4",
            lastmod=article.updated_at.date().isoformat(),
        )
        for article in articles
    ]

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n  '
        + "\n  ".join(entries)
        + "\n</urlset>\n"
    )
    return Response(content=xml, media_type="application/xml")
