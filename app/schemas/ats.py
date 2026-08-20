from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.ats import ATSAIProviderName, ATSAuditAction, ATSCriterionCategory, ATSEvaluationMode, ATSRecommendation
from app.schemas.admin import PageMeta
from app.schemas.career_application import CareerApplicationRead
from app.schemas.job_opening import JobOpeningRead


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------


class ATSCriterionCreate(BaseModel):
    category: ATSCriterionCategory
    label: str = Field(min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    match_keywords: list[str] = Field(default_factory=list, max_length=30)
    weight: float = Field(default=1.0, ge=0, le=100)
    is_required: bool = False


class ATSCriterionUpdate(BaseModel):
    category: ATSCriterionCategory | None = None
    label: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    match_keywords: list[str] | None = Field(default=None, max_length=30)
    weight: float | None = Field(default=None, ge=0, le=100)
    is_required: bool | None = None


class ATSCriterionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    config_id: str
    category: ATSCriterionCategory
    label: str
    description: str | None
    match_keywords: list[str] = Field(default_factory=list)
    weight: float
    is_required: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ATSConfigurationCreate(BaseModel):
    is_scoring_enabled: bool = True
    auto_reject_enabled: bool = False
    minimum_recommend_score: float = Field(default=70.0, ge=0, le=100)
    minimum_review_score: float = Field(default=40.0, ge=0, le=100)
    criteria: list[ATSCriterionCreate] = Field(default_factory=list)
    evaluation_mode: ATSEvaluationMode = ATSEvaluationMode.weighted
    ai_provider: ATSAIProviderName | None = None
    ai_model: str | None = Field(default=None, max_length=100)


class ATSConfigurationUpdate(BaseModel):
    is_scoring_enabled: bool | None = None
    auto_reject_enabled: bool | None = None
    minimum_recommend_score: float | None = Field(default=None, ge=0, le=100)
    minimum_review_score: float | None = Field(default=None, ge=0, le=100)
    evaluation_mode: ATSEvaluationMode | None = None
    ai_provider: ATSAIProviderName | None = None
    ai_model: str | None = Field(default=None, max_length=100)


class ATSConfigurationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str
    is_scoring_enabled: bool
    auto_reject_enabled: bool
    minimum_recommend_score: float
    minimum_review_score: float
    criteria: list[ATSCriterionRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    evaluation_mode: ATSEvaluationMode = ATSEvaluationMode.weighted
    ai_provider: ATSAIProviderName | None = None
    ai_model: str | None = None


class ATSConfigurationWithJob(ATSConfigurationRead):
    job_title: str
    job_slug: str
    job_is_open: bool


class ATSConfigurationResponse(BaseModel):
    success: bool = True
    message: str = "ATS configuration saved."
    data: ATSConfigurationRead


class ATSConfigurationListResponse(BaseModel):
    items: list[ATSConfigurationWithJob]


class ATSCriterionResponse(BaseModel):
    success: bool = True
    message: str = "Criterion saved."
    data: ATSCriterionRead


# ---------------------------------------------------------------------------
# Screening results
# ---------------------------------------------------------------------------


class ATSCriterionOutcome(BaseModel):
    """
    Shape of each entry in matched_criteria/missing_criteria for WEIGHTED
    mode. AI mode entries are shaped {label, detail} instead (see
    AIRequirementOutcome) — both are stored as plain JSON, so
    ATSScreeningResultRead types those fields as list[dict] and the
    frontend renders whichever shape is present (they always share
    "label"). This class exists for backend-side documentation/reference.
    """

    criterion_id: str
    label: str
    category: str
    weight: float
    is_required: bool


class ATSScreeningResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    application_id: str
    config_id: str | None
    total_score: float
    max_possible_score: float
    score_percentage: float = 0.0
    system_recommendation: ATSRecommendation
    override_recommendation: ATSRecommendation | None
    override_reason: str | None
    override_by: str | None
    overridden_at: datetime | None
    matched_criteria: list[dict] = Field(default_factory=list)
    missing_criteria: list[dict] = Field(default_factory=list)
    failed_mandatory_criteria: list[dict] = Field(default_factory=list)
    auto_scored: bool
    scored_at: datetime
    evaluation_method: ATSEvaluationMode = ATSEvaluationMode.weighted
    ai_provider: ATSAIProviderName | None = None
    ai_model: str | None = None
    ai_strengths: list[str] = Field(default_factory=list)
    ai_weaknesses: list[str] = Field(default_factory=list)
    ai_explanation: str | None = None
    ai_fallback_reason: str | None = None

    @property
    def final_recommendation(self) -> ATSRecommendation:
        return self.override_recommendation or self.system_recommendation


class ATSScreenResponse(BaseModel):
    success: bool = True
    message: str = "Application screened."
    data: ATSScreeningResultRead


class ATSScreenAllResponse(BaseModel):
    success: bool = True
    message: str
    screened_count: int
    results: list[ATSScreeningResultRead]
    failed: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Candidate list (career applications enriched with ATS data)
# ---------------------------------------------------------------------------


class CareerApplicationWithATS(CareerApplicationRead):
    screening: ATSScreeningResultRead | None = None


class PaginatedATSApplications(BaseModel):
    meta: PageMeta
    items: list[CareerApplicationWithATS]


# ---------------------------------------------------------------------------
# Recruiter notes & audit trail
# ---------------------------------------------------------------------------


class ATSRecruiterNoteCreate(BaseModel):
    note: str = Field(min_length=1, max_length=5000)


class ATSRecruiterNoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    application_id: str
    admin_id: str
    admin_username: str | None = None
    note: str
    created_at: datetime


class ATSRecruiterNoteResponse(BaseModel):
    success: bool = True
    message: str = "Note added."
    data: ATSRecruiterNoteRead


class ATSAuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    application_id: str
    admin_id: str | None
    admin_username: str | None = None
    action: ATSAuditAction
    details: dict
    created_at: datetime


class ATSOverrideRequest(BaseModel):
    recommendation: ATSRecommendation
    reason: str = Field(min_length=2, max_length=2000)


# ---------------------------------------------------------------------------
# Vetting detail (single-candidate deep dive)
# ---------------------------------------------------------------------------


class ATSVettingDetail(BaseModel):
    application: CareerApplicationRead
    job: JobOpeningRead | None
    screening: ATSScreeningResultRead | None
    notes: list[ATSRecruiterNoteRead]
    history: list[ATSAuditLogRead]


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class ATSStats(BaseModel):
    total_applications: int
    total_screened: int
    total_unscreened: int
    recommended_count: int
    review_count: int
    not_recommended_count: int
    average_score_percentage: float


# ---------------------------------------------------------------------------
# AI providers (status + job draft generation)
# ---------------------------------------------------------------------------


class ATSAIProviderStatus(BaseModel):
    configured: bool
    default_model: str


class ATSAIProvidersResponse(BaseModel):
    providers: dict[str, ATSAIProviderStatus]


class AIJobGenerateRequest(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    provider: ATSAIProviderName
    model: str | None = Field(default=None, max_length=100)


class AIJobDraftData(BaseModel):
    summary: str
    description: str
    responsibilities: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    provider: str
    model: str


class AIJobGenerateResponse(BaseModel):
    success: bool = True
    message: str = "Draft generated — review and edit before publishing."
    data: AIJobDraftData


# ---------------------------------------------------------------------------
# AI screening-criteria suggestion
# ---------------------------------------------------------------------------


class AICriteriaGenerateRequest(BaseModel):
    provider: ATSAIProviderName
    model: str | None = Field(default=None, max_length=100)


class AISuggestedCriterionData(BaseModel):
    category: ATSCriterionCategory
    label: str
    description: str = ""
    match_keywords: list[str] = Field(default_factory=list)
    weight: float = 10.0
    is_required: bool = False


class AICriteriaSuggestionData(BaseModel):
    criteria: list[AISuggestedCriterionData]
    provider: str
    model: str


class AICriteriaGenerateResponse(BaseModel):
    success: bool = True
    message: str = "Suggested criteria generated — review and edit before adding."
    data: AICriteriaSuggestionData
