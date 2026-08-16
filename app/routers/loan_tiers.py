from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.loan_tier import LoanTier
from app.schemas.loan_tier import (
    LoanTierListResponse,
    LoanTierPublicListResponse,
    LoanTierPublicRead,
    LoanTierRead,
)
from app.services.auth import require_roles

router = APIRouter(prefix="/api/loan-tiers", tags=["loan-tiers"])


def _active_tiers_query(db: Session, product_slug: str | None):
    query = db.query(LoanTier).filter(LoanTier.is_active.is_(True))
    if product_slug:
        query = query.filter(LoanTier.product_slug == product_slug)
    return query.order_by(LoanTier.product_slug, LoanTier.display_order).all()


@router.get("", response_model=LoanTierPublicListResponse)
def list_active_loan_tiers(
    product_slug: str | None = None, db: Session = Depends(get_db)
) -> LoanTierPublicListResponse:
    """
    Powers the public Loan Calculator and Apply flow — the frontend groups
    this flat list by product_slug. Admin edits to rates/fees/bounds here
    take effect immediately, no frontend deploy needed.

    Returns only the fields the general public needs (amount/term bounds,
    rate, tracking fee) — internal fee rates/amounts are left out here and
    only served via GET /api/loan-tiers/internal to logged-in admins/loan
    officers. See LoanTierPublicRead for the exact field list.
    """
    tiers = _active_tiers_query(db, product_slug)
    return LoanTierPublicListResponse(items=[LoanTierPublicRead.model_validate(t) for t in tiers])


@router.get(
    "/internal",
    response_model=LoanTierListResponse,
    dependencies=[Depends(require_roles("admin", "loan_officer"))],
)
def list_active_loan_tiers_internal(
    product_slug: str | None = None, db: Session = Depends(get_db)
) -> LoanTierListResponse:
    """
    Same active-tier set as the public endpoint above, but with every
    field — including internal fee rates/amounts and guarantor counts.
    Restricted to logged-in admins and loan officers; the public Loan
    Calculator calls this in addition to the public endpoint when the
    visitor is signed in with one of those roles, to show the full
    "Fees & charges" breakdown that the general public doesn't see.
    """
    tiers = _active_tiers_query(db, product_slug)
    return LoanTierListResponse(items=[LoanTierRead.model_validate(t) for t in tiers])


# from fastapi import APIRouter, Depends
# from sqlalchemy.orm import Session

# from app.database import get_db
# from app.models.loan_tier import LoanTier
# from app.schemas.loan_tier import LoanTierListResponse, LoanTierRead

# router = APIRouter(prefix="/api/loan-tiers", tags=["loan-tiers"])


# @router.get("", response_model=LoanTierListResponse)
# def list_active_loan_tiers(product_slug: str | None = None, db: Session = Depends(get_db)) -> LoanTierListResponse:
#     """
#     Powers the public Loan Calculator and Apply flow — the frontend groups
#     this flat list by product_slug. Admin edits to rates/fees/bounds here
#     take effect immediately, no frontend deploy needed.
#     """
#     query = db.query(LoanTier).filter(LoanTier.is_active.is_(True))
#     if product_slug:
#         query = query.filter(LoanTier.product_slug == product_slug)
#     tiers = query.order_by(LoanTier.product_slug, LoanTier.display_order).all()
#     return LoanTierListResponse(items=[LoanTierRead.model_validate(t) for t in tiers])
