from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr

from app.models.career_application import CareerApplicationStatus


class CareerApplicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str | None
    full_name: str
    email: EmailStr
    phone: str
    role: str
    cover_note: str
    cv_original_filename: str
    status: CareerApplicationStatus
    created_at: datetime


class CareerApplicationCreateResponse(BaseModel):
    success: bool = True
    message: str = "Application received. Our recruitment team reviews applications weekly."
    data: CareerApplicationRead
