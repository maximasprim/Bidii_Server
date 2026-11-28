import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.loan_tier import PRODUCT_NAMES
from app.models.product_routing import (
    ROUTABLE_PRODUCT_SLUGS,
    SUGGESTED_ROLE_FOR_PRODUCT,
    ProductRoutingAgent,
    ProductRoutingAssignment,
)
from app.schemas.product_routing import (
    ProductRoutingListResponse,
    ProductRoutingRead,
    RoutedAgentRead,
    ProductRoutingUpdate,
    ProductRoutingUpdateResponse,
)
from app.services.auth import require_roles
from app.services.product_routing import get_routing_agent_ids

logger = logging.getLogger("bidii.product_routing_admin")

router = APIRouter(prefix="/api/admin/product-routing", tags=["admin-product-routing"])


def _to_read(product_slug: str, agents: list[AdminUser]) -> ProductRoutingRead:
    """`agents` = every agent the product is routed to, in order. The single-agent
    fields (assigned_admin_*) describe the first one, exactly as before."""
    first = agents[0] if agents else None
    return ProductRoutingRead(
        product_slug=product_slug,
        product_name=PRODUCT_NAMES.get(product_slug, product_slug),
        suggested_role=SUGGESTED_ROLE_FOR_PRODUCT.get(product_slug),
        assigned_admin_id=first.id if first else None,
        assigned_admin_username=first.username if first else None,
        assigned_admin_email=first.email if first else None,
        assigned_admins=[
            RoutedAgentRead(id=a.id, username=a.username, email=a.email, is_active=a.is_active) for a in agents
        ],
    )


def _agents_for(db: Session, product_slug: str) -> list[AdminUser]:
    ids = get_routing_agent_ids(db, product_slug)
    if not ids:
        return []
    by_id = {a.id: a for a in db.query(AdminUser).filter(AdminUser.id.in_(ids)).all()}
    return [by_id[i] for i in ids if i in by_id]


@router.get("", response_model=ProductRoutingListResponse, dependencies=[Depends(require_roles("admin"))])
def list_product_routing(db: Session = Depends(get_db)) -> ProductRoutingListResponse:
    """
    Admin-only. Always returns exactly one row per ROUTABLE_PRODUCT_SLUGS,
    even for a product nobody has configured yet (assigned_admin_id null),
    so the Loan Routing admin page always has a complete, stable set of
    rows to render.
    """
    items = [_to_read(slug, _agents_for(db, slug)) for slug in ROUTABLE_PRODUCT_SLUGS]
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

    # Multi-agent form (assigned_admin_ids) wins when sent; otherwise the original single
    # assigned_admin_id form, where null clears. Duplicates are dropped, order is kept.
    if payload.assigned_admin_ids is not None:
        requested = list(dict.fromkeys(payload.assigned_admin_ids))
    else:
        requested = [payload.assigned_admin_id] if payload.assigned_admin_id is not None else []

    admins: list[AdminUser] = []
    for admin_id in requested:
        admin = db.query(AdminUser).filter(AdminUser.id == admin_id).first()
        if admin is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No admin user with that id.")
        if not admin.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{admin.username}'s admin account is deactivated and can't be routed applications.",
            )
        admins.append(admin)

    row = db.query(ProductRoutingAssignment).filter(ProductRoutingAssignment.product_slug == product_slug).first()
    if row is None:
        row = ProductRoutingAssignment(product_slug=product_slug)
        db.add(row)

    # Keep agents already on the product (so their turn history/order is stable), drop the
    # ones no longer picked, add the new ones.
    existing = {r.admin_id: r for r in db.query(ProductRoutingAgent).filter(ProductRoutingAgent.product_slug == product_slug).all()}
    wanted = {a.id for a in admins}
    for admin_id, agent_row in existing.items():
        if admin_id not in wanted:
            db.delete(agent_row)
    for admin in admins:
        if admin.id not in existing:
            db.add(ProductRoutingAgent(product_slug=product_slug, admin_id=admin.id))
    # The legacy column keeps pointing at the first agent (null when none), so anything
    # still reading it - or a rollback of multi-agent routing - sees a valid single agent.
    row.assigned_admin_id = admins[0].id if admins else None
    db.commit()

    logger.info("Product %r routing set to admin_ids=%r", product_slug, [a.id for a in admins])
    return ProductRoutingUpdateResponse(data=_to_read(product_slug, _agents_for(db, product_slug)))
