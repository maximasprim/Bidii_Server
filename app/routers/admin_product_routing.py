import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.loan_tier import PRODUCT_NAMES
from app.models.product_routing import ROUTABLE_PRODUCT_SLUGS, SUGGESTED_ROLE_FOR_PRODUCT, ProductRoutingAssignment
from app.schemas.product_routing import (
    ProductRoutingListResponse,
    ProductRoutingRead,
    ProductRoutingUpdate,
    ProductRoutingUpdateResponse,
)
from app.services.auth import require_roles

logger = logging.getLogger("bidii.product_routing_admin")

router = APIRouter(prefix="/api/admin/product-routing", tags=["admin-product-routing"])


def _to_read(product_slug: str, row: ProductRoutingAssignment | None, admin: AdminUser | None) -> ProductRoutingRead:
    return ProductRoutingRead(
        product_slug=product_slug,
        product_name=PRODUCT_NAMES.get(product_slug, product_slug),
        suggested_role=SUGGESTED_ROLE_FOR_PRODUCT.get(product_slug),
        assigned_admin_id=row.assigned_admin_id if row else None,
        assigned_admin_username=admin.username if admin else None,
        assigned_admin_email=admin.email if admin else None,
    )


@router.get("", response_model=ProductRoutingListResponse, dependencies=[Depends(require_roles("admin"))])
def list_product_routing(db: Session = Depends(get_db)) -> ProductRoutingListResponse:
    """
    Admin-only. Always returns exactly one row per ROUTABLE_PRODUCT_SLUGS,
    even for a product nobody has configured yet (assigned_admin_id null),
    so the Loan Routing admin page always has a complete, stable set of
    rows to render.
    """
    rows = {
        row.product_slug: row
        for row in db.query(ProductRoutingAssignment)
        .filter(ProductRoutingAssignment.product_slug.in_(ROUTABLE_PRODUCT_SLUGS))
        .all()
    }
    admin_ids = {row.assigned_admin_id for row in rows.values() if row.assigned_admin_id}
    admins = {a.id: a for a in db.query(AdminUser).filter(AdminUser.id.in_(admin_ids)).all()} if admin_ids else {}

    items = [
        _to_read(slug, rows.get(slug), admins.get(rows[slug].assigned_admin_id) if slug in rows and rows[slug].assigned_admin_id else None)
        for slug in ROUTABLE_PRODUCT_SLUGS
    ]
    return ProductRoutingListResponse(items=items)


@router.put(
    "/{product_slug}",
    response_model=ProductRoutingUpdateResponse,
    dependencies=[Depends(require_roles("admin"))],
)
def update_product_routing(
    product_slug: str, payload: ProductRoutingUpdate, db: Session = Depends(get_db)
) -> ProductRoutingUpdateResponse:
    """
    Admin-only. Setting assigned_admin_id routes every *new* application
    for this product straight to that admin (see
    app/services/product_routing.py) - applications already submitted
    are never touched retroactively. Setting it to null reverts the
    product to normal branch-based routing.
    """
    if product_slug not in ROUTABLE_PRODUCT_SLUGS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'"{product_slug}" isn\'t a product that supports individual routing.',
        )

    admin: AdminUser | None = None
    if payload.assigned_admin_id is not None:
        admin = db.query(AdminUser).filter(AdminUser.id == payload.assigned_admin_id).first()
        if admin is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No admin user with that id.")
        if not admin.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That admin account is deactivated and can't be routed applications.",
            )

    row = db.query(ProductRoutingAssignment).filter(ProductRoutingAssignment.product_slug == product_slug).first()
    if row is None:
        row = ProductRoutingAssignment(product_slug=product_slug)
        db.add(row)
    row.assigned_admin_id = payload.assigned_admin_id
    db.commit()
    db.refresh(row)

    logger.info("Product %r routing set to admin_id=%r", product_slug, payload.assigned_admin_id)
    return ProductRoutingUpdateResponse(data=_to_read(product_slug, row, admin))
