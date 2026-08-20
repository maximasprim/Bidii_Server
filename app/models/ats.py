"""
ATS (Applicant Tracking System) models.

Deliberately isolated from career_application.py / job_opening.py — this
module only ever *references* CareerApplication/JobOpening by foreign key,
it never modifies those models. All tables here are brand new, so
Base.metadata.create_all(bind=engine) in main.py creates them automatically
on startup (see app/main.py's `_migrate_schema` docstring for why that's
only true for new TABLES, not new columns on existing ones — not a concern
here since nothing below touches an existing table).

Every application without an ATSScreeningResult row simply has no ATS data;
the rest of the app (career_applications, jobs) is completely unaware this
module exists.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ATSCriterionCategory(str, enum.Enum):
    qualification = "qualification"
    education = "education"
    experience = "experience"
    skill = "skill"
    certification = "certification"
    location = "location"
    custom = "custom"


class ATSRecommendation(str, enum.Enum):
    recommended = "recommended"
    review = "review"
    not_recommended = "not_recommended"


class ATSAuditAction(str, enum.Enum):
    config_created = "config_created"
    config_updated = "config_updated"
    criterion_added = "criterion_added"
    criterion_updated = "criterion_updated"
    criterion_removed = "criterion_removed"
    screened = "screened"
    auto_rejected = "auto_rejected"
    recommendation_overridden = "recommendation_overridden"
    note_added = "note_added"
    ai_evaluation_failed = "ai_evaluation_failed"
    ai_fallback_to_weighted = "ai_fallback_to_weighted"


class ATSEvaluationMode(str, enum.Enum):
    """Which engine ATSConfiguration.evaluation_mode selects for a job."""

    weighted = "weighted"
    ai = "ai"


class ATSAIProviderName(str, enum.Enum):
    openai = "openai"
    gemini = "gemini"


class ATSConfiguration(Base):
    """
    One screening configuration per job posting. Created on demand the
    first time an admin opens a job's ATS Configuration tab — a job with
    no ATSConfiguration row simply has ATS screening turned off for it.
    """

    __tablename__ = "ats_configurations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(ForeignKey("job_openings.id"), unique=True, index=True)

    is_scoring_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Off by default (see ATS requirement: "Do NOT automatically reject
    # candidates unless explicitly enabled by an administrator"). When on,
    # a screening run that fails a mandatory criterion sets the underlying
    # CareerApplication.status to "rejected" in addition to recording the
    # "not_recommended" category.
    auto_reject_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Score thresholds, as a percentage of max possible weighted score
    # (0-100). >= minimum_recommend_score -> "recommended";
    # < minimum_review_score -> "not_recommended"; in between -> "review".
    minimum_recommend_score: Mapped[float] = mapped_column(Float, default=70.0)
    minimum_review_score: Mapped[float] = mapped_column(Float, default=40.0)

    # Which engine actually runs when this job is screened. Weighted
    # scoring (ats_scoring.py) is untouched and remains the default — AI
    # evaluation is strictly opt-in per job. See app/services/ats_ai_evaluation.py.
    evaluation_mode: Mapped[ATSEvaluationMode] = mapped_column(Enum(ATSEvaluationMode), default=ATSEvaluationMode.weighted)
    ai_provider: Mapped[ATSAIProviderName | None] = mapped_column(Enum(ATSAIProviderName), nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    criteria: Mapped[list["ATSCriterion"]] = relationship(
        "ATSCriterion", back_populates="config", cascade="all, delete-orphan", order_by="ATSCriterion.created_at"
    )


class ATSCriterion(Base):
    """A single screening criterion belonging to one job's ATSConfiguration."""

    __tablename__ = "ats_criteria"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    config_id: Mapped[str] = mapped_column(ForeignKey("ats_configurations.id"), index=True)

    category: Mapped[ATSCriterionCategory] = mapped_column(Enum(ATSCriterionCategory))
    label: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Keywords/phrases matched (case-insensitive substring) against an
    # application's cover note + resolved role to auto-evaluate this
    # criterion. Kept simple and dependency-free — no CV text extraction —
    # so this stays isolated and easy to extend later.
    match_keywords: Mapped[list] = mapped_column(JSON, default=list)

    weight: Mapped[float] = mapped_column(Float, default=1.0)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    config: Mapped["ATSConfiguration"] = relationship("ATSConfiguration", back_populates="criteria")


