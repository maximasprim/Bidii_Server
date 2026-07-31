from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.loan_application import LoanApplicationStatus


class LoanApplicationCreate(BaseModel):
    """
    Mirrors the frontend's Apply page (src/pages/Apply.tsx):

        Step 1 (loan details): product, plan/tier, amount, term
        Step 2 (your information): fullName, idNumber, phone, email, monthlyIncome

    amount/term/product_slug/tier_id are re-validated in the router against
    the live, admin-editable LoanTier table (not here in the schema, since
    that validation needs a DB session) — client-side slider constraints
    are a UX nicety, not a security boundary, so the backend checks
    independently regardless of what the frontend sent.
    """

    product_slug: str
    tier_id: str
    amount: float = Field(gt=0)
    term_value: int = Field(gt=0)
    term_unit: Literal["weeks", "months"]
    estimated_installment: float = Field(ge=0)

    full_name: str = Field(min_length=2, max_length=200)
    id_number: str = Field(min_length=6, max_length=20)
    phone: str = Field(min_length=10, max_length=40)
    email: EmailStr
    monthly_income: str = Field(min_length=1, max_length=100)


class LoanApplicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_slug: str
    product_name: str
    tier_id: str
    tier_label: str
    amount: float
    term_value: int
    term_unit: str
    estimated_installment: float
    full_name: str
    id_number: str
    phone: str
    email: EmailStr
    monthly_income: str
    status: LoanApplicationStatus
    created_at: datetime


class LoanApplicationCreateResponse(BaseModel):
    success: bool = True
    message: str = "Application received. A loan officer will contact you within 24 hours to verify your details."
    data: LoanApplicationRead
