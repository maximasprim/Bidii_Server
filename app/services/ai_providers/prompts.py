"""
Prompt text and response parsing shared by every AI provider.

Kept out of the individual provider files so OpenAI and Gemini are always
evaluating candidates / drafting jobs against the *identical* instructions
and the *identical* validation rules - the only thing that differs between
providers is how the HTTP call itself is made and how JSON-mode is
requested, both isolated inside their respective provider classes.
"""

import json

from app.services.ai_providers.base import (
    AIBranchMatch,
    AICriteriaSuggestion,
    AIEvaluationResult,
    AIFormalJDDraft,
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
    "and return ONLY a JSON object matching the exact schema you're given - no prose, "
    "no markdown fences, no commentary outside the JSON. You assist human recruiters; "
    "you never make a final hiring or rejection decision. Be fair, specific, and base "
    "every judgement strictly on the job posting and the candidate material provided - "
    "never invent qualifications, experience, or details the candidate didn't state. "
    "The candidate's name is deliberately withheld from you and should play no role in "
    "your evaluation - if a name, photo description, or other identity detail happens to "
    "appear inside the raw CV text, ignore it entirely and judge only the substance of "
    "their experience, skills, and qualifications."
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
- "review" = partial fit, or fit is unclear from the material given - a human should look closer.
- "not_recommended" = clearly does not meet the job's core/mandatory requirements.
- Base missing_requirements on what's actually absent from the candidate's cover note and CV text
  (if provided) - do not penalise a candidate just because the CV text wasn't extractable.
"""

# Used instead of EVALUATION_JSON_SCHEMA_HINT whenever the job has
# configured weighted screening criteria (see ATSConfiguration.criteria).
# The model is deliberately NOT asked for an overall score or
# recommendation here - those are computed deterministically afterwards
# from each criterion's met/partial/not_met verdict and its configured
# weight (see ats_scoring.bucket_recommendation, called from
# app/routers/admin_ats_screening.py). That removes the failure mode
# where a model's self-reported score and its self-reported
# recommendation label could disagree with each other, and makes sure
# this job's configured thresholds and mandatory criteria are always
# actually respected in AI mode instead of silently ignored.
CRITERIA_AWARE_JSON_SCHEMA_HINT = """
Return exactly this JSON shape:
{
  "criterion_results": [
    {"criterion_id": "<id exactly as given below>", "status": "<met|partial|not_met>", "detail": "<one short sentence citing what in the candidate material supports this verdict>"}
  ],
  "strengths": ["<short strength>", ...],
  "weaknesses": ["<short weakness or gap>", ...],
  "explanation": "<2-4 sentence overall summary a recruiter can read in a few seconds>"
}
Guidance:
- Return EXACTLY one entry in criterion_results for every criterion listed below, using its exact
  criterion_id - do not add, skip, merge, or invent criteria.
- "met" = clearly satisfied by the candidate's cover note or CV text.
- "partial" = some evidence, but incomplete, weak, or unclear - treat this the same as "not_met" for
  scoring purposes, but say so explicitly in detail so a recruiter can judge for themselves.
- "not_met" = no evidence found, or the candidate explicitly states they don't meet it.
- Do not invent or credit a qualification the candidate didn't actually state.
- Do NOT return an overall score_percentage or recommendation field - the system computes those
  itself from your criterion_results and each criterion's configured weight.
"""


def _criteria_block(criteria: list[dict]) -> str:
    lines = []
    for c in criteria:
        required = " (MANDATORY - failing this alone should drive not_met/failing the candidate overall)" if c.get(
            "is_required"
        ) else ""
        description = f" - {c['description']}" if c.get("description") else ""
        lines.append(f"- criterion_id={c['id']}: {c['label']}{required}{description}")
    return "\n".join(lines)


def build_evaluation_prompt(job_context: dict, candidate_context: dict, criteria: list[dict] | None = None) -> str:
    cv_text = candidate_context.get("cv_text")
    cv_section = (
        f"CANDIDATE CV TEXT (extracted from their uploaded CV):\n{cv_text}\n"
        if cv_text
        else "CANDIDATE CV TEXT: not available (couldn't be extracted) - evaluate using the cover note only, "
        "and don't treat this absence itself as a weakness.\n"
    )
    if criteria:
        criteria_section = (
            "\nSCREENING CRITERIA TO EVALUATE (assess the candidate against EACH of these individually "
            f"- see the JSON shape below):\n{_criteria_block(criteria)}\n"
        )
        schema_hint = CRITERIA_AWARE_JSON_SCHEMA_HINT
    else:
        criteria_section = ""
        schema_hint = EVALUATION_JSON_SCHEMA_HINT
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
Role applied for: {candidate_context.get('role_applied_for')}
Cover note:
{candidate_context.get('cover_note') or '(none provided)'}

{cv_section}
{criteria_section}
{schema_hint}"""


JOB_GENERATION_SYSTEM_PROMPT = (
    "You are a recruitment copywriter for Bidii Credit, a Kenyan lending company with "
    "branches across the country. You draft a complete, realistic job posting from a job "
    "title alone. Return ONLY a JSON object matching the exact schema you're given - no "
    "prose, no markdown fences, no commentary outside the JSON. Write in clear, "
    "professional English suitable for a Kenyan financial-services job board. This is a "
    "DRAFT for a human recruiter to edit before publishing - it is never published "
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
  eligibility/work conditions (e.g. willingness to travel) as applicable to this role - mixed
  together in one flat list, in the same natural style as a typical job board posting.
"""


def build_job_generation_prompt(title: str) -> str:
    return f"""JOB TITLE: {title}

{JOB_GENERATION_JSON_SCHEMA_HINT}"""


CRITERIA_SUGGESTION_SYSTEM_PROMPT = (
    "You are a recruitment screening consultant for Bidii Credit, a Kenyan lending company. "
    "Given one job posting, you propose a starter set of screening criteria a recruiter can use "
    "to weight-score applicants - these are keyword-matching rules, not free-text judgements, so "
    "every criterion needs concrete match_keywords a real applicant might actually write in a cover "
    "note. Return ONLY a JSON object matching the exact schema you're given - no prose, no markdown "
    "fences, no commentary outside the JSON. This is a DRAFT for a human recruiter to review, edit, "
    "and selectively add before it affects any scoring - it is never applied automatically, so it's "
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
- Propose 6-10 criteria in total, drawn from whichever categories actually apply to this job - don't
  force a criterion into a category it doesn't fit, and don't include "location" or "certification"
  unless the posting actually implies one.
- match_keywords: 4-10 concrete lowercase words/phrases each. Matching is literal (no stemming or
  synonym expansion happens automatically), so include multiple realistic variants of how a real
  applicant might phrase the same thing - different word forms (e.g. "manage", "managed",
  "management"), common synonyms (e.g. "credit analysis", "loan appraisal", "underwriting"), and
  both the spelled-out and abbreviated form where relevant (e.g. "certified public accountant",
  "cpa"). Avoid vague single words like "good" or "team".
- is_required: true for at most 1-2 genuinely non-negotiable requirements (e.g. a specific degree or
  license the posting states as mandatory) - most criteria should be false.
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


def parse_evaluation_response(
    raw: str, provider: str, model: str, cv_text_used: bool, criteria: list[dict] | None = None
) -> AIEvaluationResult:
    try:
        data = json.loads(_strip_json_fences(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise AIProviderInvalidResponseError(f"{provider} did not return valid JSON: {exc}") from exc

    if criteria:
        return _parse_criteria_aware_evaluation(data, criteria=criteria, provider=provider, model=model, cv_text_used=cv_text_used)

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
        criteria_aware=False,
    )


def _parse_criteria_aware_evaluation(
    data: dict, *, criteria: list[dict], provider: str, model: str, cv_text_used: bool
) -> AIEvaluationResult:
    """
    Builds matched_criteria/missing_criteria/failed_mandatory_criteria in
    the exact shape app/services/ats_scoring.py produces, and computes
    score_percentage purely from each criterion's configured weight and
    the model's met/not_met verdict for it - the model's own opinion of
    the overall score/recommendation is never requested or trusted (see
    CRITERIA_AWARE_JSON_SCHEMA_HINT). `criteria` is the source of truth
    for which criteria exist: any criterion_id the model didn't address
    is treated as not_met rather than silently dropped, and any
    criterion_id in the response that doesn't match a real criterion is
    ignored.
    """
    try:
        raw_results = data.get("criterion_results", [])
        if not isinstance(raw_results, list):
            raise ValueError("criterion_results must be a list")

        verdict_by_id: dict[str, dict] = {}
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("criterion_id", "")).strip()
            if not cid:
                continue
            verdict_by_id[cid] = {
                "status": str(item.get("status", "")).strip().lower(),
                "detail": str(item.get("detail", "")).strip(),
            }

        matched: list[dict] = []
        missing: list[dict] = []
        failed_mandatory: list[dict] = []
        total_score = 0.0
        max_possible_score = 0.0

        for c in criteria:
            max_possible_score += c["weight"]
            outcome = {
                "criterion_id": c["id"],
                "label": c["label"],
                "category": c["category"],
                "weight": c["weight"],
                "is_required": c["is_required"],
            }
            verdict = verdict_by_id.get(c["id"])
            if verdict is None:
                outcome["detail"] = "AI response didn't address this criterion - treated as not met."
                met = False
            else:
                outcome["detail"] = verdict["detail"]
                met = verdict["status"] == "met"

            if met:
                total_score += c["weight"]
                matched.append(outcome)
            else:
                missing.append(outcome)
                if c["is_required"]:
                    failed_mandatory.append(outcome)

        score_percentage = (total_score / max_possible_score * 100) if max_possible_score > 0 else 0.0
        strengths = [str(s).strip() for s in data.get("strengths", []) if str(s).strip()]
        weaknesses = [str(s).strip() for s in data.get("weaknesses", []) if str(s).strip()]
        explanation = str(data.get("explanation", "")).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise AIProviderInvalidResponseError(f"{provider} returned JSON with an unexpected shape: {exc}") from exc

    return AIEvaluationResult(
        score_percentage=round(score_percentage, 2),
        recommendation="",  # authoritative value is computed by the caller via bucket_recommendation()
        strengths=strengths,
        weaknesses=weaknesses,
        explanation=explanation,
        provider=provider,
        model=model,
        cv_text_used=cv_text_used,
        matched_criteria=matched,
        missing_criteria=missing,
        failed_mandatory_criteria=failed_mandatory,
        criteria_aware=True,
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


FORMAL_JD_SYSTEM_PROMPT = (
    "You are an HR documentation specialist for Bidii Credit, a Kenyan lending company. You draft "
    "the content for the company's standard formal Job Description document for one specific job "
    "posting. The document's fixed layout, headings, and company-wide behavioral competencies are "
    "handled by the system separately - you only draft the role-specific content requested in the "
    "JSON schema below. Return ONLY a JSON object matching that schema - no prose, no markdown "
    "fences, no commentary outside the JSON. Write in clear, professional English matching the "
    "tone of a formal Kenyan corporate HR document. This is a DRAFT for a human recruiter to review "
    "and edit before it's turned into a PDF - it is never issued automatically."
)

FORMAL_JD_JSON_SCHEMA_HINT = """
Return exactly this JSON shape:
{
  "overall_role_purpose": "<one sentence stating the role's overall purpose, e.g. 'To deliver regional commercial targets'>",
  "reports_to": "<job title of the position this role reports to, e.g. 'General Manager - Commercial'>",
  "key_responsibilities": [
    {
      "heading": "<short heading for this responsibility area, e.g. 'Loan Book Quality Management'>",
      "bullets": ["<specific responsibility 1>", "<specific responsibility 2>", ...],
      "pct_time": <integer percentage of time spent on this area>,
      "criteria": ["<measurable performance criterion 1>", "<measurable performance criterion 2>", ...]
    }
  ],
  "reporting_relationships": "<one line: which roles/teams report to this position, or 'None' if individual contributor>",
  "decision_making_mandates": "<one line: what this role can decide/authorize on its own>",
  "planning_responsibility": "<one line: what this role is responsible for planning>",
  "relationship_management": "<one line: which departments/stakeholders this role routinely works with>",
  "minimum_qualifications": ["<qualification 1, e.g. a degree/diploma requirement>", ...],
  "experience_and_skills": ["<experience or skill requirement 1>", ...]
}
Guidance:
- key_responsibilities: 4-6 entries. Each pct_time is a whole number and all entries' pct_time
  values must sum to exactly 100.
- Each key_responsibilities entry needs 1-4 bullets and 1-4 measurable criteria.
- minimum_qualifications: 2-4 items (education/certification level, years of relevant experience,
  any required tools/software).
- experience_and_skills: 4-6 items (soft skills, technical skills, track record expectations).
- Base everything on the job's actual title, department, description, requirements, and
  responsibilities given below - don't invent responsibilities unrelated to this specific role.
"""


def build_formal_jd_prompt(job_context: dict) -> str:
    return f"""JOB POSTING
Title: {job_context.get('title')}
Department: {job_context.get('department')}
Location: {job_context.get('location')}
Employment type: {job_context.get('employment_type')}
Description: {job_context.get('description')}
Responsibilities (as currently posted publicly):
{_bullet_list(job_context.get('responsibilities') or [])}
Requirements / qualifications / skills / experience / eligibility (as currently posted publicly):
{_bullet_list(job_context.get('requirements') or [])}

{FORMAL_JD_JSON_SCHEMA_HINT}"""


def parse_formal_jd_response(raw: str, provider: str, model: str) -> AIFormalJDDraft:
    try:
        data = json.loads(_strip_json_fences(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise AIProviderInvalidResponseError(f"{provider} did not return valid JSON: {exc}") from exc

    try:
        overall_role_purpose = str(data["overall_role_purpose"]).strip()
        reports_to = str(data.get("reports_to", "")).strip()
        key_responsibilities = []
        for item in data.get("key_responsibilities", []):
            heading = str(item.get("heading", "")).strip()
            bullets = [str(b).strip() for b in item.get("bullets", []) if str(b).strip()]
            criteria = [str(c).strip() for c in item.get("criteria", []) if str(c).strip()]
            pct_time = item.get("pct_time", 0)
            if not heading or not bullets:
                continue
            key_responsibilities.append(
                {"heading": heading, "bullets": bullets, "pct_time": int(pct_time), "criteria": criteria}
            )
        if not overall_role_purpose or not key_responsibilities:
            raise ValueError("missing overall_role_purpose or key_responsibilities")

        reporting_relationships = str(data.get("reporting_relationships", "")).strip()
        decision_making_mandates = str(data.get("decision_making_mandates", "")).strip()
        planning_responsibility = str(data.get("planning_responsibility", "")).strip()
        relationship_management = str(data.get("relationship_management", "")).strip()
        minimum_qualifications = [str(q).strip() for q in data.get("minimum_qualifications", []) if str(q).strip()]
        experience_and_skills = [str(s).strip() for s in data.get("experience_and_skills", []) if str(s).strip()]
    except (KeyError, TypeError, ValueError) as exc:
        raise AIProviderInvalidResponseError(f"{provider} returned JSON with an unexpected shape: {exc}") from exc

    return AIFormalJDDraft(
        overall_role_purpose=overall_role_purpose,
        reports_to=reports_to,
        key_responsibilities=key_responsibilities,
        reporting_relationships=reporting_relationships,
        decision_making_mandates=decision_making_mandates,
        planning_responsibility=planning_responsibility,
        relationship_management=relationship_management,
        minimum_qualifications=minimum_qualifications,
        experience_and_skills=experience_and_skills,
        provider=provider,
        model=model,
    )


BRANCH_MATCH_SYSTEM_PROMPT = (
    "You are a logistics assistant for Bidii Credit, a Kenyan lending company with physical branches. "
    "Given a loan applicant's own description of where they are and a list of the company's active "
    "branches (each with an id and address), you pick the ONE branch that is geographically closest to "
    "the applicant, using your knowledge of Kenyan geography. Return ONLY a JSON object matching the "
    "exact schema you're given - no prose, no markdown fences, no commentary outside the JSON."
)

BRANCH_MATCH_JSON_SCHEMA_HINT = """
Return exactly this JSON shape:
{
  "branch_id": "<the id of the single closest branch, copied exactly from the list below>",
  "reasoning": "<one short sentence explaining the geographic reasoning>"
}
You MUST pick one of the branch ids given below, even if none are a close match - always pick
whichever is nearest given your knowledge of Kenyan geography. Never invent a branch id.
"""


def build_branch_match_prompt(location_text: str, branches: list[dict]) -> str:
    branch_lines = "\n".join(f"- id={b['id']}: {b['name']} - {b['address']}" for b in branches)
    return f"""APPLICANT'S STATED LOCATION
{location_text}

ACTIVE BRANCHES
{branch_lines}

{BRANCH_MATCH_JSON_SCHEMA_HINT}"""


def parse_branch_match_response(raw: str, *, valid_branch_ids: set[str], provider: str, model: str) -> AIBranchMatch:
    try:
        data = json.loads(_strip_json_fences(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise AIProviderInvalidResponseError(f"{provider} did not return valid JSON: {exc}") from exc

    try:
        branch_id = str(data["branch_id"]).strip()
        reasoning = str(data.get("reasoning", "")).strip()
        if branch_id not in valid_branch_ids:
            raise ValueError(f"branch_id {branch_id!r} is not one of the branches given")
    except (KeyError, TypeError, ValueError) as exc:
        raise AIProviderInvalidResponseError(f"{provider} returned JSON with an unexpected shape: {exc}") from exc

    return AIBranchMatch(branch_id=branch_id, reasoning=reasoning, provider=provider, model=model)
