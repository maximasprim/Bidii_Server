"""Request bodies for the Excel import wizard. Unknown fields are rejected so a typo can't silently do nothing."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class ImportOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_par: bool | None = None
    import_targets: bool | None = None
    import_actions: bool | None = None
    create_missing_staff: bool | None = None
    overwrite_manual: bool | None = None
    overwrite_targets: bool | None = None
    default_role_key: str | None = Field(default=None, min_length=1, max_length=60)
    exclude_months: list[Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]] | None = Field(default=None, max_length=12)


class NewUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    kind: Literal["branch", "team", "head_office"]


class SheetDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["use", "create", "skip"]
    unit_id: str | None = Field(default=None, max_length=36)
    new_unit: NewUnit | None = None
    role_key: str | None = Field(default=None, max_length=60)


class StaffDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["match", "create", "skip"]
    profile_id: str | None = Field(default=None, max_length=36)
    role_key: str | None = Field(default=None, max_length=60)


class DecisionsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    options: ImportOptions | None = None
    sheets: dict[str, SheetDecision] | None = Field(default=None, max_length=60)
    staff: dict[str, StaffDecision] | None = Field(default=None, max_length=2000)


class CommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]
