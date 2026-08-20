"""
Google Gemini provider. Only this file (plus prompts.py, shared) knows
anything about the google-genai SDK — imported nowhere else in the
codebase. Requires GEMINI_API_KEY in .env; see app/config.py.
"""

from app.services.ai_providers.base import (
    AICriteriaSuggestion,
    AIEvaluationResult,
    AIJobDraft,
    AIProvider,
    AIProviderError,
    AIProviderInvalidResponseError,
    AIProviderRateLimitError,
    AIProviderTimeoutError,
)
from app.services.ai_providers.prompts import (
    CRITERIA_SUGGESTION_SYSTEM_PROMPT,
    EVALUATION_SYSTEM_PROMPT,
    JOB_GENERATION_SYSTEM_PROMPT,
    build_criteria_suggestion_prompt,
    build_evaluation_prompt,
    build_job_generation_prompt,
    parse_criteria_suggestion_response,
    parse_evaluation_response,
    parse_job_draft_response,
)

DEFAULT_MODEL = "gemini-3.5-flash-lite"


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(self, api_key: str):
        self._api_key = api_key

    def _complete_json(self, *, system_prompt: str, user_prompt: str, model: str, timeout_seconds: int) -> str:
        # Imported lazily so google-genai is only required if this provider
        # is actually selected/used — optional for weighted-scoring-only setups.
        from google import genai
        from google.genai import errors as genai_errors
        from google.genai import types as genai_types

        client = genai.Client(api_key=self._api_key)
        try:
            response = client.models.generate_content(
                model=model or DEFAULT_MODEL,
                contents=user_prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    temperature=0.2,
                    http_options=genai_types.HttpOptions(timeout=timeout_seconds * 1000),
                ),
            )
        except genai_errors.ClientError as exc:
            status_code = getattr(exc, "code", None)
            if status_code == 429:
                raise AIProviderRateLimitError(f"Gemini rate limit hit: {exc}") from exc
            raise AIProviderError(f"Gemini API error: {exc}") from exc
        except genai_errors.ServerError as exc:
            raise AIProviderError(f"Gemini server error: {exc}") from exc
        except TimeoutError as exc:
            raise AIProviderTimeoutError(f"Gemini request timed out: {exc}") from exc
        except Exception as exc:  # network errors, auth errors, etc.
            message = str(exc)
            if "timeout" in message.lower() or "deadline" in message.lower():
                raise AIProviderTimeoutError(f"Gemini request timed out: {exc}") from exc
            raise AIProviderError(f"Gemini request failed: {exc}") from exc

        text = getattr(response, "text", None)
        if not text:
            raise AIProviderInvalidResponseError("Gemini returned an empty response body.")
        return text

    def evaluate_candidate(self, *, job_context, candidate_context, model, timeout_seconds) -> AIEvaluationResult:
        prompt = build_evaluation_prompt(job_context, candidate_context)
        raw = self._complete_json(
            system_prompt=EVALUATION_SYSTEM_PROMPT, user_prompt=prompt, model=model, timeout_seconds=timeout_seconds
        )
        return parse_evaluation_response(
            raw, provider=self.name, model=model or DEFAULT_MODEL, cv_text_used=bool(candidate_context.get("cv_text"))
        )

    def generate_job_draft(self, *, title, model, timeout_seconds) -> AIJobDraft:
        prompt = build_job_generation_prompt(title)
        raw = self._complete_json(
            system_prompt=JOB_GENERATION_SYSTEM_PROMPT, user_prompt=prompt, model=model, timeout_seconds=timeout_seconds
        )
        return parse_job_draft_response(raw, provider=self.name, model=model or DEFAULT_MODEL)

    def suggest_screening_criteria(self, *, job_context, model, timeout_seconds) -> AICriteriaSuggestion:
        prompt = build_criteria_suggestion_prompt(job_context)
        raw = self._complete_json(
            system_prompt=CRITERIA_SUGGESTION_SYSTEM_PROMPT,
            user_prompt=prompt,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        return parse_criteria_suggestion_response(raw, provider=self.name, model=model or DEFAULT_MODEL)
