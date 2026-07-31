import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.loan_application import LoanApplication
from app.models.loan_tier import PRODUCT_NAMES, LoanTier
from app.schemas.loan_application import (
    LoanApplicationCreate,
    LoanApplicationCreateResponse,
    LoanApplicationRead,
)

logger = logging.getLogger("bidii.loan_applications")

router = APIRouter(prefix="/api/loan-applications", tags=["loan-applications"])


def _get_active_tier(db: Session, product_slug: str, tier_id: str) -> LoanTier:
    tier = (
        db.query(LoanTier)
        .filter(
            LoanTier.product_slug == product_slug,
            LoanTier.tier_key == tier_id,
            LoanTier.is_active.is_(True),
        )
        .first()
    )
    if tier is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown product/plan combination: {product_slug} / {tier_id}",
        )
    return tier


@router.post("", response_model=LoanApplicationCreateResponse, status_code=status.HTTP_201_CREATED)
def submit_loan_application(
    payload: LoanApplicationCreate, db: Session = Depends(get_db)
) -> LoanApplicationCreateResponse:
    """
    Receives a loan application from the frontend's Apply page (the final
    "Submit Application" step), validates amount/term against the live,
    admin-editable LoanTier bounds, persists it, and returns a confirmation.
    """
    tier = _get_active_tier(db, payload.product_slug, payload.tier_id)

    if tier.term_unit != payload.term_unit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Term unit for {tier.label} should be '{tier.term_unit}', got '{payload.term_unit}'",
        )
    if not (tier.min_amount <= payload.amount <= tier.max_amount):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Amount for {tier.label} must be between {tier.min_amount:,.0f} and "
                f"{tier.max_amount:,.0f}, got {payload.amount:,.0f}"
            ),
        )
    if not (tier.min_term <= payload.term_value <= tier.max_term):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Term for {tier.label} must be between {tier.min_term} and "
                f"{tier.max_term} {tier.term_unit}, got {payload.term_value}"
            ),
        )

    record = LoanApplication(
        product_slug=payload.product_slug,
        product_name=PRODUCT_NAMES.get(payload.product_slug, payload.product_slug),
        tier_id=payload.tier_id,
        tier_label=tier.label,
        amount=payload.amount,
        term_value=payload.term_value,
        term_unit=payload.term_unit,
        estimated_installment=payload.estimated_installment,
        full_name=payload.full_name,
        id_number=payload.id_number,
        phone=payload.phone,
        email=payload.email,
        monthly_income=payload.monthly_income,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    logger.info(
        "New loan application from %s <%s> for %s (%s), amount=%.0f term=%s %s",
        record.full_name,
        record.email,
        record.product_name,
        record.tier_label,
        record.amount,
        record.term_value,
        record.term_unit,
    )

    return LoanApplicationCreateResponse(data=LoanApplicationRead.model_validate(record))
