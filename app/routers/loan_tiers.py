from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.loan_tier import LoanTier
from app.schemas.loan_tier import LoanTierListResponse, LoanTierRead

router = APIRouter(prefix="/api/loan-tiers", tags=["loan-tiers"])


@router.get("", response_model=LoanTierListResponse)
def list_active_loan_tiers(product_slug: str | None = None, db: Session = Depends(get_db)) -> LoanTierListResponse:
    """
    Powers the public Loan Calculator and Apply flow — the frontend groups
    this flat list by product_slug. Admin edits to rates/fees/bounds here
    take effect immediately, no frontend deploy needed.
    """
    query = db.query(LoanTier).filter(LoanTier.is_active.is_(True))
    if product_slug:
        query = query.filter(LoanTier.product_slug == product_slug)
    tiers = query.order_by(LoanTier.product_slug, LoanTier.display_order).all()
    return LoanTierListResponse(items=[LoanTierRead.model_validate(t) for t in tiers])
