"""
Prompt text and response parsing shared by every AI provider.

Kept out of the individual provider files so OpenAI and Gemini are always
evaluating candidates / drafting jobs against the *identical* instructions
and the *identical* validation rules — the only thing that differs between
providers is how the HTTP call itself is made and how JSON-mode is
requested, both isolated inside their respective provider classes.
"""

import json

from app.services.ai_providers.base import (
    AICriteriaSuggestion,
    AIEvaluationResult,
    AIJobDraft,
    AIProviderInvalidResponseError,
    AIRequirementOutcome,
    AISuggestedCriterion,
)

VALID_CRITERION_CATEGORIES = {
    "qualification",
    "education",
    "experience",
    "skill",
    "certification",
    "location",
    "custom",
}

EVALUATION_SYSTEM_PROMPT = (
    "You are an impartial recruitment screening assistant for Bidii Credit, a Kenyan "
    "lending company. You evaluate a single job applicant against a single job posting "
    "and return ONLY a JSON object matching the exact schema you're given — no prose, "
    "no markdown fences, no commentary outside the JSON. You assist human recruiters; "
    "you never make a final hiring or rejection decision. Be fair, specific, and base "
    "every judgement strictly on the job posting and the candidate material provided — "
    "never invent qualifications, experience, or details the candidate didn't state."
)

EVALUATION_JSON_SCHEMA_HINT = """
Return exactly this JSON shape:
{
  "score_percentage": <number 0-100, how well the candidate fits this specific job>,
  "recommendation": "<one of: recommended, review, not_recommended>",
  "matched_requirements": [{"label": "<short requirement text>", "detail": "<why it's met>"}],
  "missing_requirements": [{"label": "<short requirement text>", "detail": "<why it's unmet/unclear>"}],
  "strengths": ["<short strength>", ...],
  "weaknesses": ["<short weakness or gap>", ...],
  "explanation": "<2-4 sentence overall summary a recruiter can read in a few seconds>"
}
Guidance:
- "recommended" = strong fit on the job's core requirements.
- "review" = partial fit, or fit is unclear from the material given — a human should look closer.
- "not_recommended" = clearly does not meet the job's core/mandatory requirements.
- Base missing_requirements on what's actually absent from the candidate's cover note and CV text
  (if provided) — do not penalise a candidate just because the CV text wasn't extractable.
"""


def build_evaluation_prompt(job_context: dict, candidate_context: dict) -> str:
    cv_text = candidate_context.get("cv_text")
    cv_section = (
        f"CANDIDATE CV TEXT (extracted from their uploaded CV):\n{cv_text}\n"
        if cv_text
        else "CANDIDATE CV TEXT: not available (couldn't be extracted) — evaluate using the cover note only, "
        "and don't treat this absence itself as a weakness.\n"
    )
    return f"""JOB POSTING
Title: {job_context.get('title')}
Department: {job_context.get('department')}
Location: {job_context.get('location')}
Employment type: {job_context.get('employment_type')}
Description: {job_context.get('description')}
Responsibilities:
{_bullet_list(job_context.get('responsibilities') or [])}
Requirements / qualifications / skills / experience / eligibility:
{_bullet_list(job_context.get('requirements') or [])}

CANDIDATE
Name: {candidate_context.get('full_name')}
Role applied for: {candidate_context.get('role_applied_for')}
Cover note:
{candidate_context.get('cover_note') or '(none provided)'}

{cv_section}
{EVALUATION_JSON_SCHEMA_HINT}"""


JOB_GENERATION_SYSTEM_PROMPT = (
    "You are a recruitment copywriter for Bidii Credit, a Kenyan lending company with "
    "branches across the country. You draft a complete, realistic job posting from a job "
    "title alone. Return ONLY a JSON object matching the exact schema you're given — no "
    "prose, no markdown fences, no commentary outside the JSON. Write in clear, "
    "professional English suitable for a Kenyan financial-services job board. This is a "
    "DRAFT for a human recruiter to edit before publishing — it is never published "
    "automatically, so it's fine (expected, even) for them to change details like exact "
    "location or department."
)

