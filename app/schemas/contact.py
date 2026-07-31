from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, ConfigDict

from app.models.contact import ContactSubject


class ContactCreate(BaseModel):
    """
    Mirrors the frontend's Zod schema exactly (src/pages/Contact.tsx):

        name: z.string().min(2)
        email: z.string().email()
        phone: z.string().min(10)
        subject: z.string().min(1)   -> one of ContactSubject
        message: z.string().min(10)

    Keeping field names and constraints identical means a validation error
    here maps directly onto the same form fields the frontend already shows
    errors under.
    """

    name: str = Field(min_length=2, max_length=200)
    email: EmailStr
    phone: str = Field(min_length=10, max_length=40)
    subject: ContactSubject
    message: str = Field(min_length=10, max_length=5000)


class ContactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: EmailStr
    phone: str
    subject: ContactSubject
    message: str
    created_at: datetime


class ContactCreateResponse(BaseModel):
    success: bool = True
    message: str = "Message received. A member of our team will get back to you within one business day."
    data: ContactRead
