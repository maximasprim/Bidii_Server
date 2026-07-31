from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.loan_tier import PRODUCT_SLUGS, LoanTier
from app.schemas.loan_tier import (
    LoanTierCreate,
    LoanTierCreateResponse,
    LoanTierListResponse,
    LoanTierRead,
    LoanTierUpdate,
    LoanTierUpdateResponse,
)
from app.services.auth import get_current_admin

router = APIRouter(prefix="/api/admin/loan-tiers", tags=["admin-loan-tiers"], dependencies=[Depends(get_current_admin)])


@router.get("", response_model=LoanTierListResponse)
def list_all_loan_tiers(product_slug: str | None = None, db: Session = Depends(get_db)) -> LoanTierListResponse:
    """Includes inactive tiers, for the admin loan-terms configuration page."""
    query = db.query(LoanTier)
    if product_slug:
        query = query.filter(LoanTier.product_slug == product_slug)
    tiers = query.order_by(LoanTier.product_slug, LoanTier.display_order).all()
    return LoanTierListResponse(items=[LoanTierRead.model_validate(t) for t in tiers])


@router.post("", response_model=LoanTierCreateResponse, status_code=status.HTTP_201_CREATED)
def create_loan_tier(payload: LoanTierCreate, db: Session = Depends(get_db)) -> LoanTierCreateResponse:
    if payload.product_slug not in PRODUCT_SLUGS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"product_slug must be one of: {', '.join(PRODUCT_SLUGS)}",
        )
    if payload.min_amount > payload.max_amount:
        raise HTTPException(status_code=422, detail="min_amount can't be greater than max_amount.")
    if payload.min_term > payload.max_term:
        raise HTTPException(status_code=422, detail="min_term can't be greater than max_term.")

    existing = (
        db.query(LoanTier)
        .filter(LoanTier.product_slug == payload.product_slug, LoanTier.tier_key == payload.tier_key)
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'A tier with key "{payload.tier_key}" already exists for {payload.product_slug}.',
        )

    tier = LoanTier(**payload.model_dump())
    db.add(tier)
    db.commit()
    db.refresh(tier)
    return LoanTierCreateResponse(data=LoanTierRead.model_validate(tier))


@router.patch("/{tier_id}", response_model=LoanTierUpdateResponse)
def update_loan_tier(tier_id: str, payload: LoanTierUpdate, db: Session = Depends(get_db)) -> LoanTierUpdateResponse:
    tier = db.query(LoanTier).filter(LoanTier.id == tier_id).first()
    if tier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan tier not found.")

    updates = payload.model_dump(exclude_unset=True)
    new_min = updates.get("min_amount", tier.min_amount)
    new_max = updates.get("max_amount", tier.max_amount)
    if new_min > new_max:
        raise HTTPException(status_code=422, detail="min_amount can't be greater than max_amount.")
    new_min_term = updates.get("min_term", tier.min_term)
    new_max_term = updates.get("max_term", tier.max_term)
    if new_min_term > new_max_term:
        raise HTTPException(status_code=422, detail="min_term can't be greater than max_term.")

    for field, value in updates.items():
        setattr(tier, field, value)

    db.commit()
    db.refresh(tier)
    return LoanTierUpdateResponse(data=LoanTierRead.model_validate(tier))


@router.delete("/{tier_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_loan_tier(tier_id: str, db: Session = Depends(get_db)) -> None:
    """
    Hard delete is safe here — LoanApplication stores its own denormalized
    copy of the tier's label/id/terms at submission time rather than a
    foreign key, so historical applications are unaffected. Prefer setting
    is_active=False instead if the intent is just to stop offering a tier
    while keeping it around for reference.
    """
    tier = db.query(LoanTier).filter(LoanTier.id == tier_id).first()
    if tier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan tier not found.")
    db.delete(tier)
    db.commit()
