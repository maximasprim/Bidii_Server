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
    """Structured result of one AI candidate evaluation. Provider-agnostic."""

    score_percentage: float  # 0-100
    recommendation: str  # "recommended" | "review" | "not_recommended"
    matched_requirements: list[AIRequirementOutcome] = field(default_factory=list)
    missing_requirements: list[AIRequirementOutcome] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    explanation: str = ""
    provider: str = ""
    model: str = ""
    cv_text_used: bool = False


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
    ) -> AIEvaluationResult: ...

    @abstractmethod
    def generate_job_draft(self, *, title: str, model: str, timeout_seconds: int) -> AIJobDraft: ...

    @abstractmethod
    def suggest_screening_criteria(
        self, *, job_context: dict, model: str, timeout_seconds: int
    ) -> AICriteriaSuggestion: ...