import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.role_permission import RolePermission
from app.schemas.role_permission import (
    MenuItem,
    MyPermissionsResponse,
    RolePermissionRead,
    RolePermissionsResponse,
    RolePermissionUpdate,
    RolePermissionUpdateResponse,
)
from app.services.auth import get_current_admin, require_roles
from app.services.role_permissions import (
    CONFIGURABLE_ROLES,
    MENU_PATHS,
    MENU_REGISTRY,
    get_all_effective_permissions,
    get_effective_menus_for_role,
)

logger = logging.getLogger("bidii.role_permissions")

router = APIRouter(prefix="/api/admin/role-permissions", tags=["admin-role-permissions"])


@router.get("/mine", response_model=MyPermissionsResponse)
def get_my_permissions(
    db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)
) -> MyPermissionsResponse:
    """
    Any logged-in admin can call this — it only ever describes their own
    access, never another role's. Powers AdminLayout's sidebar filtering
    and route guard on every dashboard page load.
    """
    return MyPermissionsResponse(
        role=current_admin.role, allowed_menus=get_effective_menus_for_role(db, current_admin.role)
    )


@router.get("", response_model=RolePermissionsResponse, dependencies=[Depends(require_roles("admin"))])
def list_role_permissions(db: Session = Depends(get_db)) -> RolePermissionsResponse:
    """Admin-only — powers the Roles & Permissions settings page's full grid."""
    permissions = get_all_effective_permissions(db)
    return RolePermissionsResponse(
        menus=[MenuItem(path=path, label=label) for path, label in MENU_REGISTRY],
        items=[RolePermissionRead(role=p.role, allowed_menus=p.allowed_menus, is_default=p.is_default) for p in permissions],
    )


@router.put("/{role}", response_model=RolePermissionUpdateResponse, dependencies=[Depends(require_roles("admin"))])
def update_role_permissions(
    role: str,
    payload: RolePermissionUpdate,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> RolePermissionUpdateResponse:
    """
    Admin-only. "admin" itself can't be edited here — it always has every
    menu, unconditionally, specifically so nobody (including by mistake)
    can lock every admin account out of the dashboard. Attempting to
    update it is rejected outright rather than silently ignored.
    """
    if role == "admin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The admin role always has full access and can't be restricted here.",
        )
    if role not in CONFIGURABLE_ROLES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f'Unknown role "{role}".')

    invalid = [p for p in payload.allowed_menus if p not in MENU_PATHS]
    if invalid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown menu path(s): {', '.join(invalid)}")

    # Every role always keeps Overview — it's the landing page and the
    # route guard's redirect target, so a role with zero menus (including
    # Overview) would have nowhere to land and effectively be locked out
    # of the dashboard entirely on their next login.
    allowed = payload.allowed_menus if "/admin" in payload.allowed_menus else ["/admin", *payload.allowed_menus]
    # De-duplicate while preserving order, in case the frontend sent one twice.
    allowed = list(dict.fromkeys(allowed))

    row = db.query(RolePermission).filter(RolePermission.role == role).first()
    if row is None:
        row = RolePermission(role=role)
        db.add(row)
    row.allowed_menus = allowed
    row.updated_by = current_admin.username
    db.commit()
    db.refresh(row)

    logger.info("Admin %r updated menu permissions for role %r: %s", current_admin.username, role, allowed)
    return RolePermissionUpdateResponse(data=RolePermissionRead(role=row.role, allowed_menus=row.allowed_menus, is_default=False))