JOB_GENERATION_JSON_SCHEMA_HINT = """
Return exactly this JSON shape:
{
  "summary": "<one-sentence hook, ~20-30 words>",
  "description": "<2-4 sentence paragraph describing the role and its purpose at the company>",
  "responsibilities": ["<responsibility 1>", "<responsibility 2>", ...],
  "requirements": ["<requirement/qualification/skill/experience/eligibility item 1>", ...]
}
Guidance:
- responsibilities: 5-9 concrete, action-oriented bullet points.
- requirements: 5-10 bullet points covering education, relevant experience, skills, and any
  eligibility/work conditions (e.g. willingness to travel) as applicable to this role — mixed
  together in one flat list, in the same natural style as a typical job board posting.
"""


def build_job_generation_prompt(title: str) -> str:
    return f"""JOB TITLE: {title}

{JOB_GENERATION_JSON_SCHEMA_HINT}"""


CRITERIA_SUGGESTION_SYSTEM_PROMPT = (
    "You are a recruitment screening consultant for Bidii Credit, a Kenyan lending company. "
    "Given one job posting, you propose a starter set of screening criteria a recruiter can use "
    "to weight-score applicants — these are keyword-matching rules, not free-text judgements, so "
    "every criterion needs concrete match_keywords a real applicant might actually write in a cover "
    "note. Return ONLY a JSON object matching the exact schema you're given — no prose, no markdown "
    "fences, no commentary outside the JSON. This is a DRAFT for a human recruiter to review, edit, "
    "and selectively add before it affects any scoring — it is never applied automatically, so it's "
    "fine (expected, even) for them to change labels, weights, or drop criteria that don't fit."
)

CRITERIA_SUGGESTION_JSON_SCHEMA_HINT = """
Return exactly this JSON shape:
{
  "criteria": [
    {
      "category": "<one of: qualification, education, experience, skill, certification, location, custom>",
      "label": "<short human-readable name, e.g. '2+ years lending experience'>",
      "description": "<one short sentence a recruiter would see, optional>",
      "match_keywords": ["<keyword or short phrase an applicant might write>", ...],
      "weight": <number 1-30, roughly how much this should count toward the total score>,
      "is_required": <true only if failing this should flag the candidate as not meeting a mandatory bar>
    }
  ]
}
Guidance:
- Propose 6-10 criteria in total, drawn from whichever categories actually apply to this job — don't
  force a criterion into a category it doesn't fit, and don't include "location" or "certification"
  unless the posting actually implies one.
- match_keywords: 3-8 concrete lowercase words/phrases each, specific enough to plausibly appear in a
  real cover note (e.g. "credit analysis", "loan officer", "excel") — avoid vague single words like
  "good" or "team".
- is_required: true for at most 1-2 genuinely non-negotiable requirements (e.g. a specific degree or
  license the posting states as mandatory) — most criteria should be false.
- weight: reflect relative importance across the set (e.g. core experience higher than a nice-to-have skill).
"""


def build_criteria_suggestion_prompt(job_context: dict) -> str:
    return f"""JOB POSTING
Title: {job_context.get('title')}
Department: {job_context.get('department')}
Location: {job_context.get('location')}
Employment type: {job_context.get('employment_type')}
Description: {job_context.get('description')}
Responsibilities:
{_bullet_list(job_context.get('responsibilities') or [])}
Requirements / qualifications / skills / experience / eligibility:
{_bullet_list(job_context.get('requirements') or [])}

{CRITERIA_SUGGESTION_JSON_SCHEMA_HINT}"""