class ATSScreeningResult(Base):
    """
    Latest screening outcome for one career application. Re-running
    screening overwrites this row (the history of *how* it changed lives in
    ATSAuditLog, not here) — this table always reflects "what the system
    currently says about this candidate".
    """

    __tablename__ = "ats_screening_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    application_id: Mapped[str] = mapped_column(ForeignKey("career_applications.id"), unique=True, index=True)
    config_id: Mapped[str | None] = mapped_column(ForeignKey("ats_configurations.id"), nullable=True)

    total_score: Mapped[float] = mapped_column(Float, default=0.0)
    max_possible_score: Mapped[float] = mapped_column(Float, default=0.0)

    system_recommendation: Mapped[ATSRecommendation] = mapped_column(
        Enum(ATSRecommendation), default=ATSRecommendation.review
    )

    # Manual override by a recruiter/admin. When set, this — not
    # system_recommendation — is what the vetting UI treats as the final
    # call, while system_recommendation is preserved for transparency.
    override_recommendation: Mapped[ATSRecommendation | None] = mapped_column(
        Enum(ATSRecommendation), nullable=True
    )
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    override_by: Mapped[str | None] = mapped_column(ForeignKey("admin_users.id"), nullable=True)
    overridden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Each entry: {criterion_id, label, category, weight, is_required}
    matched_criteria: Mapped[list] = mapped_column(JSON, default=list)
    missing_criteria: Mapped[list] = mapped_column(JSON, default=list)
    failed_mandatory_criteria: Mapped[list] = mapped_column(JSON, default=list)

    # Mirrors `len(failed_mandatory_criteria) > 0`, kept as a plain boolean
    # column (rather than querying the JSON column directly) so filtering
    # by "failed a mandatory criterion" is a simple, portable SQL filter
    # across both SQLite (dev) and Postgres (prod).
    has_failed_mandatory: Mapped[bool] = mapped_column(Boolean, default=False)

    auto_scored: Mapped[bool] = mapped_column(Boolean, default=True)
    scored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # --- AI evaluation fields (all null/empty in weighted mode) -----------
    # Which engine actually produced this result — independent of the
    # job's *current* ATSConfiguration.evaluation_mode, so a result stays
    # self-describing even if the config is switched after the fact.
    evaluation_method: Mapped[ATSEvaluationMode] = mapped_column(Enum(ATSEvaluationMode), default=ATSEvaluationMode.weighted)
    ai_provider: Mapped[ATSAIProviderName | None] = mapped_column(Enum(ATSAIProviderName), nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ai_strengths: Mapped[list] = mapped_column(JSON, default=list)
    ai_weaknesses: Mapped[list] = mapped_column(JSON, default=list)
    ai_explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when AI evaluation was attempted but failed and this result is a
    # weighted-scoring fallback instead — see ATSAuditAction.ai_fallback_to_weighted.
    ai_fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ATSRecruiterNote(Base):
    """Free-text vetting notes a recruiter/admin attaches to an application. Never auto-generated."""

    __tablename__ = "ats_recruiter_notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    application_id: Mapped[str] = mapped_column(ForeignKey("career_applications.id"), index=True)
    admin_id: Mapped[str] = mapped_column(ForeignKey("admin_users.id"))
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class ATSAuditLog(Base):
    """
    Append-only audit trail of ATS activity for one application: screening
    runs, recommendation overrides, notes added, and config changes that
    affected it. Nothing in this module ever deletes or edits a row here.
    """

    __tablename__ = "ats_audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    application_id: Mapped[str] = mapped_column(ForeignKey("career_applications.id"), index=True)
    # Nullable: a system-triggered screening run (not initiated by a
    # specific logged-in admin, e.g. a "screen all" batch job) has no actor.
    admin_id: Mapped[str | None] = mapped_column(ForeignKey("admin_users.id"), nullable=True)

    action: Mapped[ATSAuditAction] = mapped_column(Enum(ATSAuditAction))
    details: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
