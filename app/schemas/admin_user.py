from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

AdminRole = Literal[
    "admin",
    "loan_officer",
    "hr",
    "marketing_manager",
    "branch_office_admin",
    "check_off_agent",
    "logbook_agent",
    "rental_loan_agent",
    "sme_loan_agent",
    "mobile_loan_agent",
]

class AdminUserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=8, max_length=200)
    role: AdminRole = "admin"
    # Meaningful only for role="loan_officer" (their home branch) - ignored
    # for every other role.
    email: EmailStr | None = None
    branch_id: str | None = None
    # Meaningful only for role="branch_office_admin" (the branches they
    # oversee) - ignored for every other role.
    managed_branch_ids: list[str] | None = None


class AdminUserUpdate(BaseModel):
    """All fields optional - only what's provided gets changed."""

    username: str | None = Field(default=None, min_length=3, max_length=100)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    role: AdminRole | None = None
    email: EmailStr | None = None
    is_active: bool | None = None
    branch_id: str | None = None
    managed_branch_ids: list[str] | None = None


class AdminUserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    role: str
    email: str | None = None
    is_active: bool
    branch_id: str | None = None
    managed_branch_ids: list[str] | None = None
    created_at: datetime


class AdminUserCreateResponse(BaseModel):
    success: bool = True
    message: str = "Admin user created."
    data: AdminUserRead


class AdminUserUpdateResponse(BaseModel):
    success: bool = True
    message: str = "Admin user updated."
    data: AdminUserRead


class AdminUserListResponse(BaseModel):
    items: list[AdminUserRead]


class AdminUserHardDeleteResponse(BaseModel):
    success: bool = True
    message: str = "Admin user permanently deleted."
    # What had to be cleaned up first so the delete itself wouldn't hit a
    # foreign-key conflict - see delete_admin_user_permanently in
    # admin.py for exactly what each count covers. Anything not listed
    # here (e.g. loan applications that were assigned to this admin as a
    # loan officer) is summarized in reassigned/unassigned below instead
    # of deleted outright, since that content has value independent of
    # who this admin was.
    deleted_recruiter_notes: int = 0
    deleted_internal_notifications: int = 0
    unassigned_loan_applications: int = 0
    anonymized_records: int = 0
 