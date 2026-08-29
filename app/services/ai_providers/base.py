"""
Abstract AI provider interface.

Every concrete provider (OpenAI, Gemini) implements `AIProvider` and is
built through app/services/ai_providers/factory.py — nothing in the ATS
screening logic or job-generation logic ever imports OpenAIProvider or
GeminiProvider directly. Swapping, adding, or removing a provider only
touches this package; app/services/ats_ai_evaluation.py and
app/services/ai_job_generation.py (the orchestrators that call into this
package) never change.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class AIProviderError(Exception):
    """Base class for every AI-provider failure. Callers catch this to fall back gracefully."""


class AIProviderNotConfiguredError(AIProviderError):
    """The selected provider has no API key configured server-side (or is unknown)."""


class AIProviderTimeoutError(AIProviderError):
    """The provider didn't respond within the configured timeout."""


class AIProviderRateLimitError(AIProviderError):
    """The provider rejected the request due to rate limiting/quota."""


class AIProviderInvalidResponseError(AIProviderError):
    """The provider responded, but not with valid/parseable structured output."""


@dataclass
class AIRequirementOutcome:
    label: str
    detail: str = ""


@dataclass
class AIEvaluationResult:
    """
    Structured result of one AI candidate evaluation. Provider-agnostic.

    `score_percentage` is always attributable to a deterministic
    computation once criteria_aware is True: it's derived by
    admin_ats_screening.py from matched_criteria/missing_criteria's
    weights via bucket_recommendation() (see ats_scoring.py), exactly the
    same code path the weighted engine uses — the model is only asked for
    a per-criterion met/partial/not_met verdict, never for the score or
    recommendation label itself, so there's no way for the two to
    disagree. When criteria_aware is False (the job has no configured
    ATSCriterion rows yet), score_percentage is the model's own
    self-reported figure and matched_criteria/missing_criteria are empty
    — matched_requirements/missing_requirements (free-text) are used
    instead. Either way, `recommendation` here is only ever the model's
    raw self-reported label, kept for audit/debugging visibility — the
    system's stored recommendation always comes from bucket_recommendation(),
    never from this field directly.
    """

    score_percentage: float  # 0-100
    recommendation: str  # "recommended" | "review" | "not_recommended" — see docstring above
    matched_requirements: list[AIRequirementOutcome] = field(default_factory=list)
    missing_requirements: list[AIRequirementOutcome] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    explanation: str = ""
    provider: str = ""
    model: str = ""
    cv_text_used: bool = False
    # Populated only when the job has configured weighted criteria (see
    # app/services/ats_ai_evaluation.py) — the AI's per-criterion verdicts,
    # in the exact same {criterion_id,label,category,weight,is_required}
    # shape app/services/ats_scoring.py uses, so the two engines' results
    # render identically in the admin UI and so score/recommendation can
    # be computed deterministically instead of trusted from the model.
    matched_criteria: list[dict] = field(default_factory=list)
    missing_criteria: list[dict] = field(default_factory=list)
    failed_mandatory_criteria: list[dict] = field(default_factory=list)
    criteria_aware: bool = False
    # Populated only by the criteria-aware reproducibility check (see
    # app/services/ats_ai_evaluation.py) — criteria where two independent
    # evaluation calls disagreed on met/not_met. These are also included
    # in missing_criteria (an unreliable "met" verdict isn't credited
    # either), so they're already visible wherever missing_criteria is
    # rendered; this list exists so the caller can force the overall
    # recommendation to "review" whenever it's non-empty, rather than
    # trusting a score built partly on a coin flip.
    inconsistent_criteria: list[dict] = field(default_factory=list)


@dataclass
class AISuggestedCriterion:
    """One AI-suggested screening criterion. Maps 1:1 onto ATSCriterionCreate — never saved automatically, only returned as a draft for a recruiter to review, edit, and add."""

    category: str  # one of ATSCriterionCategory's values
    label: str
    description: str = ""
    match_keywords: list[str] = field(default_factory=list)
    weight: float = 10.0
    is_required: bool = False


@dataclass
class AICriteriaSuggestion:
    criteria: list[AISuggestedCriterion] = field(default_factory=list)
    provider: str = ""
    model: str = ""


@dataclass
class AIJobDraft:
    """Structured draft of a job posting. Never saved automatically — only used to pre-fill the create-job form."""

    summary: str
    description: str
    responsibilities: list[str] = field(default_factory=list)
    # Qualifications, requirements, skills, experience and eligibility are
    # merged into one flat list here — that matches how every existing job
    # posting on this site already stores "Requirements" (see
    # app/models/job_opening.py — one JSON list, no sub-categories) so the
    # generated draft drops straight into the existing create-job form with
    # zero schema changes.
    requirements: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""


@dataclass
class AIFormalJDDraft:
    """
    Structured draft of a formal Job Description document — the fixed,
    letterhead-style JD format (see app/services/jd_pdf.py), not the
    plain job-posting draft AIJobDraft above feeds into the create-job
    form. Never saved automatically: the router only returns this for an
    admin to review/edit before it's saved to JobOpening.jd_content and,
    from there, rendered to PDF.
    """

    overall_role_purpose: str
    reports_to: str = ""
    key_responsibilities: list[dict] = field(default_factory=list)  # [{heading, bullets, pct_time, criteria}]
    reporting_relationships: str = ""
    decision_making_mandates: str = ""
    planning_responsibility: str = ""
    relationship_management: str = ""
    minimum_qualifications: list[str] = field(default_factory=list)
    experience_and_skills: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""


