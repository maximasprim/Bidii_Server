from pydantic import BaseModel, Field


class MenuItem(BaseModel):
    path: str
    label: str


class MyPermissionsResponse(BaseModel):
    """What GET /api/admin/role-permissions/mine returns — any logged-in
    admin can call this, it only ever describes their own access."""

    role: str
    allowed_menus: list[str]


class RolePermissionRead(BaseModel):
    role: str
    allowed_menus: list[str]
    is_default: bool


class RolePermissionsResponse(BaseModel):
    """What GET /api/admin/role-permissions returns — admin-only, the
    full settings-page view across every configurable role."""

    menus: list[MenuItem]
    items: list[RolePermissionRead]


class RolePermissionUpdate(BaseModel):
    allowed_menus: list[str] = Field(
        default_factory=list,
        description="Menu paths this role should be able to access. Must be a subset of the registered menu paths.",
    )


class RolePermissionUpdateResponse(BaseModel):
    success: bool = True
    message: str = "Permissions updated."
    data: RolePermissionRead
