import logging
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.data.job_openings import KNOWN_ROLES
from app.database import get_db
from app.models.career_application import CareerApplication
from app.models.job_opening import JobOpening
from app.schemas.career_application import CareerApplicationCreateResponse, CareerApplicationRead
from app.services.storage import supabase, BUCKET

logger = logging.getLogger("bidii.careers")

router = APIRouter(prefix="/api/careers", tags=["careers"])

settings = get_settings()
_email_adapter = TypeAdapter(EmailStr)


def _safe_filename(original: str) -> str:
    """Strips path separators and unsafe characters, keeps the extension."""
    name = Path(original).name  # drops any directory components
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name or "cv.pdf"


@router.post("/applications", response_model=CareerApplicationCreateResponse, status_code=status.HTTP_201_CREATED)
async def submit_career_application(
    full_name: str = Form(..., min_length=2, max_length=200),
    email: str = Form(...),
    phone: str = Form(..., min_length=10, max_length=40),
    role: str = Form(..., min_length=1, max_length=150),
    cover_note: str = Form(..., min_length=10, max_length=5000),
    job_id: str | None = Form(None),
    cv: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> CareerApplicationCreateResponse:
    """
    Receives a career application from the frontend's Careers page form
    (multipart/form-data - this endpoint handles a file upload, unlike the
    contact/loan-application endpoints which are plain JSON).

    job_id links the application to a real JobOpening for the admin vetting
    view. When present, the job's current title is used as `role` (so it's
    accurate even if the posting is edited/closed later); when absent (the
    "General application" option), `role` is taken as given and loosely
    checked against KNOWN_ROLES for visibility only, not rejection.
    """
    try:
        validated_email = str(_email_adapter.validate_python(email))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Enter a valid email address.") from exc

    resolved_role = role
    if job_id:
        job = db.query(JobOpening).filter(JobOpening.id == job_id).first()
        if job is None:
            raise HTTPException(status_code=422, detail="This job posting could not be found.")
        resolved_role = job.title
    elif role not in KNOWN_ROLES:
        # Not rejected - job openings change more often than this list is
        # updated. Just logged so stale entries in KNOWN_ROLES get noticed.
        logger.info("Career application for a role not in KNOWN_ROLES: %r", role)

    if cv.content_type != "application/pdf":
        raise HTTPException(status_code=422, detail="CV must be a PDF file.")

    contents = await cv.read()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(contents) > max_bytes:
        raise HTTPException(
            status_code=422,
            detail=f"CV file is too large. Maximum size is {settings.max_upload_size_mb}MB.",
        )
    if len(contents) == 0:
        raise HTTPException(status_code=422, detail="The uploaded CV file is empty.")

    # settings.upload_dir.mkdir(parents=True, exist_ok=True)
    # careers_dir = settings.upload_dir / "careers"
    # careers_dir.mkdir(parents=True, exist_ok=True)

    # original_name = _safe_filename(cv.filename or "cv.pdf")
    # stored_name = f"{uuid.uuid4()}_{original_name}"
    # (careers_dir / stored_name).write_bytes(contents)
    original_name = _safe_filename(cv.filename or "cv.pdf")

    stored_name = f"{uuid.uuid4()}_{original_name}"
    storage_path = f"careers/{stored_name}"

    try:
        supabase.storage.from_(BUCKET).upload(
            storage_path,
            contents,
            {
                   "content-type": "application/pdf",
                   "upsert": False,
            },
       )

        logger.info("CV uploaded to Supabase Storage: %s", storage_path)

    except Exception as exc:
        logger.exception("Failed to upload CV to Supabase Storage")
        raise HTTPException(
           status_code=500,
           detail="Failed to store CV. Please try again.",
        )  from exc

    record = CareerApplication(
        job_id=job_id or None,
        full_name=full_name,
        email=validated_email,
        phone=phone,
        role=resolved_role,
        cover_note=cover_note,
        cv_original_filename=original_name,
        cv_stored_filename=storage_path,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    logger.info("New career application from %s <%s> for role=%s", full_name, validated_email, resolved_role)

    return CareerApplicationCreateResponse(data=CareerApplicationRead.model_validate(record))
