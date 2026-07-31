import logging

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.contact import ContactMessage
from app.schemas.contact import ContactCreate, ContactCreateResponse, ContactRead

logger = logging.getLogger("bidii.contact")

router = APIRouter(prefix="/api/contact", tags=["contact"])


@router.post("", response_model=ContactCreateResponse, status_code=status.HTTP_201_CREATED)
def submit_contact_message(payload: ContactCreate, db: Session = Depends(get_db)) -> ContactCreateResponse:
    """
    Receives a contact form submission from the frontend's Contact page,
    persists it, and returns a confirmation. This is the endpoint
    src/pages/Contact.tsx's onSubmit calls.
    """
    record = ContactMessage(
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        subject=payload.subject,
        message=payload.message,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    logger.info("New contact message from %s <%s> (subject=%s)", record.name, record.email, record.subject.value)

    return ContactCreateResponse(data=ContactRead.model_validate(record))
