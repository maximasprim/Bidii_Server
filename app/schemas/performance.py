from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RoleKind = Literal["sales", "appraisal", "hybrid"]
StaffStatus = Literal["active", "probation", "pip", "exited"]
UnitKind = Literal["branch", "team", "head_office"]
ActionType = Literal[
    "talk", "coaching", "verbal_warning", "first_written_warning", "final_warning", "pip", "pip_extended",
    "pip_lifted", "demotion", "dismissal", "probation_confirm", "probation_extend", "promotion", "transfer",
    "exit", "recognition",
]


# ---- Config -------------------------------------------------------------


class ConfigRead(BaseModel):
    config: dict[str, Any]
    version: int


class ConfigUpdate(BaseModel):
    config: dict[str, Any]


class ConfigUpdateResponse(BaseModel):
    success: bool = True
    message: str = "Performance settings saved."
    data: ConfigRead


# ---- Rating scales ------------------------------------------------------


class ScaleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    effective_from: date
    categories: list[dict[str, Any]]
    rating_bands: list[dict[str, Any]]
    rating_to_pi: dict[str, Any]
    note: str | None = None
    created_by: str | None = None
    created_at: datetime
    is_current: bool = False


class ScaleListResponse(BaseModel):
    items: list[ScaleRead]


class ScaleCreate(BaseModel):
    effective_from: date
    categories: list[dict[str, Any]]
    rating_bands: list[dict[str, Any]]
    rating_to_pi: dict[str, Any]
    note: str | None = Field(default=None, max_length=300)


class ScaleWriteResponse(BaseModel):
    success: bool = True
    message: str = "Rating scale saved."
    data: ScaleRead


class ScalePreviewRequest(BaseModel):
    categories: list[dict[str, Any]]
    # Scores to classify under both the current and the candidate thresholds.
    scores: list[float] = Field(default_factory=list, max_length=5000)


class ScalePreviewResponse(BaseModel):
    before: dict[str, int]
    after: dict[str, int]
    moved: int
    total: int


# ---- Performance roles --------------------------------------------------


class PerformanceRoleCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    key: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=2000)
    department: str | None = Field(default=None, max_length=120)
    kind: RoleKind = "appraisal"
    config_overrides: dict[str, Any] | None = None


class PerformanceRoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    department: str | None = Field(default=None, max_length=120)
    kind: RoleKind | None = None
    is_active: bool | None = None
    config_overrides: dict[str, Any] | None = None
    clear_config_overrides: bool = False


class PerformanceRoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    key: str
    name: str
    description: str | None = None
    department: str | None = None
    kind: str
    is_active: bool
    is_system: bool
    config_overrides: dict[str, Any] | None = None
    staff_count: int = 0
    # Highest saved JD version (None until a JD exists) and whether its KPI weights total 100,
    # i.e. whether this role can be used for scoring yet.
    current_version: int | None = None
    kpi_count: int = 0
    configured: bool = False
    created_at: datetime


class PerformanceRoleListResponse(BaseModel):
    items: list[PerformanceRoleRead]


class PerformanceRoleWriteResponse(BaseModel):
    success: bool = True
    message: str = "Saved."
    data: PerformanceRoleRead


# ---- Units & regions ----------------------------------------------------


class UnitCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    kind: UnitKind = "branch"
    branch_id: str | None = None
    manager_profile_id: str | None = None
    parent_unit_id: str | None = None  # a team under a branch (for example its Debt Collection unit)


class UnitUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    kind: UnitKind | None = None
    branch_id: str | None = None
    manager_profile_id: str | None = None
    is_active: bool | None = None


class UnitRead(BaseModel):
    id: str
    name: str
    kind: str
    branch_id: str | None = None
    manager_profile_id: str | None = None
    parent_unit_id: str | None = None
    region_id: str | None = None
    region_name: str | None = None
    is_active: bool


class UnitListResponse(BaseModel):
    items: list[UnitRead]


class UnitWriteResponse(BaseModel):
    success: bool = True
    message: str = "Saved."
    data: UnitRead


class RegionCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    manager_profile_id: str | None = None


class RegionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    manager_profile_id: str | None = None
    is_active: bool | None = None


class RegionRead(BaseModel):
    id: str
    name: str
    manager_profile_id: str | None = None
    is_active: bool
    unit_count: int = 0


class RegionListResponse(BaseModel):
    items: list[RegionRead]


class RegionWriteResponse(BaseModel):
    success: bool = True
    message: str = "Saved."
    data: RegionRead


