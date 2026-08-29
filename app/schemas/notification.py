from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.notification import NotificationLogStatus, NotificationTrigger


class NotificationTemplateCreate(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    trigger: NotificationTrigger = NotificationTrigger.manual
    subject: str = Field(min_length=2, max_length=255)
    body: str = Field(min_length=2, max_length=5000)
    is_active: bool = True


class NotificationTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=150)
    trigger: NotificationTrigger | None = None
    subject: str | None = Field(default=None, min_length=2, max_length=255)
    body: str | None = Field(default=None, min_length=2, max_length=5000)
    is_active: bool | None = None


class NotificationTemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    trigger: NotificationTrigger
    subject: str
    body: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class NotificationTemplateListResponse(BaseModel):
    items: list[NotificationTemplateRead]


class NotificationTemplateResponse(BaseModel):
    success: bool = True
    data: NotificationTemplateRead


class NotificationAutomationRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trigger: NotificationTrigger
    template_id: str | None
    is_enabled: bool
    updated_at: datetime


class NotificationAutomationRuleUpdate(BaseModel):
    template_id: str | None = None
    is_enabled: bool = False


class NotificationAutomationListResponse(BaseModel):
    items: list[NotificationAutomationRuleRead]


class NotificationSendRequest(BaseModel):
    application_id: str
    # Either pick an existing template (subject/body pre-filled and
    # editable client-side) or send fully custom text - both land here the
    # same way, since by the time this reaches the backend there's no
    # meaningful difference between "edited template" and "custom".
    template_id: str | None = None
    subject: str = Field(min_length=2, max_length=255)
    body: str = Field(min_length=2, max_length=5000)


class NotificationLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    application_id: str
    template_id: str | None
    trigger: NotificationTrigger
    recipient_email: str
    subject: str
    body: str
    status: NotificationLogStatus
    error_message: str | None
    sent_by_admin_id: str | None
    created_at: datetime


class NotificationLogListResponse(BaseModel):
    items: list[NotificationLogRead]


class NotificationSendResponse(BaseModel):
    success: bool = True
    data: NotificationLogRead
