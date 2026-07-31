from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.job_opening import JobOpening
from app.schemas.job_opening import JobOpeningListResponse, JobOpeningRead

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=JobOpeningListResponse)
def list_open_jobs(department: str | None = None, db: Session = Depends(get_db)) -> JobOpeningListResponse:
    query = db.query(JobOpening).filter(JobOpening.is_open.is_(True))
    if department:
        query = query.filter(JobOpening.department == department)
    jobs = query.order_by(JobOpening.created_at.desc()).all()
    return JobOpeningListResponse(items=[JobOpeningRead.model_validate(j) for j in jobs])