class UnitRegionAssign(BaseModel):
    # None removes the unit from any region as of effective_from.
    region_id: str | None = None
    effective_from: date | None = None


# ---- Staff profiles -----------------------------------------------------


class NewLogin(BaseModel):
    """Create the dashboard login together with the staff profile (administrators only)."""

    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=8, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    role: str = Field(default="staff", max_length=20)


class StaffProfileCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=150)
    performance_role_id: str
    admin_user_id: str | None = None
    employee_no: str | None = Field(default=None, max_length=40)
    unit_id: str | None = None
    manager_id: str | None = None
    level_id: str | None = None
    hire_date: date | None = None
    probation_end_date: date | None = None
    status: StaffStatus = "active"
    is_executive: bool = False
    login: NewLogin | None = None


class StaffProfileUpdate(BaseModel):
    """All optional - only what's provided changes. Send an explicit null to clear a nullable link."""

    full_name: str | None = Field(default=None, min_length=2, max_length=150)
    performance_role_id: str | None = None
    admin_user_id: str | None = None
    employee_no: str | None = Field(default=None, max_length=40)
    unit_id: str | None = None
    manager_id: str | None = None
    level_id: str | None = None
    hire_date: date | None = None
    probation_end_date: date | None = None
    status: StaffStatus | None = None
    is_executive: bool | None = None
    is_active: bool | None = None
    # Why a unit/manager/role changed (kept in the assignment history).
    change_reason: str | None = Field(default=None, max_length=200)


class StaffProfileRead(BaseModel):
    id: str
    full_name: str
    employee_no: str | None = None
    performance_role_id: str
    performance_role_name: str | None = None
    performance_role_kind: str | None = None
    admin_user_id: str | None = None
    username: str | None = None
    unit_id: str | None = None
    unit_name: str | None = None
    manager_id: str | None = None
    manager_name: str | None = None
    level_id: str | None = None
    level_label: str | None = None
    hire_date: date | None = None
    probation_end_date: date | None = None
    status: str
    is_executive: bool = False
    is_active: bool
    created_at: datetime


class StaffProfileListResponse(BaseModel):
    items: list[StaffProfileRead]
    total: int


class StaffProfileWriteResponse(BaseModel):
    success: bool = True
    message: str = "Saved."
    data: StaffProfileRead


class LinkableUser(BaseModel):
    id: str
    username: str
    role: str
    branch_id: str | None = None


class LinkableUsersResponse(BaseModel):
    items: list[LinkableUser]


class AssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    unit_id: str | None = None
    manager_id: str | None = None
    performance_role_id: str | None = None
    effective_from: date
    effective_to: date | None = None
    reason: str | None = None


class AssignmentListResponse(BaseModel):
    items: list[AssignmentRead]


# ---- Actions ------------------------------------------------------------


class ActionCreate(BaseModel):
    action_type: ActionType
    started_on: date
    review_on: date | None = None
    ends_on: date | None = None
    outcome: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=4000)


class ActionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    profile_id: str
    action_type: str
    restricted: bool
    started_on: date
    review_on: date | None = None
    ends_on: date | None = None
    outcome: str | None = None
    note: str | None = None
    source: str
    raised_by_username: str | None = None
    created_at: datetime


class ActionListResponse(BaseModel):
    items: list[ActionRead]


class ActionWriteResponse(BaseModel):
    success: bool = True
    message: str = "Saved."
    data: ActionRead


# ---- Me -----------------------------------------------------------------


class MyPerformanceResponse(BaseModel):
    """Who the caller is, how they're measured, and what the team menu can show them."""

    profile: StaffProfileRead | None = None
    categories: list[dict[str, Any]]
    rating_bands: list[dict[str, Any]]
    # People other than the caller they may see; the nav uses this to hide an empty Team page.
    team_size: int = 0
    sees_everyone: bool = False
    read_only: bool = False


# ---- Audit --------------------------------------------------------------


class AuditEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    actor_username: str
    action: str
    entity_type: str
    entity_id: str | None = None
    details: dict[str, Any] | None = None
    created_at: datetime


class AuditListResponse(BaseModel):
    items: list[AuditEntry]


# ---- Job descriptions, KPIs, versions -------------------------------------------


class JDOptions(BaseModel):
    kpi_units: list[str]
    kpi_directions: list[str]
    kpi_sources: list[str]
    kpi_frequencies: list[str]
    signoff_steps: list[str]
    default_competencies: list[dict[str, str]]


class KpiRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    label: str
    description: str | None = None
    unit: str
    direction: str
    weight: float
    source: str
    frequency: str
    cap_pct: float | None = None
    jd_section: str | None = None
    sort_order: int = 0


class RoleVersionRead(BaseModel):
    role_id: str
    role_key: str
    role_name: str
    # 0 means "this role has no saved job description yet": jd/kpis hold a blank starting point.
    version: int
    is_latest: bool
    locked: bool
    note: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    jd: dict[str, Any]
    kpis: list[KpiRead]
    signoff_chain: list[str]
    effective_signoff_chain: list[str]
    requires_ceo_signoff: bool
    weights: dict[str, float]
    configured: bool
    created_new_version: bool = False


class RoleVersionSave(BaseModel):
    jd: dict[str, Any]
    kpis: list[dict[str, Any]] = Field(default_factory=list, max_length=25)
    signoff_chain: list[str] = Field(default_factory=lambda: ["staff", "manager", "rm_gm", "hr"])
    requires_ceo_signoff: bool = False
    note: str | None = Field(default=None, max_length=300)


class VersionSummary(BaseModel):
    version: int
    is_latest: bool
    locked: bool
    note: str | None = None
    created_by: str | None = None
    created_at: datetime
    kpi_count: int


class VersionListResponse(BaseModel):
    items: list[VersionSummary]


class AIMeta(BaseModel):
    provider: str
    model: str
    prompt_version: str
    cached: bool
    label: str


class JDExtractResponse(BaseModel):
    jd: dict[str, Any]
    warnings: list[str]
    ai: AIMeta


class KpiSuggestRequest(BaseModel):
    responsibilities: list[dict[str, Any]] = Field(max_length=15)
    regenerate: bool = False


class KpiSuggestResponse(BaseModel):
    kpis: list[dict[str, Any]]
    ai_used: bool
    ai: AIMeta | None = None
    fallback_reason: str | None = None
    warnings: list[str]


# ---- Org structure ---------------------------------------------------------------------


class OrgNode(BaseModel):
    id: str
    full_name: str
    role_name: str | None = None
    level_label: str | None = None
    unit_name: str | None = None
    status: str
    is_active: bool
    direct_reports: int = 0
    total_reports: int = 0
    children: list["OrgNode"] = Field(default_factory=list)


OrgNode.model_rebuild()


class OrgTreeResponse(BaseModel):
    roots: list[OrgNode]
    total: int


class OrgIssue(BaseModel):
    code: str
    severity: Literal["error", "warning", "info"]
    message: str
    profile_id: str | None = None
    unit_id: str | None = None
    region_id: str | None = None
    role_id: str | None = None


class OrgHealthResponse(BaseModel):
    issues: list[OrgIssue]
    counts: dict[str, int]


class SetupBranch(BaseModel):
    branch_id: str
    name: str
    unit_id: str | None = None
    proposed_region: str | None = None


class SetupRegion(BaseModel):
    name: str
    branch_ids: list[str]
    exists: bool = False


class SetupExtraUnit(BaseModel):
    name: str
    kind: str
    exists: bool


class SetupPreview(BaseModel):
    branches: list[SetupBranch]
    regions: list[SetupRegion]
    extra_units: list[SetupExtraUnit]
    unmatched: list[str]


class SetupApplyRequest(BaseModel):
    regions: list[SetupRegion] = Field(max_length=20)
    effective_from: date | None = None


class SetupApplyResponse(BaseModel):
    units_created: int
    units_linked: int
    regions_created: int
    links_created: int
    conflicts: list[str]


# ---- Levels ----------------------------------------------------------------------------


class LevelCreate(BaseModel):
    role_id: str
    label: str = Field(min_length=1, max_length=80)
    rank: int = Field(default=1, ge=1, le=20)
    default_targets: dict[str, float] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=300)


class LevelUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    rank: int | None = Field(default=None, ge=1, le=20)
    default_targets: dict[str, float] | None = None
    note: str | None = Field(default=None, max_length=300)
    is_active: bool | None = None


class LevelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    role_id: str
    key: str
    label: str
    rank: int
    default_targets: dict[str, float]
    note: str | None = None
    is_active: bool
    staff_count: int = 0


class LevelListResponse(BaseModel):
    items: list[LevelRead]


class LevelWriteResponse(BaseModel):
    success: bool = True
    message: str = "Saved."
    data: LevelRead


# ---- Targets ---------------------------------------------------------------------------


