import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.branch import Branch
from app.models.loan_application import LoanApplication
from app.models.loan_tier import PRODUCT_NAMES, LoanTier
from app.schemas.loan_application import (
    LoanApplicationCreate,
    LoanApplicationCreateResponse,
)
from app.services.branch_assignment import assign_branch
from app.services.internal_notifications import notify_branch_of_new_application
from app.services.loan_application_duplicate_check import find_pending_duplicate
from app.services.loan_application_presenter import to_loan_application_read

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

    if payload.county not in COVERED_COUNTIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"We're sorry, we don't currently cover loan applications from {payload.county} yet. "
                f"We're currently serving {', '.join(COVERED_COUNTIES)} counties."
            ),
        )

    existing_pending = find_pending_duplicate(db, id_number=payload.id_number, full_name=payload.full_name)
    if existing_pending is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "You already have a pending loan application with us "
                f"({existing_pending.product_name}, submitted on "
                f"{existing_pending.created_at.strftime('%d %b %Y')}). "
                "Please wait for it to be processed before submitting a new one."
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
        location=payload.location,
        county=payload.county,
    )

    # Every application always gets routed to some real, active branch -
    # see app/services/branch_assignment.py for the exact/AI/fallback
    # strategy. This never raises: a branch-matching problem must never
    # block an applicant's submission from going through.
    branch_id, method = assign_branch(db, payload.location, product_slug=payload.product_slug, county=payload.county)
    record.assigned_branch_id = branch_id
    record.branch_assignment_method = method

    db.add(record)
    db.commit()
    db.refresh(record)

    logger.info(
        "New loan application from %s <%s> for %s (%s), amount=%.0f term=%s %s, location=%r -> branch=%r (%s)",
        record.full_name,
        record.email,
        record.product_name,
        record.tier_label,
        record.amount,
        record.term_value,
        record.term_unit,
        record.location,
        record.assigned_branch_id,
        record.branch_assignment_method,
    )

    if record.assigned_branch_id:
        branch = db.query(Branch).filter(Branch.id == record.assigned_branch_id).first()
        if branch:
            # Never raises - see the function's own docstring.
            notify_branch_of_new_application(db, branch_id=branch.id, branch_name=branch.name, application=record)

    return LoanApplicationCreateResponse(data=to_loan_application_read(db, record))
