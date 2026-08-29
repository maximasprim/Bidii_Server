"""
Configurable candidate-email notifications. Three concerns:
- /templates: CRUD for reusable subject/body text.
- /automation: one rule per real CareerApplicationStatus, wiring a status
  change to automatically send a template (see app/services/notifications.py).
- /send: send a message to one candidate right now, at any stage,
  regardless of automation - either from a template or fully custom text.
- /logs: what was actually sent to a given candidate, and whether it worked.

Access is gated the same way ATS is - see app/services/role_permissions.py
- since candidate email addresses and communication history are exactly
the kind of data role-based menu access exists to protect.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.career_application import CareerApplication
from app.models.notification import (
    NotificationAutomationRule,
    NotificationLog,
    NotificationTemplate,
    NotificationTrigger,
)
from app.schemas.notification import (
    NotificationAutomationListResponse,
    NotificationAutomationRuleRead,
    NotificationAutomationRuleUpdate,
    NotificationLogListResponse,
    NotificationSendRequest,
    NotificationSendResponse,
    NotificationTemplateCreate,
    NotificationTemplateListResponse,
    NotificationTemplateResponse,
    NotificationTemplateUpdate,
)
from app.services.auth import get_current_admin
from app.services.notifications import send_notification
from app.services.role_permissions import require_menu_access

logger = logging.getLogger("bidii.admin_notifications")

router = APIRouter(
    prefix="/api/admin/notifications",
    tags=["admin-notifications"],
    dependencies=[Depends(get_current_admin), Depends(require_menu_access("/admin/notifications"))],
)

# Every real status a candidate can automatically transition into.
# "manual" deliberately excluded - see NotificationTrigger's docstring.
_AUTOMATABLE_TRIGGERS = [
    NotificationTrigger.received,
    NotificationTrigger.reviewing,
    NotificationTrigger.shortlisted,
    NotificationTrigger.rejected,
    NotificationTrigger.hired,
]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


@router.get("/templates", response_model=NotificationTemplateListResponse)
def list_templates(db: Session = Depends(get_db)) -> NotificationTemplateListResponse:
    templates = db.query(NotificationTemplate).order_by(NotificationTemplate.created_at.desc()).all()
    return NotificationTemplateListResponse(items=templates)


@router.post("/templates", response_model=NotificationTemplateResponse, status_code=status.HTTP_201_CREATED)
def create_template(payload: NotificationTemplateCreate, db: Session = Depends(get_db)) -> NotificationTemplateResponse:
    template = NotificationTemplate(**payload.model_dump())
    db.add(template)
    db.commit()
    db.refresh(template)
    return NotificationTemplateResponse(data=template)


@router.patch("/templates/{template_id}", response_model=NotificationTemplateResponse)
def update_template(
    template_id: str, payload: NotificationTemplateUpdate, db: Session = Depends(get_db)
) -> NotificationTemplateResponse:
    template = db.query(NotificationTemplate).filter(NotificationTemplate.id == template_id).first()
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification template not found.")

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(template, field, value)

    db.commit()
    db.refresh(template)
    return NotificationTemplateResponse(data=template)


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(template_id: str, db: Session = Depends(get_db)) -> None:
    template = db.query(NotificationTemplate).filter(NotificationTemplate.id == template_id).first()
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification template not found.")

    in_use = (
        db.query(NotificationAutomationRule).filter(NotificationAutomationRule.template_id == template_id).first()
    )
    if in_use is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This template is wired to the '{in_use.trigger.value}' automation rule - "
            "turn that off or point it at a different template first.",
        )

    db.delete(template)
    db.commit()


# ---------------------------------------------------------------------------
# Automation rules - one row per real status, get-or-create on read (same
# pattern app/routers/admin_ats_config.py already uses for ATSConfiguration).
# ---------------------------------------------------------------------------


@router.get("/automation", response_model=NotificationAutomationListResponse)
def list_automation_rules(db: Session = Depends(get_db)) -> NotificationAutomationListResponse:
    existing = {
        rule.trigger: rule
        for rule in db.query(NotificationAutomationRule)
        .filter(NotificationAutomationRule.trigger.in_(_AUTOMATABLE_TRIGGERS))
        .all()
    }
    rules = []
    for trigger in _AUTOMATABLE_TRIGGERS:
        rule = existing.get(trigger)
        if rule is None:
            rule = NotificationAutomationRule(trigger=trigger, is_enabled=False, template_id=None)
            db.add(rule)
        rules.append(rule)
    db.commit()
    for rule in rules:
        db.refresh(rule)
    return NotificationAutomationListResponse(items=rules)


@router.put("/automation/{trigger}", response_model=NotificationAutomationRuleRead)
def update_automation_rule(
    trigger: NotificationTrigger, payload: NotificationAutomationRuleUpdate, db: Session = Depends(get_db)
) -> NotificationAutomationRuleRead:
    if trigger not in _AUTOMATABLE_TRIGGERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="'manual' can't have an automation rule."
        )
    if payload.template_id:
        template = db.query(NotificationTemplate).filter(NotificationTemplate.id == payload.template_id).first()
        if template is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification template not found.")

    rule = db.query(NotificationAutomationRule).filter(NotificationAutomationRule.trigger == trigger).first()
    if rule is None:
        rule = NotificationAutomationRule(trigger=trigger)
        db.add(rule)

    rule.template_id = payload.template_id
    rule.is_enabled = payload.is_enabled
    db.commit()
    db.refresh(rule)
    return rule


# ---------------------------------------------------------------------------
# Manual send + logs
# ---------------------------------------------------------------------------


@router.post("/send", response_model=NotificationSendResponse)
def send_manual_notification(
    payload: NotificationSendRequest, db: Session = Depends(get_db), current_admin=Depends(get_current_admin)
) -> NotificationSendResponse:
    """
    Sends to a candidate right now, at whatever stage their application is
    currently at - independent of the automation rules above, which only
    fire on an actual status change. This is the "send an email to any
    candidate at any stage" entry point.
    """
    application = db.query(CareerApplication).filter(CareerApplication.id == payload.application_id).first()
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found.")

    log = send_notification(
        db,
        application,
        subject_template=payload.subject,
        body_template=payload.body,
        trigger=NotificationTrigger.manual,
        template_id=payload.template_id,
        admin_id=current_admin.id,
    )
    return NotificationSendResponse(data=log)


@router.get("/logs", response_model=NotificationLogListResponse)
def list_notification_logs(application_id: str | None = None, db: Session = Depends(get_db)) -> NotificationLogListResponse:
    query = db.query(NotificationLog)
    if application_id:
        query = query.filter(NotificationLog.application_id == application_id)
    logs = query.order_by(NotificationLog.created_at.desc()).limit(200).all()
    return NotificationLogListResponse(items=logs)
