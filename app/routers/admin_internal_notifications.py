"""
The admin-facing "bell icon" inbox - every admin sees only their own
InternalNotification rows. See app/models/internal_notification.py and
app/services/internal_notifications.py for how these get created; this
router is purely read + mark-read, nothing here creates a notification.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_user import AdminUser
from app.models.internal_notification import InternalNotification
from app.services.auth import get_current_admin

router = APIRouter(prefix="/api/admin/internal-notifications", tags=["admin-internal-notifications"])


class InternalNotificationRead(BaseModel):
    id: str
    message: str
    link_path: str | None
    related_loan_application_id: str | None
    is_read: bool
    created_at: str

    model_config = {"from_attributes": True}


class InternalNotificationListResponse(BaseModel):
    items: list[InternalNotificationRead]
    unread_count: int


def _serialize(n: InternalNotification) -> InternalNotificationRead:
    return InternalNotificationRead(
        id=n.id,
        message=n.message,
        link_path=n.link_path,
        related_loan_application_id=n.related_loan_application_id,
        is_read=n.is_read,
        created_at=n.created_at.isoformat(),
    )


@router.get("", response_model=InternalNotificationListResponse)
def list_my_notifications(
    db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)
) -> InternalNotificationListResponse:
    notifications = (
        db.query(InternalNotification)
        .filter(InternalNotification.recipient_admin_id == current_admin.id)
        .order_by(InternalNotification.created_at.desc())
        .limit(50)
        .all()
    )
    unread_count = (
        db.query(InternalNotification)
        .filter(InternalNotification.recipient_admin_id == current_admin.id, InternalNotification.is_read.is_(False))
        .count()
    )
    return InternalNotificationListResponse(items=[_serialize(n) for n in notifications], unread_count=unread_count)


@router.post("/{notification_id}/read", response_model=InternalNotificationRead)
def mark_notification_read(
    notification_id: str, db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)
) -> InternalNotificationRead:
    notification = (
        db.query(InternalNotification)
        .filter(InternalNotification.id == notification_id, InternalNotification.recipient_admin_id == current_admin.id)
        .first()
    )
    if notification is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found.")
    notification.is_read = True
    db.commit()
    db.refresh(notification)
    return _serialize(notification)


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
def mark_all_notifications_read(db: Session = Depends(get_db), current_admin: AdminUser = Depends(get_current_admin)) -> None:
    db.query(InternalNotification).filter(
        InternalNotification.recipient_admin_id == current_admin.id, InternalNotification.is_read.is_(False)
    ).update({"is_read": True})
    db.commit()
