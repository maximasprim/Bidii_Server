from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BranchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    address: str = Field(min_length=1, max_length=255)
    hours: str = Field(min_length=1, max_length=150)
    phone: str = Field(min_length=1, max_length=30)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    display_order: int = 0
    is_active: bool = True
    county: str | None = Field(default=None, max_length=50)


class BranchUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    address: str | None = Field(default=None, min_length=1, max_length=255)
    hours: str | None = Field(default=None, min_length=1, max_length=150)
    phone: str | None = Field(default=None, min_length=1, max_length=30)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    display_order: int | None = None
    is_active: bool | None = None
    county: str | None = Field(default=None, max_length=50)


class BranchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    address: str
    hours: str
    phone: str
    lat: float
    lng: float
    display_order: int
    is_active: bool
    county: str | None = None
    created_at: datetime
    updated_at: datetime


class BranchPublicRead(BaseModel):
    """What the public Branch Locator / homepage preview gets - no
    created_at/updated_at clutter, otherwise identical to BranchRead.
    There's no sensitive-data split here (unlike loan tiers' internal fee
    fields) - branch address/hours/phone are public information by
    nature - this is just a smaller, public-facing shape."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    address: str
    hours: str
    phone: str
    lat: float
    lng: float
    display_order: int
    is_active: bool


class BranchCreateResponse(BaseModel):
    success: bool = True
    message: str = "Branch created."
    data: BranchRead


class BranchUpdateResponse(BaseModel):
    success: bool = True
    message: str = "Branch updated."
    data: BranchRead


class BranchListResponse(BaseModel):
    items: list[BranchRead]


class BranchPublicListResponse(BaseModel):
    items: list[BranchPublicRead]
