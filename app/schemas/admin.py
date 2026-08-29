from pydantic import BaseModel

from app.schemas.career_application import CareerApplicationRead
from app.schemas.contact import ContactRead
from app.schemas.loan_application import LoanApplicationRead


class PageMeta(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int


class PaginatedContacts(BaseModel):
    meta: PageMeta
    items: list[ContactRead]


class PaginatedLoanApplications(BaseModel):
    meta: PageMeta
    items: list[LoanApplicationRead]


class PaginatedCareerApplications(BaseModel):
    meta: PageMeta
    items: list[CareerApplicationRead]


class StatusUpdate(BaseModel):
    status: str


class LoanApplicationAssignRequest(BaseModel):
    """Both fields optional - supply one, the other, or both in one call."""

    assigned_branch_id: str | None = None
    assigned_loan_officer_id: str | None = None
    

class ProductBreakdown(BaseModel):
    product_slug: str
    product_name: str
    count: int
    total_amount_requested: float


class DashboardStats(BaseModel):
    total_contacts: int
    total_loan_applications: int
    total_career_applications: int

    loan_applications_by_status: dict[str, int]
    career_applications_by_status: dict[str, int]

    loan_applications_by_product: list[ProductBreakdown]

    contacts_last_7_days: int
    loan_applications_last_7_days: int
    career_applications_last_7_days: int

    total_amount_requested_all_time: float
