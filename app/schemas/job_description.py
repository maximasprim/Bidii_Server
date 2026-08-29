from pydantic import BaseModel, Field

from app.schemas.ats import ATSAIProviderName


class JDKeyResponsibility(BaseModel):
    heading: str = Field(min_length=1, max_length=200)
    bullets: list[str] = Field(default_factory=list, max_length=10)
    pct_time: int = Field(ge=0, le=100)
    criteria: list[str] = Field(default_factory=list, max_length=10)


class JDContent(BaseModel):
    """
    The role-specific content of the formal Job Description document -
    everything in the sample JD PDF EXCEPT the fixed header/letterhead,
    the info table (title/department/reports-to, which come from the
    JobOpening record itself), and the company-wide Performance and
    Behavioral Competencies section (fixed text, not per-role - see
    app/services/jd_pdf.py). Stored as JobOpening.jd_content once an
    admin has generated and/or edited it; used to render the PDF.
    """

    overall_role_purpose: str = Field(min_length=1, max_length=500)
    reports_to: str = Field(default="", max_length=200)
    key_responsibilities: list[JDKeyResponsibility] = Field(min_length=1, max_length=10)
    reporting_relationships: str = Field(default="", max_length=500)
    decision_making_mandates: str = Field(default="", max_length=500)
    planning_responsibility: str = Field(default="", max_length=500)
    relationship_management: str = Field(default="", max_length=500)
    minimum_qualifications: list[str] = Field(default_factory=list, max_length=15)
    experience_and_skills: list[str] = Field(default_factory=list, max_length=15)


class JDGenerateRequest(BaseModel):
    provider: ATSAIProviderName
    model: str | None = Field(default=None, max_length=100)


class JDGenerateResponse(BaseModel):
    success: bool = True
    message: str = "Draft generated - review and edit before saving or exporting to PDF."
    data: JDContent
    provider: str
    model: str


class JDUpdateRequest(BaseModel):
    jd_content: JDContent


class JDResponse(BaseModel):
    success: bool = True
    message: str = "Job description saved."
    data: JDContent | None = None
