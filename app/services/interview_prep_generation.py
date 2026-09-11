"""
Orchestrates AI generation of a candidate-specific interview prep sheet
for one career application - see app/schemas/interview_prep.py for the
InterviewPrepContent shape and app/services/interview_prep_pdf.py for how
it's rendered to PDF.

The three standard sections every candidate is asked (HR & Background,
Competency & Situational, Panel Assessment & Fit) are taken directly from
the company's INTERVIEW_QUESTIONS.docx reference sheet and are fixed here,
never AI-generated - every candidate for every role gets the exact same
baseline questions, which keeps the process fair and consistent and means
a flaky/hallucinated AI response can never lose or alter them. The AI
(see app/services/ai_providers/base.py's AIInterviewPrepDraft) only drafts
the candidate-specific summary, strengths, probing areas, and a
"Role-Specific & Technical" section built from this candidate's actual
cover note/CV against this job's actual requirements.
"""

from app.models.career_application import CareerApplication
from app.models.job_opening import JobOpening
from app.services.ai_providers.base import AIInterviewPrepDraft
from app.services.ai_providers.factory import get_provider
from app.services.cv_text_extraction import extract_cv_text

DEFAULT_TIMEOUT_SECONDS = 30

# Fixed, non-AI-generated baseline every candidate is asked - see this
# module's docstring. `why_it_matters` is intentionally blank for these:
# they're standard questions the panel already knows how to use, not
# candidate-specific insight.
STANDARD_INTERVIEW_SECTIONS: list[dict] = [
    {
        "title": "HR & Background",
        "questions": [
            {"question": "Tell us briefly about yourself and your career journey.", "why_it_matters": ""},
            {"question": "Why are you interested in joining us?", "why_it_matters": ""},
            {"question": "What motivates you at work?", "why_it_matters": ""},
            {"question": "What are your salary expectations and availability?", "why_it_matters": ""},
            {"question": "What are you looking for in your next role?", "why_it_matters": ""},
        ],
    },
    {
        "title": "Competency & Situational",
        "questions": [
            {"question": "Tell us about a difficult situation you handled.", "why_it_matters": ""},
            {"question": "How do you handle disagreement within a team?", "why_it_matters": ""},
            {"question": "Describe a time you made a mistake and what you learned.", "why_it_matters": ""},
            {
                "question": "How would you handle unclear instructions or competing priorities?",
                "why_it_matters": "",
            },
        ],
    },
    {
        "title": "Panel Assessment & Fit",
        "questions": [
            {"question": "What would you aim to achieve in your first 90 days?", "why_it_matters": ""},
            {"question": "What type of workplace brings out your best performance?", "why_it_matters": ""},
            {"question": "Why should we select you for this role?", "why_it_matters": ""},
        ],
    },
]

ROLE_SPECIFIC_SECTION_TITLE = "Role-Specific & Technical"


def build_standard_sections() -> list[dict]:
    """A fresh (deep enough) copy so a caller mutating the result never mutates the module-level constant."""
    return [
        {"title": section["title"], "questions": [dict(q) for q in section["questions"]]}
        for section in STANDARD_INTERVIEW_SECTIONS
    ]


def generate_interview_prep_with_ai(
    *,
    job: JobOpening | None,
    application: CareerApplication,
    provider_name: str,
    model: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> AIInterviewPrepDraft:
    """
    Raises the same AIProviderError family as evaluate_candidate_with_ai
    (see app/services/ats_ai_evaluation.py) - the caller (the admin_ai.py
    router) decides what to do on failure.

    `job` is None for a "General application" (CareerApplication.job_id is
    nullable - see that model) - in that case the prompt falls back to
    just the free-text role the candidate typed in, with no
    description/requirements/responsibilities to draw on. The AI is still
    asked for a candidate-specific summary/strengths/probes and
    role-specific questions; there's just less to tailor them against.
    """
    provider = get_provider(provider_name)  # raises AIProviderNotConfiguredError if no key

    cv_text = extract_cv_text(application.cv_stored_filename)  # None on any failure - handled gracefully downstream

    job_context = (
        {
            "title": job.title,
            "department": job.department,
            "location": job.location,
            "employment_type": job.type,
            "description": job.description,
            "requirements": job.requirements,
            "responsibilities": job.responsibilities,
        }
        if job is not None
        else {
            "title": application.role,
            "department": "",
            "location": "",
            "employment_type": "",
            "description": "",
            "requirements": [],
            "responsibilities": [],
        }
    )
    candidate_context = {
        # full_name deliberately excluded - see the same note on
        # app/services/ats_ai_evaluation.py's candidate_context; the model
        # only needs the content of the application, not the candidate's
        # identity.
        "role_applied_for": application.role,
        "cover_note": application.cover_note,
        "cv_text": cv_text,
    }

    return provider.generate_interview_prep(
        job_context=job_context, candidate_context=candidate_context, model=model, timeout_seconds=timeout_seconds
    )