def parse_criteria_suggestion_response(raw: str, provider: str, model: str) -> AICriteriaSuggestion:
    try:
        data = json.loads(_strip_json_fences(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise AIProviderInvalidResponseError(f"{provider} did not return valid JSON: {exc}") from exc

    try:
        raw_criteria = data["criteria"]
        if not isinstance(raw_criteria, list) or not raw_criteria:
            raise ValueError("criteria must be a non-empty list")

        criteria: list[AISuggestedCriterion] = []
        for item in raw_criteria:
            category = str(item.get("category", "")).strip().lower()
            label = str(item.get("label", "")).strip()
            if not label:
                continue
            if category not in VALID_CRITERION_CATEGORIES:
                category = "custom"
            keywords = [str(k).strip().lower() for k in item.get("match_keywords", []) if str(k).strip()]
            weight = max(0.0, min(100.0, float(item.get("weight", 10) or 10)))
            criteria.append(
                AISuggestedCriterion(
                    category=category,
                    label=label,
                    description=str(item.get("description", "") or "").strip(),
                    match_keywords=keywords,
                    weight=round(weight, 2),
                    is_required=bool(item.get("is_required", False)),
                )
            )
        if not criteria:
            raise ValueError("no usable criteria in response")
    except (KeyError, TypeError, ValueError) as exc:
        raise AIProviderInvalidResponseError(f"{provider} returned JSON with an unexpected shape: {exc}") from exc

    return AICriteriaSuggestion(criteria=criteria, provider=provider, model=model)


def _bullet_list(items: list[str]) -> str:
    if not items:
        return "(none listed)"
    return "\n".join(f"- {item}" for item in items)


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return text.strip()


def parse_evaluation_response(raw: str, provider: str, model: str, cv_text_used: bool) -> AIEvaluationResult:
    try:
        data = json.loads(_strip_json_fences(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise AIProviderInvalidResponseError(f"{provider} did not return valid JSON: {exc}") from exc

    try:
        score = float(data["score_percentage"])
        recommendation = str(data["recommendation"]).strip().lower()
        if recommendation not in {"recommended", "review", "not_recommended"}:
            raise ValueError(f"unexpected recommendation value {recommendation!r}")
        score = max(0.0, min(100.0, score))

        matched = [
            AIRequirementOutcome(label=str(item.get("label", "")).strip(), detail=str(item.get("detail", "")).strip())
            for item in data.get("matched_requirements", [])
            if str(item.get("label", "")).strip()
        ]
        missing = [
            AIRequirementOutcome(label=str(item.get("label", "")).strip(), detail=str(item.get("detail", "")).strip())
            for item in data.get("missing_requirements", [])
            if str(item.get("label", "")).strip()
        ]
        strengths = [str(s).strip() for s in data.get("strengths", []) if str(s).strip()]
        weaknesses = [str(s).strip() for s in data.get("weaknesses", []) if str(s).strip()]
        explanation = str(data.get("explanation", "")).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise AIProviderInvalidResponseError(f"{provider} returned JSON with an unexpected shape: {exc}") from exc

    return AIEvaluationResult(
        score_percentage=round(score, 2),
        recommendation=recommendation,
        matched_requirements=matched,
        missing_requirements=missing,
        strengths=strengths,
        weaknesses=weaknesses,
        explanation=explanation,
        provider=provider,
        model=model,
        cv_text_used=cv_text_used,
    )


def parse_job_draft_response(raw: str, provider: str, model: str) -> AIJobDraft:
    try:
        data = json.loads(_strip_json_fences(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise AIProviderInvalidResponseError(f"{provider} did not return valid JSON: {exc}") from exc

    try:
        summary = str(data["summary"]).strip()
        description = str(data["description"]).strip()
        responsibilities = [str(r).strip() for r in data.get("responsibilities", []) if str(r).strip()]
        requirements = [str(r).strip() for r in data.get("requirements", []) if str(r).strip()]
        if not description or not requirements:
            raise ValueError("missing description or requirements")
    except (KeyError, TypeError, ValueError) as exc:
        raise AIProviderInvalidResponseError(f"{provider} returned JSON with an unexpected shape: {exc}") from exc

    return AIJobDraft(
        summary=summary,
        description=description,
        responsibilities=responsibilities,
        requirements=requirements,
        provider=provider,
        model=model,
    )
