from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.career_application import CareerApplication
from app.models.job_opening import JobOpening
from app.schemas.job_description import JDContent, JDResponse, JDUpdateRequest
from app.schemas.job_opening import (
    AdminJobOpeningListResponse,
    JobOpeningCreate,
    JobOpeningCreateResponse,
    JobOpeningRead,
    JobOpeningUpdate,
    JobOpeningUpdateResponse,
    JobOpeningWithCount,
)
from app.services.auth import get_current_admin
from app.services.slugify import slugify

router = APIRouter(prefix="/api/admin/jobs", tags=["admin-jobs"], dependencies=[Depends(get_current_admin)])


def _unique_slug(db: Session, base_slug: str, exclude_id: str | None = None) -> str:
    slug = base_slug
    suffix = 2
    while True:
        query = db.query(JobOpening).filter(JobOpening.slug == slug)
        if exclude_id:
            query = query.filter(JobOpening.id != exclude_id)
        if query.first() is None:
            return slug
        slug = f"{base_slug}-{suffix}"
        suffix += 1


@router.get("", response_model=AdminJobOpeningListResponse)
def list_all_jobs(db: Session = Depends(get_db)) -> AdminJobOpeningListResponse:
    """Includes closed postings, and an application_count per job for the vetting view."""
    counts = dict(
        db.query(CareerApplication.job_id, func.count(CareerApplication.id))
        .group_by(CareerApplication.job_id)
        .all()
    )
    jobs = db.query(JobOpening).order_by(JobOpening.created_at.desc()).all()
    return AdminJobOpeningListResponse(
        items=[
            JobOpeningWithCount(**JobOpeningRead.model_validate(j).model_dump(), application_count=counts.get(j.id, 0))
            for j in jobs
        ]
    )


@router.post("", response_model=JobOpeningCreateResponse, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobOpeningCreate, db: Session = Depends(get_db)) -> JobOpeningCreateResponse:
    base_slug = slugify(payload.slug or f"{payload.title}-{payload.location}")
    slug = _unique_slug(db, base_slug)

    job = JobOpening(
        slug=slug,
        title=payload.title,
        department=payload.department,
        location=payload.location,
        type=payload.type,
        description=payload.description,
        requirements=payload.requirements,
        responsibilities=payload.responsibilities,
        is_open=payload.is_open,
        application_deadline=payload.application_deadline,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return JobOpeningCreateResponse(data=JobOpeningRead.model_validate(job))


@router.patch("/{job_id}", response_model=JobOpeningUpdateResponse)
def update_job(job_id: str, payload: JobOpeningUpdate, db: Session = Depends(get_db)) -> JobOpeningUpdateResponse:
    job = db.query(JobOpening).filter(JobOpening.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found.")

    if payload.title is not None:
        job.title = payload.title
    if payload.department is not None:
        job.department = payload.department
    if payload.location is not None:
        job.location = payload.location
    if payload.type is not None:
        job.type = payload.type
    if payload.description is not None:
        job.description = payload.description
    if payload.requirements is not None:
        job.requirements = payload.requirements
    if payload.responsibilities is not None:
        job.responsibilities = payload.responsibilities
    if payload.is_open is not None:
        job.is_open = payload.is_open
    if payload.clear_application_deadline:
        job.application_deadline = None
    elif payload.application_deadline is not None:
        job.application_deadline = payload.application_deadline
    if payload.slug is not None:
        job.slug = _unique_slug(db, slugify(payload.slug), exclude_id=job.id)

    db.commit()
    db.refresh(job)
    return JobOpeningUpdateResponse(data=JobOpeningRead.model_validate(job))


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: str, db: Session = Depends(get_db)) -> None:
    job = db.query(JobOpening).filter(JobOpening.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found.")

    application_count = db.query(func.count(CareerApplication.id)).filter(CareerApplication.job_id == job_id).scalar() or 0
    if application_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Can't delete a posting with {application_count} application(s) on file. Close it instead.",
        )

    db.delete(job)
    db.commit()


# ---------------------------------------------------------------------------
# Formal Job Description document - see app/schemas/job_description.py for
# the JDContent shape and app/services/jd_pdf.py for how it's rendered.
# AI generation lives in admin_ai.py (POST /api/admin/ai/jobs/{job_id}/jd/generate),
# matching where every other "AI, outside of scoring one candidate" endpoint
# lives - this file only owns saving the (admin-reviewed) content and
# rendering/downloading the PDF from whatever's currently saved.
# ---------------------------------------------------------------------------


@router.get("/{job_id}/jd", response_model=JDResponse)
def get_job_description(job_id: str, db: Session = Depends(get_db)) -> JDResponse:
    job = db.query(JobOpening).filter(JobOpening.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found.")
    return JDResponse(data=JDContent.model_validate(job.jd_content) if job.jd_content else None)


@router.put("/{job_id}/jd", response_model=JDResponse)
def save_job_description(job_id: str, payload: JDUpdateRequest, db: Session = Depends(get_db)) -> JDResponse:
    """
    Saves (admin-reviewed, possibly hand-edited) JD content for this job.
    Whether it came from the AI generator or was typed by hand makes no
    difference here - either way it's saved through this one endpoint,
    same as every other "AI draft becomes real data" flow in this app
    (see the module docstrings on app/services/ai_job_generation.py and
    app/services/ai_criteria_suggestion.py for the same pattern).
    """
    job = db.query(JobOpening).filter(JobOpening.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found.")

    job.jd_content = payload.jd_content.model_dump()
    db.commit()
    db.refresh(job)
    return JDResponse(data=JDContent.model_validate(job.jd_content))


@router.get("/{job_id}/jd/pdf")
def download_job_description_pdf(job_id: str, db: Session = Depends(get_db)):
    """
    Renders the currently-saved jd_content into the fixed-format PDF and
    returns it directly - nothing is written to disk or Supabase Storage;
    it's generated fresh into memory on every request, since rendering is
    cheap and this way the PDF can never go stale relative to the saved
    content.
    """
    job = db.query(JobOpening).filter(JobOpening.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found.")
    if not job.jd_content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This job has no formal job description yet - generate or write one first.",
        )

    import io

    from app.services.jd_pdf import render_jd_pdf

    buffer = io.BytesIO()
    render_jd_pdf(job=job, jd_content=job.jd_content, output_path=buffer)
    pdf_bytes = buffer.getvalue()

    filename = f"JD - {job.title}.pdf".replace("/", "-")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
