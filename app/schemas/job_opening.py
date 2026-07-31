from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

JobType = Literal["Full-time", "Contract"]


class JobOpeningCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    department: str = Field(min_length=2, max_length=150)
    location: str = Field(min_length=2, max_length=150)
    type: JobType
    description: str = Field(min_length=10, max_length=3000)
    is_open: bool = True
    slug: str | None = Field(default=None, max_length=200)


class JobOpeningUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=200)
    department: str | None = Field(default=None, min_length=2, max_length=150)
    location: str | None = Field(default=None, min_length=2, max_length=150)
    type: JobType | None = None
    description: str | None = Field(default=None, min_length=10, max_length=3000)
    is_open: bool | None = None
    slug: str | None = Field(default=None, max_length=200)


class JobOpeningRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    slug: str
    title: str
    department: str
    location: str
    type: str
    description: str
    is_open: bool
    created_at: datetime
    updated_at: datetime


class JobOpeningWithCount(JobOpeningRead):
    application_count: int


class JobOpeningCreateResponse(BaseModel):
    success: bool = True
    message: str = "Job posting created."
    data: JobOpeningRead


class JobOpeningUpdateResponse(BaseModel):
    success: bool = True
    message: str = "Job posting updated."
    data: JobOpeningRead


class JobOpeningListResponse(BaseModel):
    items: list[JobOpeningRead]


class AdminJobOpeningListResponse(BaseModel):
    items: list[JobOpeningWithCount]