class TargetRow(BaseModel):
    kpi_key: str
    label: str
    unit: str
    direction: str
    value: float | None = None  # None: no target in force (the KPI is not scored)
    effective_from: date | None = None
    source: str | None = None
    reason: str | None = None


class TargetHistoryRow(BaseModel):
    id: str
    kpi_key: str
    effective_from: date
    value: float
    source: str
    reason: str
    set_by_username: str | None = None
    created_at: datetime


class StaffTargetsResponse(BaseModel):
    profile_id: str
    month: date
    items: list[TargetRow]
    history: list[TargetHistoryRow]


class TargetSet(BaseModel):
    kpi_key: str = Field(min_length=1, max_length=60)
    effective_from: date
    value: float
    reason: str = Field(max_length=300)


class TargetSetResponse(BaseModel):
    changed: bool
    message: str
    data: StaffTargetsResponse


class TargetGridRow(BaseModel):
    profile_id: str
    full_name: str
    unit_name: str | None = None
    role_name: str | None = None
    level_label: str | None = None
    value: float | None = None
    effective_from: date | None = None
    source: str | None = None
    has_kpi: bool = True


class TargetGridResponse(BaseModel):
    kpi_key: str
    month: date
    items: list[TargetGridRow]


BulkMode = Literal["set_value", "level_defaults", "copy_forward"]


class BulkTargetRequest(BaseModel):
    mode: BulkMode
    effective_from: date
    reason: str = Field(max_length=300)
    dry_run: bool = True
    role_id: str | None = None
    unit_id: str | None = None
    region_id: str | None = None
    level_id: str | None = None
    profile_ids: list[str] | None = Field(default=None, max_length=1000)
    kpi_key: str | None = Field(default=None, max_length=60)
    value: float | None = None  # set_value
    overwrite: bool = False  # level_defaults: replace targets that already exist
    source_month: date | None = None  # copy_forward
    uplift_pct: float = 0  # copy_forward


class PlanItemRead(BaseModel):
    profile_id: str
    full_name: str
    kpi_key: str | None = None
    current: float | None = None
    new: float | None = None
    action: Literal["create", "unchanged", "skip"]
    note: str | None = None


class BulkTargetResponse(BaseModel):
    dry_run: bool
    applied: int
    summary: dict[str, int]
    items: list[PlanItemRead]


class TargetRequestCreate(BaseModel):
    kpi_key: str = Field(min_length=1, max_length=60)
    effective_from: date
    value: float
    reason: str = Field(max_length=300)


class TargetRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    profile_id: str
    profile_name: str | None = None
    kpi_key: str
    effective_from: date
    proposed_value: float
    current_value: float | None = None
    reason: str
    requested_by_username: str
    status: str
    decided_by_username: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    created_at: datetime
    # Computed for the caller by the server, so the UI never has to guess what the four-eyes rule allows.
    is_mine: bool = False
    can_decide: bool = False


class TargetRequestListResponse(BaseModel):
    items: list[TargetRequestRead]


class TargetRequestWriteResponse(BaseModel):
    success: bool = True
    message: str = "Saved."
    data: TargetRequestRead


class TargetRequestDecision(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=300)


# ---- Monthly entries -------------------------------------------------------------------


class EntryWindow(BaseModel):
    state: Literal["open", "closed", "future"]
    closes_on: date | None = None


class EntrySuggestion(BaseModel):
    count: int
    total_amount: float
    note: str


class EntryRow(BaseModel):
    kpi_key: str
    label: str
    unit: str
    direction: str
    entry_id: str | None = None
    value: float | None = None
    status: str | None = None
    note: str | None = None
    rejection_reason: str | None = None
    target: float | None = None
    verified_value: float | None = None
    achievement_pct: float | None = None  # from VERIFIED figures only
    pending_verification: bool = False
    evidence_name: str | None = None
    suggestion: EntrySuggestion | None = None


class EntryMonthResponse(BaseModel):
    profile_id: str
    month: date
    window: EntryWindow
    rows: list[EntryRow]
    can_enter: bool
    can_verify: bool


class EntrySave(BaseModel):
    kpi_key: str = Field(min_length=1, max_length=60)
    month: date
    value: float
    note: str | None = Field(default=None, max_length=300)


class EntrySubmit(BaseModel):
    month: date


class EntryWriteResponse(BaseModel):
    message: str
    data: EntryMonthResponse


class QueueItem(BaseModel):
    entry_id: str
    profile_id: str
    profile_name: str
    unit_name: str | None = None
    kpi_key: str
    label: str
    unit: str
    month: date
    value: float
    target: float | None = None
    entered_by_username: str | None = None
    submitted_at: datetime | None = None


