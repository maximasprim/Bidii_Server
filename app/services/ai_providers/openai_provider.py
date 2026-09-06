"""
OpenAI provider. Only this file (plus prompts.py, shared) knows anything
about the OpenAI SDK - the `openai` package is imported nowhere else in the
codebase. Requires OPENAI_API_KEY in .env; see app/config.py.
"""

from app.services.ai_providers.base import (
    AIBranchMatch,
    AICriteriaSuggestion,
    AIEvaluationResult,
    AIFormalJDDraft,
    AIGeocodeResult,
    AIJobDraft,
    AIProvider,
    AIProviderError,
    AIProviderInvalidResponseError,
    AIProviderRateLimitError,
    AIProviderTimeoutError,
)
from app.services.ai_providers.prompts import (
    BRANCH_MATCH_SYSTEM_PROMPT,
    CRITERIA_SUGGESTION_SYSTEM_PROMPT,
    EVALUATION_SYSTEM_PROMPT,
    FORMAL_JD_SYSTEM_PROMPT,
    GEOCODE_SYSTEM_PROMPT,
    JOB_GENERATION_SYSTEM_PROMPT,
    build_branch_match_prompt,
    build_criteria_suggestion_prompt,
    build_evaluation_prompt,
    build_formal_jd_prompt,
    build_geocode_prompt,
    build_job_generation_prompt,
    parse_branch_match_response,
    parse_criteria_suggestion_response,
    parse_evaluation_response,
    parse_formal_jd_response,
    parse_geocode_response,
    parse_job_draft_response,
)

DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIProvider(AIProvider):
    name = "openai"

    def __init__(self, api_key: str):
        self._api_key = api_key

    def _client(self, timeout_seconds: int):
        # Imported lazily so the `openai` package is only required if this
        # provider is actually selected/used - installing it is optional
        # for anyone running the ATS in weighted-scoring-only mode.
        from openai import OpenAI

        return OpenAI(api_key=self._api_key, timeout=timeout_seconds)

    def _complete_json(self, *, system_prompt: str, user_prompt: str, model: str, timeout_seconds: int) -> str:
        import openai as openai_sdk

        client = self._client(timeout_seconds)
        try:
            response = client.chat.completions.create(
                model=model or DEFAULT_MODEL,
                response_format={"type": "json_object"},
                temperature=0.2,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except openai_sdk.APITimeoutError as exc:
            raise AIProviderTimeoutError(f"OpenAI request timed out: {exc}") from exc
        except openai_sdk.RateLimitError as exc:
            raise AIProviderRateLimitError(f"OpenAI rate limit hit: {exc}") from exc
        except openai_sdk.APIError as exc:
            raise AIProviderError(f"OpenAI API error: {exc}") from exc
        except Exception as exc:  # network errors, auth errors, etc.
            raise AIProviderError(f"OpenAI request failed: {exc}") from exc

        try:
            content = response.choices[0].message.content
        except (IndexError, AttributeError) as exc:
            raise AIProviderInvalidResponseError(f"OpenAI returned an empty/unexpected response: {exc}") from exc
        if not content:
            raise AIProviderInvalidResponseError("OpenAI returned an empty response body.")
        return content

    def evaluate_candidate(self, *, job_context, candidate_context, model, timeout_seconds, criteria=None) -> AIEvaluationResult:
        prompt = build_evaluation_prompt(job_context, candidate_context, criteria=criteria)
        raw = self._complete_json(
            system_prompt=EVALUATION_SYSTEM_PROMPT, user_prompt=prompt, model=model, timeout_seconds=timeout_seconds
        )
        return parse_evaluation_response(
            raw,
            provider=self.name,
            model=model or DEFAULT_MODEL,
            cv_text_used=bool(candidate_context.get("cv_text")),
            criteria=criteria,
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

    def generate_formal_jd(self, *, job_context, model, timeout_seconds) -> AIFormalJDDraft:
        prompt = build_formal_jd_prompt(job_context)
        raw = self._complete_json(
            system_prompt=FORMAL_JD_SYSTEM_PROMPT, user_prompt=prompt, model=model, timeout_seconds=timeout_seconds
        )
        return parse_formal_jd_response(raw, provider=self.name, model=model or DEFAULT_MODEL)

    def suggest_nearest_branch(self, *, location_text, branches, model, timeout_seconds) -> AIBranchMatch:
        prompt = build_branch_match_prompt(location_text, branches)
        raw = self._complete_json(
            system_prompt=BRANCH_MATCH_SYSTEM_PROMPT, user_prompt=prompt, model=model, timeout_seconds=timeout_seconds
        )
        valid_ids = {b["id"] for b in branches}
        return parse_branch_match_response(raw, valid_branch_ids=valid_ids, provider=self.name, model=model or DEFAULT_MODEL)

    def geocode_location(self, *, location_text, county, model, timeout_seconds) -> AIGeocodeResult:
        prompt = build_geocode_prompt(location_text, county)
        raw = self._complete_json(
            system_prompt=GEOCODE_SYSTEM_PROMPT, user_prompt=prompt, model=model, timeout_seconds=timeout_seconds
        )
        return parse_geocode_response(raw, provider=self.name, model=model or DEFAULT_MODEL)

# """
# OpenAI provider. Only this file (plus prompts.py, shared) knows anything
# about the OpenAI SDK - the `openai` package is imported nowhere else in the
# codebase. Requires OPENAI_API_KEY in .env; see app/config.py.
# """

# from app.services.ai_providers.base import (
#     AIBranchMatch,
#     AICriteriaSuggestion,
#     AIEvaluationResult,
#     AIFormalJDDraft,
#     AIJobDraft,
#     AIProvider,
#     AIProviderError,
#     AIProviderInvalidResponseError,
#     AIProviderRateLimitError,
#     AIProviderTimeoutError,
# )
# from app.services.ai_providers.prompts import (
#     BRANCH_MATCH_SYSTEM_PROMPT,
#     CRITERIA_SUGGESTION_SYSTEM_PROMPT,
#     EVALUATION_SYSTEM_PROMPT,
#     FORMAL_JD_SYSTEM_PROMPT,
#     JOB_GENERATION_SYSTEM_PROMPT,
#     build_branch_match_prompt,
#     build_criteria_suggestion_prompt,
#     build_evaluation_prompt,
#     build_formal_jd_prompt,
#     build_job_generation_prompt,
#     parse_branch_match_response,
#     parse_criteria_suggestion_response,
#     parse_evaluation_response,
#     parse_formal_jd_response,
#     parse_job_draft_response,
# )

# DEFAULT_MODEL = "gpt-4o-mini"


# class OpenAIProvider(AIProvider):
#     name = "openai"

#     def __init__(self, api_key: str):
#         self._api_key = api_key

#     def _client(self, timeout_seconds: int):
#         # Imported lazily so the `openai` package is only required if this
#         # provider is actually selected/used - installing it is optional
#         # for anyone running the ATS in weighted-scoring-only mode.
#         from openai import OpenAI

#         return OpenAI(api_key=self._api_key, timeout=timeout_seconds)

#     def _complete_json(self, *, system_prompt: str, user_prompt: str, model: str, timeout_seconds: int) -> str:
#         import openai as openai_sdk

#         client = self._client(timeout_seconds)
#         try:
#             response = client.chat.completions.create(
#                 model=model or DEFAULT_MODEL,
#                 response_format={"type": "json_object"},
#                 temperature=0.2,
#                 messages=[
#                     {"role": "system", "content": system_prompt},
#                     {"role": "user", "content": user_prompt},
#                 ],
#             )
#         except openai_sdk.APITimeoutError as exc:
#             raise AIProviderTimeoutError(f"OpenAI request timed out: {exc}") from exc
#         except openai_sdk.RateLimitError as exc:
#             raise AIProviderRateLimitError(f"OpenAI rate limit hit: {exc}") from exc
#         except openai_sdk.APIError as exc:
#             raise AIProviderError(f"OpenAI API error: {exc}") from exc
#         except Exception as exc:  # network errors, auth errors, etc.
#             raise AIProviderError(f"OpenAI request failed: {exc}") from exc

#         try:
#             content = response.choices[0].message.content
#         except (IndexError, AttributeError) as exc:
#             raise AIProviderInvalidResponseError(f"OpenAI returned an empty/unexpected response: {exc}") from exc
#         if not content:
#             raise AIProviderInvalidResponseError("OpenAI returned an empty response body.")
#         return content

#     def evaluate_candidate(self, *, job_context, candidate_context, model, timeout_seconds, criteria=None) -> AIEvaluationResult:
#         prompt = build_evaluation_prompt(job_context, candidate_context, criteria=criteria)
#         raw = self._complete_json(
#             system_prompt=EVALUATION_SYSTEM_PROMPT, user_prompt=prompt, model=model, timeout_seconds=timeout_seconds
#         )
#         return parse_evaluation_response(
#             raw,
#             provider=self.name,
#             model=model or DEFAULT_MODEL,
#             cv_text_used=bool(candidate_context.get("cv_text")),
#             criteria=criteria,
#         )

#     def generate_job_draft(self, *, title, model, timeout_seconds) -> AIJobDraft:
#         prompt = build_job_generation_prompt(title)
#         raw = self._complete_json(
#             system_prompt=JOB_GENERATION_SYSTEM_PROMPT, user_prompt=prompt, model=model, timeout_seconds=timeout_seconds
#         )
#         return parse_job_draft_response(raw, provider=self.name, model=model or DEFAULT_MODEL)

#     def suggest_screening_criteria(self, *, job_context, model, timeout_seconds) -> AICriteriaSuggestion:
#         prompt = build_criteria_suggestion_prompt(job_context)
#         raw = self._complete_json(
#             system_prompt=CRITERIA_SUGGESTION_SYSTEM_PROMPT,
#             user_prompt=prompt,
#             model=model,
#             timeout_seconds=timeout_seconds,
#         )
#         return parse_criteria_suggestion_response(raw, provider=self.name, model=model or DEFAULT_MODEL)

#     def generate_formal_jd(self, *, job_context, model, timeout_seconds) -> AIFormalJDDraft:
#         prompt = build_formal_jd_prompt(job_context)
#         raw = self._complete_json(
#             system_prompt=FORMAL_JD_SYSTEM_PROMPT, user_prompt=prompt, model=model, timeout_seconds=timeout_seconds
#         )
#         return parse_formal_jd_response(raw, provider=self.name, model=model or DEFAULT_MODEL)

#     def suggest_nearest_branch(self, *, location_text, branches, model, timeout_seconds) -> AIBranchMatch:
#         prompt = build_branch_match_prompt(location_text, branches)
#         raw = self._complete_json(
#             system_prompt=BRANCH_MATCH_SYSTEM_PROMPT, user_prompt=prompt, model=model, timeout_seconds=timeout_seconds
#         )
#         valid_ids = {b["id"] for b in branches}
#         return parse_branch_match_response(raw, valid_branch_ids=valid_ids, provider=self.name, model=model or DEFAULT_MODEL)