@dataclass
class AIBranchMatch:
    """
    Result of asking the AI which of the company's active branches is the
    real-world closest match for an applicant's free-text location. Used
    only as a fallback (see app/services/branch_assignment.py) when a
    direct keyword match against branch names/addresses fails - most
    applicants typing a recognized town match instantly with zero AI cost.
    """

    branch_id: str  # must be one of the ids given in the prompt - validated by the parser, not trusted blindly
    reasoning: str
    provider: str = ""
    model: str = ""


class AIProvider(ABC):
    name: str

    @abstractmethod
    def evaluate_candidate(
        self,
        *,
        job_context: dict,
        candidate_context: dict,
        model: str,
        timeout_seconds: int,
        criteria: list[dict] | None = None,
    ) -> AIEvaluationResult: ...

    @abstractmethod
    def generate_job_draft(self, *, title: str, model: str, timeout_seconds: int) -> AIJobDraft: ...

    @abstractmethod
    def suggest_screening_criteria(
        self, *, job_context: dict, model: str, timeout_seconds: int
    ) -> AICriteriaSuggestion: ...

    @abstractmethod
    def generate_formal_jd(self, *, job_context: dict, model: str, timeout_seconds: int) -> AIFormalJDDraft: ...

    @abstractmethod
    def suggest_nearest_branch(
        self, *, location_text: str, branches: list[dict], model: str, timeout_seconds: int
    ) -> AIBranchMatch: ...
# """
# Abstract AI provider interface.

# Every concrete provider (OpenAI, Gemini) implements `AIProvider` and is
# built through app/services/ai_providers/factory.py — nothing in the ATS
# screening logic or job-generation logic ever imports OpenAIProvider or
# GeminiProvider directly. Swapping, adding, or removing a provider only
# touches this package; app/services/ats_ai_evaluation.py and
# app/services/ai_job_generation.py (the orchestrators that call into this
# package) never change.
# """

# from abc import ABC, abstractmethod
# from dataclasses import dataclass, field


# class AIProviderError(Exception):
#     """Base class for every AI-provider failure. Callers catch this to fall back gracefully."""


# class AIProviderNotConfiguredError(AIProviderError):
#     """The selected provider has no API key configured server-side (or is unknown)."""


# class AIProviderTimeoutError(AIProviderError):
#     """The provider didn't respond within the configured timeout."""


# class AIProviderRateLimitError(AIProviderError):
#     """The provider rejected the request due to rate limiting/quota."""


# class AIProviderInvalidResponseError(AIProviderError):
#     """The provider responded, but not with valid/parseable structured output."""


# @dataclass
# class AIRequirementOutcome:
#     label: str
#     detail: str = ""


# @dataclass
# class AIEvaluationResult:
#     """Structured result of one AI candidate evaluation. Provider-agnostic."""

#     score_percentage: float  # 0-100
#     recommendation: str  # "recommended" | "review" | "not_recommended"
#     matched_requirements: list[AIRequirementOutcome] = field(default_factory=list)
#     missing_requirements: list[AIRequirementOutcome] = field(default_factory=list)
#     strengths: list[str] = field(default_factory=list)
#     weaknesses: list[str] = field(default_factory=list)
#     explanation: str = ""
#     provider: str = ""
#     model: str = ""
#     cv_text_used: bool = False


# @dataclass
# class AISuggestedCriterion:
#     """One AI-suggested screening criterion. Maps 1:1 onto ATSCriterionCreate — never saved automatically, only returned as a draft for a recruiter to review, edit, and add."""

#     category: str  # one of ATSCriterionCategory's values
#     label: str
#     description: str = ""
#     match_keywords: list[str] = field(default_factory=list)
#     weight: float = 10.0
#     is_required: bool = False


# @dataclass
# class AICriteriaSuggestion:
#     criteria: list[AISuggestedCriterion] = field(default_factory=list)
#     provider: str = ""
#     model: str = ""


# @dataclass
# class AIJobDraft:
#     """Structured draft of a job posting. Never saved automatically — only used to pre-fill the create-job form."""

#     summary: str
#     description: str
#     responsibilities: list[str] = field(default_factory=list)
#     # Qualifications, requirements, skills, experience and eligibility are
#     # merged into one flat list here — that matches how every existing job
#     # posting on this site already stores "Requirements" (see
#     # app/models/job_opening.py — one JSON list, no sub-categories) so the
#     # generated draft drops straight into the existing create-job form with
#     # zero schema changes.
#     requirements: list[str] = field(default_factory=list)
#     provider: str = ""
#     model: str = ""


# class AIProvider(ABC):
#     name: str

#     @abstractmethod
#     def evaluate_candidate(
#         self,
#         *,
#         job_context: dict,
#         candidate_context: dict,
#         model: str,
#         timeout_seconds: int,
#     ) -> AIEvaluationResult: ...

#     @abstractmethod
#     def generate_job_draft(self, *, title: str, model: str, timeout_seconds: int) -> AIJobDraft: ...

#     @abstractmethod
#     def suggest_screening_criteria(
#         self, *, job_context: dict, model: str, timeout_seconds: int
#     ) -> AICriteriaSuggestion: ...