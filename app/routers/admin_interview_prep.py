"""
Save/download endpoints for a candidate-specific interview prep sheet
(see app/schemas/interview_prep.py). AI generation lives in admin_ai.py
(POST /api/admin/ai/career-applications/{application_id}/interview-prep/generate),
matching where every other "AI drafts, human saves" endpoint lives - this
file only owns saving the (admin-reviewed) content and rendering/
downloading the PDF from whatever's currently saved, exactly the same
split as app/routers/admin_jobs.py's formal-JD section.

No require_menu_access dependency here on purpose - the underlying
career-applications endpoints this sits alongside (see
GET/PATCH/DELETE /api/admin/career-applications... in admin.py) don't
have one either, and this feature is reachable from both the Career
Applications list and the Candidate Screening detail page, which are
gated by two different (and not always co-granted) menu permissions -
any logged-in admin who can already see a candidate can prep for their
interview.
"""

import io

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.career_application import CareerApplication
from app.models.job_opening import JobOpening
from app.schemas.interview_prep import InterviewPrepContent, InterviewPrepResponse, InterviewPrepUpdateRequest
from app.services.auth import get_current_admin
from app.services.interview_prep_pdf import render_interview_prep_pdf

router = APIRouter(
    prefix="/api/admin/career-applications",
    tags=["admin-interview-prep"],
    dependencies=[Depends(get_current_admin)],
)


def _get_application_or_404(db: Session, application_id: str) -> CareerApplication:
    application = db.query(CareerApplication).filter(CareerApplication.id == application_id).first()
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Career application not found.")
    return application


@router.get("/{application_id}/interview-prep", response_model=InterviewPrepResponse)
def get_interview_prep(application_id: str, db: Session = Depends(get_db)) -> InterviewPrepResponse:
    application = _get_application_or_404(db, application_id)
    return InterviewPrepResponse(
        data=InterviewPrepContent.model_validate(application.interview_prep_content)
        if application.interview_prep_content
        else None
    )


@router.put("/{application_id}/interview-prep", response_model=InterviewPrepResponse)
def save_interview_prep(
    application_id: str, payload: InterviewPrepUpdateRequest, db: Session = Depends(get_db)
) -> InterviewPrepResponse:
    """
    Saves (admin-reviewed, possibly hand-edited) interview prep content
    for this application. Whether it came from the AI generator or was
    typed/edited by hand makes no difference - either way it's saved
    through this one endpoint, same pattern as
    PUT /api/admin/jobs/{job_id}/jd (see admin_jobs.py).
    """
    application = _get_application_or_404(db, application_id)
    application.interview_prep_content = payload.interview_prep_content.model_dump()
    db.commit()
    db.refresh(application)
    return InterviewPrepResponse(data=InterviewPrepContent.model_validate(application.interview_prep_content))


@router.get("/{application_id}/interview-prep/pdf")
def download_interview_prep_pdf(application_id: str, db: Session = Depends(get_db)):
    """
    Renders the currently-saved interview_prep_content into a PDF and
    returns it directly - nothing written to disk or Supabase Storage;
    generated fresh into memory on every request, matching
    GET /api/admin/jobs/{job_id}/jd/pdf's approach so this PDF can never
    go stale relative to the saved content either.
    """
    application = _get_application_or_404(db, application_id)
    if not application.interview_prep_content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This candidate has no interview prep yet - generate or write one first.",
        )
    job = (
        db.query(JobOpening).filter(JobOpening.id == application.job_id).first() if application.job_id else None
    )

    buffer = io.BytesIO()
    render_interview_prep_pdf(application=application, job=job, content=application.interview_prep_content, output_path=buffer)
    pdf_bytes = buffer.getvalue()

    filename = f"Interview Prep - {application.full_name}.pdf".replace("/", "-")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
