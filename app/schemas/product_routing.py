from pydantic import BaseModel


class ProductRoutingRead(BaseModel):
    product_slug: str
    product_name: str
    suggested_role: str | None = None
    assigned_admin_id: str | None = None
    assigned_admin_username: str | None = None
    assigned_admin_email: str | None = None


class ProductRoutingListResponse(BaseModel):
    items: list[ProductRoutingRead]


class ProductRoutingUpdate(BaseModel):
    # Null clears the assignment and reverts the product to normal
    # branch-based routing.
    assigned_admin_id: str | None = None


class ProductRoutingUpdateResponse(BaseModel):
    success: bool = True
    message: str = "Routing updated."
    data: ProductRoutingRead
