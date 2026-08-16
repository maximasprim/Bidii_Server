from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TermUnit = Literal["weeks", "months"]
RepaymentFrequency = Literal["weekly", "monthly"]
InterestBasis = Literal["flat_over_term", "per_month"]


class LoanTierCreate(BaseModel):
    product_slug: str
    tier_key: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=150)
    min_amount: float = Field(gt=0)
    max_amount: float = Field(gt=0)
    term_unit: TermUnit
    min_term: int = Field(gt=0)
    max_term: int = Field(gt=0)
    repayment_frequency: RepaymentFrequency
    interest_rate: float = Field(ge=0)
    interest_basis: InterestBasis
    registration_fee: float = Field(default=0, ge=0)
    processing_fee_rate: float = Field(default=0, ge=0)
    life_insurance_fee_rate: float = Field(default=0, ge=0)
    chattel_fee: float = Field(default=0, ge=0)
    incharge_fee: float = Field(default=0, ge=0)
    tracking_fee_per_month: float = Field(default=0, ge=0)
    excise_duty_on_fees_rate: float = Field(default=0, ge=0)
    guarantors: int | None = Field(default=None, ge=0)
    display_order: int = 0
    is_active: bool = True


class LoanTierUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=150)
    min_amount: float | None = Field(default=None, gt=0)
    max_amount: float | None = Field(default=None, gt=0)
    term_unit: TermUnit | None = None
    min_term: int | None = Field(default=None, gt=0)
    max_term: int | None = Field(default=None, gt=0)
    repayment_frequency: RepaymentFrequency | None = None
    interest_rate: float | None = Field(default=None, ge=0)
    interest_basis: InterestBasis | None = None
    registration_fee: float | None = Field(default=None, ge=0)
    processing_fee_rate: float | None = Field(default=None, ge=0)
    life_insurance_fee_rate: float | None = Field(default=None, ge=0)
    chattel_fee: float | None = Field(default=None, ge=0)
    incharge_fee: float | None = Field(default=None, ge=0)
    tracking_fee_per_month: float | None = Field(default=None, ge=0)
    excise_duty_on_fees_rate: float | None = Field(default=None, ge=0)
    guarantors: int | None = Field(default=None, ge=0)
    display_order: int | None = None
    is_active: bool | None = None


class LoanTierRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_slug: str
    tier_key: str
    label: str
    min_amount: float
    max_amount: float
    term_unit: str
    min_term: int
    max_term: int
    repayment_frequency: str
    interest_rate: float
    interest_basis: str
    registration_fee: float
    processing_fee_rate: float
    life_insurance_fee_rate: float
    chattel_fee: float
    incharge_fee: float
    tracking_fee_per_month: float
    excise_duty_on_fees_rate: float
    guarantors: int | None
    display_order: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class LoanTierPublicRead(BaseModel):
    """
    What the public Loan Calculator/Apply flow gets — enough to compute
    and display the repayment schedule (amount/term bounds, rate, and the
    tracking fee, which is itself an editable public input on logbook
    products) without exposing internal fee rates/amounts (processing fee
    %, life insurance fee %, chattel/incharge fees, excise duty rate,
    guarantor counts). Those stay restricted to admins and loan officers —
    see LoanTierRead / GET /api/loan-tiers/internal.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    product_slug: str
    tier_key: str
    label: str
    min_amount: float
    max_amount: float
    term_unit: str
    min_term: int
    max_term: int
    repayment_frequency: str
    interest_rate: float
    interest_basis: str
    tracking_fee_per_month: float
    display_order: int
    is_active: bool


class LoanTierCreateResponse(BaseModel):
    success: bool = True
    message: str = "Loan tier created."
    data: LoanTierRead


class LoanTierUpdateResponse(BaseModel):
    success: bool = True
    message: str = "Loan tier updated."
    data: LoanTierRead


class LoanTierListResponse(BaseModel):
    items: list[LoanTierRead]


class LoanTierPublicListResponse(BaseModel):
    items: list[LoanTierPublicRead]
    