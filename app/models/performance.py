"""
Staff Performance module - data model (foundations).

Design notes
------------
* A "performance role" (job family: Loan Officer, Credit Risk Analyst, ...) is
  deliberately SEPARATE from the dashboard access role in admin_users.role.
  Access roles decide which menus someone sees; performance roles decide how
  someone is measured.
* Status/kind columns are plain Strings validated in Python (never native
  Enums), so no ALTER TYPE workarounds are ever needed on Postgres.
* Every table here is NEW - nothing existing is altered.
* The only foreign key into admin_users is staff_profiles.admin_user_id
  (ON DELETE SET NULL), so the existing admin hard-delete flow cannot hit a
  foreign-key conflict from this module. Other "who did it" fields are plain
  id strings plus a username snapshot.
* manager_profile_id columns on units/regions are plain strings, not foreign
  keys: units/regions and staff_profiles would otherwise reference each other
  in a cycle. Integrity is checked in code.
* performance_units are NOT the public `branches` table (which feeds the
  website). They let the tracker hold places that have no public branch
  (Check-off, Head Office, closed branches) without publishing anything.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class PerformanceRole(Base):
    """A job family that can be measured (Loan Officer, Credit Risk Analyst, ...)."""

    __tablename__ = "performance_roles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    department: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # sales | appraisal | hybrid
    kind: Mapped[str] = mapped_column(String(20), default="appraisal")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Seeded roles can be edited/deactivated but never hard-deleted.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    # Optional per-role override of the org-wide settings (any valid subset).
    config_overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class PerformanceConfig(Base):
    """Org-wide settings as one JSON document under the key "org"; `version` bumps on every save."""

    __tablename__ = "performance_config"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(default=1)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class PerformanceConfigHistory(Base):
    """Append-only copy of every saved config version, so a score can cite the exact settings used."""

    __tablename__ = "performance_config_history"
    __table_args__ = (Index("ix_perf_cfg_hist_key_version", "key", "version", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String(40))
    version: Mapped[int]
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    changed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RatingScale(Base):
    """
    Versioned, effective-dated category thresholds + rating maps. Rows are
    immutable once written: changing a threshold creates a NEW row with a later
    effective_from, so a re-run of a closed period always uses the scale that
    was in force then.
    """

    __tablename__ = "rating_scales"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    effective_from: Mapped[date] = mapped_column(Date, unique=True, index=True)
    # [{key,label,min_score}] highest -> lowest (Above/Meeting/Approaching/Below)
    categories: Mapped[list] = mapped_column(JSON)
    # [{rating,label,min_pct}] 5 -> 1: achievement % -> appraisal rating
    rating_bands: Mapped[list] = mapped_column(JSON)
    # {"5":125,...}: manager/self 1-5 rating -> performance index
    rating_to_pi: Mapped[dict] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PerformanceUnit(Base):
    """A place staff are measured in: a branch, a team (Check-off) or Head Office. A team can sit under a branch (each branch's Debt Collection)."""

    __tablename__ = "performance_units"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default="branch")  # branch | team | head_office
    # Optional link to the public branches table (never created from here).
    branch_id: Mapped[str | None] = mapped_column(ForeignKey("branches.id"), nullable=True, index=True)
    manager_profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # The unit this one sits under (a branch's Debt Collection unit has the branch as its parent). One level only.
    parent_unit_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Region(Base):
    __tablename__ = "regions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    manager_profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UnitRegionLink(Base):
    """Which region a unit belonged to over time (reorganisations don't rewrite history)."""

    __tablename__ = "unit_region_links"
    __table_args__ = (Index("ix_unit_region_unit_from", "unit_id", "effective_from"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    unit_id: Mapped[str] = mapped_column(ForeignKey("performance_units.id"))
    region_id: Mapped[str] = mapped_column(ForeignKey("regions.id"), index=True)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)


class PerformanceLevel(Base):
    """
    A seniority tier within a performance role (e.g. Loan Officer: DSE1, DSE2, SNR DSE). Each carries the
    default monthly targets for that tier, keyed by KPI key. Defaults are only ever APPLIED explicitly
    (bulk "apply level defaults"); changing a level never silently changes anyone's target.
    """

    __tablename__ = "performance_levels"
    __table_args__ = (Index("ix_level_role_key", "role_id", "key", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    role_id: Mapped[str] = mapped_column(ForeignKey("performance_roles.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(80))
    rank: Mapped[int] = mapped_column(default=1)  # 1 = most junior
    default_targets: Mapped[dict] = mapped_column(JSON, default=dict)  # {kpi_key: number}
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class StaffProfile(Base):
    """
    Links a person to a performance role, unit and line manager. The login is
    optional: staff in the Excel tracker with no dashboard account can still be
    tracked and linked later; deleting a login keeps the profile and history.
    """

    __tablename__ = "staff_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    admin_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True, unique=True, index=True
    )
    full_name: Mapped[str] = mapped_column(String(150), index=True)
    employee_no: Mapped[str | None] = mapped_column(String(40), nullable=True)
    performance_role_id: Mapped[str] = mapped_column(ForeignKey("performance_roles.id"), index=True)
    unit_id: Mapped[str | None] = mapped_column(ForeignKey("performance_units.id"), nullable=True, index=True)
    manager_id: Mapped[str | None] = mapped_column(
        ForeignKey("staff_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # DSE1 / DSE2 / SNR DSE, Auto Champion tiers, ... (a level belongs to one performance role).
    level_id: Mapped[str | None] = mapped_column(ForeignKey("performance_levels.id"), nullable=True, index=True)
    hire_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    probation_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # active | probation | pip | exited. "pip" is only ever shown to people
    # allowed to see restricted actions - everyone else sees "active".
    status: Mapped[str] = mapped_column(String(20), default="active")
    # CEO-style read-only organisation-wide view.
    is_executive: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class StaffAssignmentHistory(Base):
    """Unit/manager/role over time, so transfers attribute results to the right place per period."""

    __tablename__ = "staff_assignment_history"
    __table_args__ = (Index("ix_assignment_profile_from", "profile_id", "effective_from"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("staff_profiles.id", ondelete="CASCADE"))
    unit_id: Mapped[str | None] = mapped_column(ForeignKey("performance_units.id"), nullable=True, index=True)
    manager_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    performance_role_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class StaffUnitCoverage(Base):
    """Units someone works in besides their main one (a debt collector who covers several branches). The main unit stays on the profile."""

    __tablename__ = "staff_unit_coverage"
    __table_args__ = (Index("ix_coverage_profile_unit", "profile_id", "unit_id", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("staff_profiles.id", ondelete="CASCADE"))
    unit_id: Mapped[str] = mapped_column(ForeignKey("performance_units.id"), index=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PerformanceAction(Base):
    """Talk / coaching / warning / PIP / probation / promotion / transfer / exit / recognition events."""

    __tablename__ = "performance_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("staff_profiles.id", ondelete="CASCADE"), index=True)
    action_type: Mapped[str] = mapped_column(String(30))
    # True for PIP / warning / disciplinary-type actions: visible only to HR,
    # the direct manager and the staff member concerned.
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    started_on: Mapped[date] = mapped_column(Date)
    review_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    ends_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | import
    raised_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PerformanceAuditLog(Base):
    """Append-only record of every sensitive action in the module (no IP addresses)."""

    __tablename__ = "performance_audit_log"
    __table_args__ = (Index("ix_perf_audit_entity", "entity_type", "entity_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    actor_username: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(60))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class PerformanceRoleVersion(Base):
    """
    One immutable-once-used version of a job role's JD, KPI set and sign-off chain.
    Edits to a version that has been used in a scored period (locked_at set)
    create the NEXT version instead, so old periods keep the definition they
    were scored with. The current version of a role is simply its highest number.
    """

    __tablename__ = "performance_role_versions"
    __table_args__ = (Index("ix_role_version_unique", "role_id", "version", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    role_id: Mapped[str] = mapped_column(ForeignKey("performance_roles.id", ondelete="CASCADE"))
    version: Mapped[int]
    # title, reports_to, branch, department, grade, date, purpose, responsibilities[{title,
    # weight_pct, description, criteria[]}], mandates, planning, relationships, qualifications[],
    # competencies[{key,title,description}], approvals[], objective_rows[]
    jd: Mapped[dict] = mapped_column(JSON, default=dict)
    # Steps between the staff member and the end, from staff/manager/csm/rm_gm/hr. "ceo" is
    # appended automatically when requires_ceo_signoff is true.
    signoff_chain: Mapped[list] = mapped_column(JSON, default=list)
    requires_ceo_signoff: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PerformanceKpi(Base):
    __tablename__ = "performance_kpis"
    __table_args__ = (Index("ix_kpi_version_key", "role_version_id", "key", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    role_version_id: Mapped[str] = mapped_column(
        ForeignKey("performance_role_versions.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(60))
    label: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str] = mapped_column(String(20))  # kes | count | percent | rating | days | score
    direction: Mapped[str] = mapped_column(String(10), default="higher")  # higher | lower
    weight: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    # kpi_entry | manual | appraisal_rating | computed | imported
    source: Mapped[str] = mapped_column(String(30), default="appraisal_rating")
    frequency: Mapped[str] = mapped_column(String(15), default="quarterly")  # monthly | quarterly | annual
    cap_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    jd_section: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sort_order: Mapped[int] = mapped_column(default=0)


class AIOutput(Base):
    """Every AI output the module produces: what model made it, from which prompt, for which (hashed) input."""

    __tablename__ = "ai_outputs"
    __table_args__ = (Index("ix_ai_outputs_kind_hash", "kind", "input_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(40))
    input_hash: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40))
    output: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PerformanceTarget(Base):
    """
    Effective-dated, append-only targets. A row says "from this month on, this person's target for this KPI is
    `value`". The target IN FORCE for a month is the row with the latest effective_from not after it (ties:
    the most recently created). Nothing is ever updated or deleted, so a mid-year change ("contract change")
    never rewrites earlier months and every change keeps its reason.
    """

    __tablename__ = "performance_targets"
    __table_args__ = (Index("ix_target_profile_kpi_from", "profile_id", "kpi_key", "effective_from"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("staff_profiles.id", ondelete="CASCADE"))
    kpi_key: Mapped[str] = mapped_column(String(60))
    effective_from: Mapped[date] = mapped_column(Date)  # always the first day of a month
    value: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    # manual | level_default | bulk | copy_forward | request | import
    source: Mapped[str] = mapped_column(String(20), default="manual")
    reason: Mapped[str] = mapped_column(String(300))
    set_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TargetChangeRequest(Base):
    """A manager's proposal to change a target; HR/admin approve (creating a PerformanceTarget row) or reject."""

    __tablename__ = "target_change_requests"
    __table_args__ = (Index("ix_target_request_status", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("staff_profiles.id", ondelete="CASCADE"), index=True)
    kpi_key: Mapped[str] = mapped_column(String(60))
    effective_from: Mapped[date] = mapped_column(Date)
    proposed_value: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    reason: Mapped[str] = mapped_column(String(300))
    requested_by_username: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(15), default="pending")  # pending | approved | rejected | withdrawn
    decided_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class KpiEntry(Base):
    """
    One monthly figure for one KPI of one person (sales disbursed, new customers, PAR, retention, ...).
    Flow: draft -> submitted -> verified | rejected (rejected goes back to draft when edited). ONLY verified
    entries feed scores; everything else shows as "pending verification".
    """

    __tablename__ = "kpi_entries"
    __table_args__ = (
        Index("ix_kpi_entry_unique", "profile_id", "kpi_key", "month", unique=True),
        Index("ix_kpi_entry_month_kpi", "month", "kpi_key"),
        Index("ix_kpi_entry_status", "status", "month"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("staff_profiles.id", ondelete="CASCADE"))
    kpi_key: Mapped[str] = mapped_column(String(60))
    month: Mapped[date] = mapped_column(Date)  # first day of the month
    value: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    status: Mapped[str] = mapped_column(String(15), default="draft")  # draft | submitted | verified | rejected | locked
    source: Mapped[str] = mapped_column(String(15), default="manual")  # manual | import
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    entered_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Optional evidence (private storage path under performance/...). Added with uploads; planned now.
    evidence_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    evidence_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Appraisal(Base):
    """
    A quarterly (or half-year / annual) appraisal. Its KPI rows, competencies, weights and sign-off chain are
    SNAPSHOTS taken when it is created, so editing the job role later never changes an appraisal in flight.
    status: draft (the staff member is filling it) -> in_review (current_step says who is up) -> finalised.
    """

    __tablename__ = "appraisals"
    __table_args__ = (Index("ix_appraisal_unique", "profile_id", "period_key", unique=True), Index("ix_appraisal_status", "status"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("staff_profiles.id", ondelete="CASCADE"), index=True)
    period_key: Mapped[str] = mapped_column(String(10))  # 2026-Q1 | 2026-H1 | 2026
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    role_id: Mapped[str] = mapped_column(String(36))
    role_version_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(15), default="draft")
    current_step: Mapped[str | None] = mapped_column(String(15), nullable=True)
    signoff_chain: Mapped[list] = mapped_column(JSON, default=list)  # snapshot, e.g. ["staff","manager","rm_gm","hr"]
    final_weighted_rating: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    created_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    finalised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Filled-form upload + AI extraction (added in the upload half of this phase; planned now).
    source: Mapped[str] = mapped_column(String(10), default="digital")  # digital | upload
    uploaded_file_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    uploaded_original_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    extraction_status: Mapped[str] = mapped_column(String(12), default="none")  # none | extracted | confirmed | failed
    extraction: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AppraisalItem(Base):
    """A KPI row on the form: Job Objective | % Weight | Measurement Criteria | Self Assessment | Rating | Manager Assessment."""

    __tablename__ = "appraisal_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    appraisal_id: Mapped[str] = mapped_column(ForeignKey("appraisals.id", ondelete="CASCADE"), index=True)
    kpi_key: Mapped[str] = mapped_column(String(60))
    label: Mapped[str] = mapped_column(String(200))
    weight: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    self_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    self_rating: Mapped[int | None] = mapped_column(nullable=True)
    manager_rating: Mapped[int | None] = mapped_column(nullable=True)
    final_rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 1), nullable=True)
    sort_order: Mapped[int] = mapped_column(default=0)


class CompetencyRating(Base):
    __tablename__ = "competency_ratings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    appraisal_id: Mapped[str] = mapped_column(ForeignKey("appraisals.id", ondelete="CASCADE"), index=True)
    competency_key: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(150))
    self_rating: Mapped[int | None] = mapped_column(nullable=True)
    manager_rating: Mapped[int | None] = mapped_column(nullable=True)
    sort_order: Mapped[int] = mapped_column(default=0)


class AppraisalObjective(Base):
    """Performance objectives for the next three months (e.g. New customers / Sales / Quality (PAR) by month)."""

    __tablename__ = "appraisal_objectives"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    appraisal_id: Mapped[str] = mapped_column(ForeignKey("appraisals.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    values: Mapped[list] = mapped_column(JSON, default=list)  # three strings, one per month
    sort_order: Mapped[int] = mapped_column(default=0)


class DevelopmentNeed(Base):
    """The two training and development needs on the form."""

    __tablename__ = "development_needs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    appraisal_id: Mapped[str] = mapped_column(ForeignKey("appraisals.id", ondelete="CASCADE"), index=True)
    slot: Mapped[int]  # 1 or 2
    text: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppraisalSignoff(Base):
    """Append-only: who signed (or returned) which step, with their comment."""

    __tablename__ = "appraisal_signoffs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    appraisal_id: Mapped[str] = mapped_column(ForeignKey("appraisals.id", ondelete="CASCADE"), index=True)
    step: Mapped[str] = mapped_column(String(15))
    decision: Mapped[str] = mapped_column(String(10))  # signed | returned
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_username: Mapped[str] = mapped_column(String(100))
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AppraisalDispute(Base):
    __tablename__ = "appraisal_disputes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    appraisal_id: Mapped[str] = mapped_column(ForeignKey("appraisals.id", ondelete="CASCADE"), index=True)
    item_key: Mapped[str | None] = mapped_column(String(60), nullable=True)  # a KPI key, or null for the overall rating
    reason: Mapped[str] = mapped_column(Text)
    raised_by_username: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | resolved
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_manager_rating: Mapped[int | None] = mapped_column(nullable=True)
    resolved_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PerformanceImport(Base):
    """
    One run of the workbook import wizard. The parsed workbook is kept (as JSON, in the database - never as a file)
    so the admin can adjust sheet->unit and name-matching decisions and re-run the dry run before anything is written.
    status: previewed -> committed -> undone (or discarded).
    """

    __tablename__ = "performance_imports"
    __table_args__ = (Index("ix_perf_import_status", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(30), default="workbook_2026")
    original_name: Mapped[str] = mapped_column(String(150))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(12), default="previewed")
    parsed: Mapped[dict] = mapped_column(JSON, default=dict)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    decisions: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by_username: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    committed_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ImportRow(Base):
    """What a committed import did to one record, with enough of the previous state to undo it."""

    __tablename__ = "import_rows"
    __table_args__ = (Index("ix_import_rows_batch", "import_id", "entity_type"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    import_id: Mapped[str] = mapped_column(ForeignKey("performance_imports.id", ondelete="CASCADE"))
    entity_type: Mapped[str] = mapped_column(String(30))  # unit | profile | assignment | target | entry | action
    entity_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(10))  # created | updated
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ScoreSnapshot(Base):
    """
    A stored result for one person and one period, written by the recompute job / when a period is closed.
    Readers never depend on it (reports are computed live from verified figures); it exists so the history of what
    was reported, and under which rating scale, survives later edits. Unique per (profile, period_key).
    """

    __tablename__ = "score_snapshots"
    __table_args__ = (Index("ix_score_snapshot_unique", "profile_id", "period_key", unique=True), Index("ix_score_snapshot_period", "period_key"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("staff_profiles.id", ondelete="CASCADE"))
    unit_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    period_key: Mapped[str] = mapped_column(String(30))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    category_key: Mapped[str | None] = mapped_column(String(30), nullable=True)
    rating: Mapped[int | None] = mapped_column(nullable=True)
    legacy_band: Mapped[str | None] = mapped_column(String(20), nullable=True)
    achievement_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 2), nullable=True)
    actual_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    target_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    coverage_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    scale_effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # per-KPI result as reported
    computed_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Recommendation(Base):
    """
    A suggestion from the rules engine (or its AI advice). Never changes a rating, status or PIP; people acknowledge, action or dismiss it.
    One per (entity, rule, month it was generated); a dismissed one is not recreated that month. `restricted` ones (PIP suggestions) are
    visible only to HR, the direct manager and the person concerned.
    """

    __tablename__ = "recommendations"
    __table_args__ = (
        Index("ix_reco_unique", "entity_type", "entity_id", "rule_key", "period_key", unique=True),
        Index("ix_reco_status", "status", "entity_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    entity_type: Mapped[str] = mapped_column(String(10))  # staff | unit
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    period_key: Mapped[str] = mapped_column(String(10))  # the month it was generated, e.g. 2026-11
    rule_key: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(6), default="rule")  # rule | ai
    severity: Mapped[str] = mapped_column(String(10), default="action")  # info | action | urgent
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    audience: Mapped[list] = mapped_column(JSON, default=list)
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[dict] = mapped_column(JSON, default=dict)  # detail, figures, optional AI advice
    status: Mapped[str] = mapped_column(String(12), default="new")  # new | acknowledged | actioned | dismissed
    status_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    acted_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assignee_profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    baseline: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # score / achievement when raised
    outcome: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # filled later: did it work?
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class PeriodLock(Base):
    """A month frozen once it is finished and checked. Entries, verification and imports for a locked month are refused; HR/admin reopen with a reason."""

    __tablename__ = "period_locks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    month: Mapped[date] = mapped_column(Date, unique=True)  # first day of the month
    status: Mapped[str] = mapped_column(String(8), default="locked")  # locked | open
    locked_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reopened_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reopen_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class NoticeAck(Base):
    """A person's acknowledgement of the staff data-use notice, once per notice version."""

    __tablename__ = "notice_acks"
    __table_args__ = (Index("ix_notice_ack_unique", "admin_user_id", "version", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    admin_user_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[str] = mapped_column(String(20))
    acknowledged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class NotificationPref(Base):
    """How a person wants performance reminders delivered, beyond the in-app bell. Email uses their login's address; SMS needs a number they gave."""

    __tablename__ = "notification_prefs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    admin_user_id: Mapped[str] = mapped_column(String(36), unique=True)
    email_on: Mapped[bool] = mapped_column(Boolean, default=True)
    sms_on: Mapped[bool] = mapped_column(Boolean, default=False)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ReminderDelivery(Base):
    """One row per person, channel and kind per day: keeps sends idempotent and gives HR a delivery report. Never stores message text."""

    __tablename__ = "reminder_deliveries"
    __table_args__ = (Index("ix_reminder_delivery_unique", "admin_user_id", "channel", "kind", "day", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    admin_user_id: Mapped[str] = mapped_column(String(36))
    channel: Mapped[str] = mapped_column(String(8))  # email | sms
    kind: Mapped[str] = mapped_column(String(12), default="daily")  # daily | weekly | pack | test
    day: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(8))  # sent | failed | skipped
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Feedback360Cycle(Base):
    """A 360 feedback round (development only: it never feeds a score, rating or PIP). draft -> open -> closed -> released."""

    __tablename__ = "feedback360_cycles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(10), default="draft")
    min_group: Mapped[int] = mapped_column(default=3)  # fewer responses than this in a group and the group is hidden
    closes_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Feedback360Subject(Base):
    __tablename__ = "feedback360_subjects"
    __table_args__ = (Index("ix_f360_subject_unique", "cycle_id", "profile_id", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cycle_id: Mapped[str] = mapped_column(ForeignKey("feedback360_cycles.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[str] = mapped_column(String(36), index=True)


class Feedback360Rater(Base):
    __tablename__ = "feedback360_raters"
    __table_args__ = (Index("ix_f360_rater_unique", "subject_id", "rater_profile_id", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    subject_id: Mapped[str] = mapped_column(ForeignKey("feedback360_subjects.id", ondelete="CASCADE"), index=True)
    rater_profile_id: Mapped[str] = mapped_column(String(36), index=True)
    relationship: Mapped[str] = mapped_column(String(14))  # self | manager | peer | direct_report | other
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending | submitted | declined
    answers: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {competency_key: 1-5}
    strengths: Mapped[str | None] = mapped_column(Text, nullable=True)
    development: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CascadeTarget(Base):
    """
    A target set at company, region or unit level for an additive KPI (sales, customers...), effective-dated and append-only like
    individual targets. It never scores anyone: it is the allocation that individual targets are compared with, so a change at the
    top shows up immediately as a gap below. level: company | region | unit (entity_id is 'company' for the company).
    """

    __tablename__ = "cascade_targets"
    __table_args__ = (Index("ix_cascade_lookup", "level", "entity_id", "kpi_key", "effective_from"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    level: Mapped[str] = mapped_column(String(8))
    entity_id: Mapped[str] = mapped_column(String(36))
    kpi_key: Mapped[str] = mapped_column(String(60))
    effective_from: Mapped[date] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    reason: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(10), default="manual")  # manual | cascade
    set_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BoardPack(Base):
    """A generated monthly one-page board pack (PDF in private storage). One per month; regenerating replaces the file and keeps the row."""

    __tablename__ = "board_packs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    month: Mapped[date] = mapped_column(Date, unique=True)  # first day of the month the pack covers
    file_path: Mapped[str] = mapped_column(String(300))
    generated_by: Mapped[str] = mapped_column(String(100))  # a username or "system"
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # headline figures only, for the list


class ImportVersion(Base):
    """Version number and notes for a posted workbook import. Versions are numbered per year ("series") in the order they were posted and never renumbered."""

    __tablename__ = "import_versions"
    __table_args__ = (Index("ix_import_version_unique", "series", "version_no", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    import_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    series: Mapped[str] = mapped_column(String(20))
    version_no: Mapped[int] = mapped_column()
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    restored_from: Mapped[int | None] = mapped_column(nullable=True)
    unposted_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    unposted_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PerformancePlan(Base):
    """
    An improvement plan someone saved from the Outlook chart: the goal and the month-by-month path needed to reach it, frozen on the day it was
    saved so later months can be plotted against it. It records a decision; it never changes a score, rating, status or PIP.

    One ACTIVE plan per person: `active_slot` is 1 while active and NULL otherwise, and the unique index on (profile_id, active_slot) makes the
    database refuse a second active plan even if two people save at the same moment (NULLs never clash, so any number of past plans are kept).
    `recommendation_id` and `profile_id` are plain id strings, like the other tables here, so nothing blocks deleting or anonymising a person.
    """

    __tablename__ = "performance_plans"
    __table_args__ = (
        Index("ix_plan_one_active", "profile_id", "active_slot", unique=True),
        Index("ix_plan_profile_created", "profile_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(String(36))
    recommendation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(12), default="active")  # active | closed | superseded
    active_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goal_pct: Mapped[float] = mapped_column(Float)
    goal_label: Mapped[str | None] = mapped_column(String(60), nullable=True)
    start_month: Mapped[str] = mapped_column(String(7))  # last scored month when saved, YYYY-MM
    start_pct: Mapped[float] = mapped_column(Float)
    plan_months: Mapped[int] = mapped_column(Integer)
    path: Mapped[list] = mapped_column(JSON, default=list)  # [{"month": "2026-11", "pct": 54.0}, ...] first item is the start month
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_username: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    closed_by_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(12), nullable=True)  # manual | superseded
    close_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # how it stood on the day it was closed
