from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AdminRole = Literal["admin", "loan_officer", "hr", "marketing_manager"]

class AdminUserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=8, max_length=200)
    role: AdminRole = "admin"


class AdminUserUpdate(BaseModel):
    """All fields optional — only what's provided gets changed."""

    username: str | None = Field(default=None, min_length=3, max_length=100)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    role: AdminRole | None = None
    is_active: bool | None = None


class AdminUserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    role: str
    is_active: bool
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