class QueueResponse(BaseModel):
    items: list[QueueItem]


class EntryDecision(BaseModel):
    entry_ids: list[str] = Field(min_length=1, max_length=200)
    approve: bool
    reason: str | None = Field(default=None, max_length=300)


class EntryDecisionResponse(BaseModel):
    updated: int
    failed: list[dict[str, str]]


class EntryReopen(BaseModel):
    reason: str = Field(max_length=300)


# ---- Appraisals ------------------------------------------------------------------------


class AppraisalCreate(BaseModel):
    period: str = Field(min_length=4, max_length=10)


class AppraisalFormSave(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    competencies: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    objectives: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    development_needs: list[dict[str, Any]] = Field(default_factory=list, max_length=2)


class AppraisalSign(BaseModel):
    decision: Literal["sign", "return"] = "sign"
    comment: str | None = Field(default=None, max_length=3000)


class DisputeCreate(BaseModel):
    item_key: str | None = Field(default=None, max_length=60)
    reason: str = Field(max_length=2000)


class DisputeResolve(BaseModel):
    resolution: str = Field(max_length=2000)
    new_manager_rating: int | None = None


class AppraisalReopen(BaseModel):
    reason: str = Field(max_length=300)


class ChainStepRead(BaseModel):
    step: str
    label: str
    state: Literal["done", "current", "upcoming"]
    signed_by: str | None = None
    signed_at: datetime | None = None
    comment: str | None = None


class AppraisalItemRead(BaseModel):
    kpi_key: str
    label: str
    weight: float
    criteria: str | None = None
    self_text: str | None = None
    self_rating: int | None = None
    manager_rating: int | None = None
    final_rating: float | None = None
    self_weighted: float | None = None  # weight x self rating / 100 (the form's "Weighted Rating")
    manager_weighted: float | None = None


class AppraisalCompetencyRead(BaseModel):
    key: str
    title: str
    self_rating: int | None = None
    manager_rating: int | None = None


class AppraisalObjectiveRead(BaseModel):
    label: str
    values: list[str]


class AppraisalNeedRead(BaseModel):
    slot: int
    text: str | None = None


class AppraisalSignoffRead(BaseModel):
    step: str
    label: str
    decision: str
    comment: str | None = None
    actor_username: str
    signed_at: datetime


class AppraisalDisputeRead(BaseModel):
    id: str
    item_key: str | None = None
    reason: str
    raised_by_username: str
    status: str
    resolution: str | None = None
    new_manager_rating: int | None = None
    resolved_by_username: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class AppraisalPermissions(BaseModel):
    can_edit: bool = False
    editable: list[str] = Field(default_factory=list)
    can_submit: bool = False
    can_sign: bool = False
    can_return: bool = False
    can_dispute: bool = False
    can_resolve: bool = False
    can_acknowledge: bool = False
    can_reopen: bool = False


class AppraisalWho(BaseModel):
    id: str
    full_name: str
    role_name: str | None = None
    unit_name: str | None = None
    manager_name: str | None = None


class AppraisalDetail(BaseModel):
    id: str
    profile: AppraisalWho
    period_key: str
    period_label: str
    period_start: date
    period_end: date
    status: str
    current_step: str | None = None
    current_step_label: str | None = None
    chain: list[ChainStepRead]
    items: list[AppraisalItemRead]
    competencies: list[AppraisalCompetencyRead]
    objective_months: list[str]
    objectives: list[AppraisalObjectiveRead]
    development_needs: list[AppraisalNeedRead]
    signoffs: list[AppraisalSignoffRead]
    disputes: list[AppraisalDisputeRead]
    self_weighted_total: float | None = None
    manager_weighted_total: float | None = None
    final_weighted_rating: float | None = None
    acknowledged_at: datetime | None = None
    acknowledged_by_username: str | None = None
    source: str
    extraction_status: str
    permissions: AppraisalPermissions


class AppraisalWriteResponse(BaseModel):
    message: str
    data: AppraisalDetail


class AppraisalSummary(BaseModel):
    id: str
    profile_id: str
    profile_name: str
    period_key: str
    period_label: str
    status: str
    current_step: str | None = None
    current_step_label: str | None = None
    awaiting_me: bool = False
    open_disputes: int = 0
    final_weighted_rating: float | None = None


class AppraisalListResponse(BaseModel):
    items: list[AppraisalSummary]
