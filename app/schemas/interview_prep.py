from pydantic import BaseModel, Field

from app.schemas.ats import ATSAIProviderName


class InterviewPrepQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    # Short note on what a strong answer would show - blank for the fixed
    # standard questions (see app/services/interview_prep_generation.py's
    # STANDARD_INTERVIEW_SECTIONS), populated by the AI for the
    # role-specific section.
    why_it_matters: str = Field(default="", max_length=500)


class InterviewPrepSection(BaseModel):
    title: str = Field(min_length=1, max_length=150)
    questions: list[InterviewPrepQuestion] = Field(default_factory=list, max_length=20)


class InterviewPrepContent(BaseModel):
    """
    A candidate-specific interview prep sheet - one per CareerApplication
    (stored as CareerApplication.interview_prep_content), not per job, since
    the candidate_summary/key_strengths/areas_to_probe and the
    Role-Specific & Technical section are all specific to one candidate's
    actual cover note/CV, not just the job posting.

    `sections` always includes the three fixed, non-AI-generated sections
    every candidate for every role is asked (HR & Background, Competency &
    Situational, Panel Assessment & Fit - taken directly from the
    company's INTERVIEW_QUESTIONS.docx reference sheet so every interview
    starts from the same fair baseline) plus, when generated, one
    "Role-Specific & Technical" section tailored to this candidate and
    job. Nothing stops an admin from editing/removing/reordering any of
    these once saved - this is a DRAFT for the panel to use, never a
    locked script.
    """

    candidate_summary: str = Field(default="", max_length=1000)
    key_strengths: list[str] = Field(default_factory=list, max_length=10)
    areas_to_probe: list[str] = Field(default_factory=list, max_length=10)
    sections: list[InterviewPrepSection] = Field(min_length=1, max_length=10)


class InterviewPrepGenerateRequest(BaseModel):
    provider: ATSAIProviderName
    model: str | None = Field(default=None, max_length=100)


class InterviewPrepGenerateResponse(BaseModel):
    success: bool = True
    message: str = "Draft generated - review and edit before saving or exporting to PDF."
    data: InterviewPrepContent
    provider: str
    model: str


class InterviewPrepUpdateRequest(BaseModel):
    interview_prep_content: InterviewPrepContent


class InterviewPrepResponse(BaseModel):
    success: bool = True
    message: str = "Interview prep saved."
    data: InterviewPrepContent | None = None
