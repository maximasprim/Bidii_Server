from pydantic import BaseModel


class RoutedAgentRead(BaseModel):
    id: str
    username: str
    email: str | None = None
    is_active: bool = True


class ProductRoutingRead(BaseModel):
    product_slug: str
    product_name: str
    suggested_role: str | None = None
    assigned_admin_id: str | None = None
    assigned_admin_username: str | None = None
    assigned_admin_email: str | None = None
    # Every agent this product is routed to (assigned_admin_* above = the first one).
    # New applications are shared between the active ones in turn.
    assigned_admins: list[RoutedAgentRead] = []


class ProductRoutingListResponse(BaseModel):
    items: list[ProductRoutingRead]


class ProductRoutingUpdate(BaseModel):
    # Null clears the assignment and reverts the product to normal
    # branch-based routing.
    assigned_admin_id: str | None = None
    # Multi-agent form: the full list of agents for the product (empty list = normal
    # branch routing). When sent it takes precedence over assigned_admin_id.
    assigned_admin_ids: list[str] | None = None


class ProductRoutingUpdateResponse(BaseModel):
    success: bool = True
    message: str = "Routing updated."
    data: ProductRoutingRead
