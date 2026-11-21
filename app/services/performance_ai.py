"""
AI assistance for the JD builder: "AI drafts, a human saves."

* Nothing here saves a JD or a KPI. Results are drafts returned to the admin.
* AI never sets a number. KPI weights are computed here from the responsibility
  weights the admin already approved; the model only proposes wording and kinds.
* Only a JD's text (cut before its approvals block, contact details masked - see
  performance_upload.redact_for_ai) or responsibility titles/criteria go to the provider.
* Output is parsed as strict JSON and validated; anything off raises AIOutputInvalid.
  KPI suggestion falls back to a plain rules-based proposal; JD extraction has no
  honest fallback, so it reports the failure and the admin enters the JD by hand.
* Every output is stored in ai_outputs with provider, model and prompt version, and
  cached by input hash (a "regenerate" flag bypasses the cache).

Coupling note: this uses each provider's `_complete_json`, so no existing provider
file had to change. A test asserts both providers still have it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.performance import AIOutput
from app.services.ai_providers.base import AIProviderError, AIProviderNotConfiguredError
from app.services.ai_providers.factory import default_model_for, first_configured_provider, get_provider
from app.services.performance_roles import (
    KPI_DIRECTIONS,
    KPI_FREQUENCIES,
    KPI_SOURCES,
    KPI_UNITS,
    slugify,
    validate_jd,
)

logger = logging.getLogger("bidii.performance.ai")

PROMPT_JD_EXTRACT = "perf-jd-extract-v1"
PROMPT_KPI_SUGGEST = "perf-kpi-suggest-v1"

_JD_SYSTEM = (
    "You extract a structured job description from the text of a job description document. "
    "Use ONLY what the text says; never invent content. If a field is absent use an empty string or empty list. "
    "Respond with a single JSON object and nothing else, with exactly these keys: "
    '"title","reports_to","branch","department","grade","date","purpose","mandates","planning","relationships" (strings); '
    '"responsibilities": a list of {"title","weight_pct","description","criteria"} where weight_pct is the "% of time" '
    "number from the text (a number, or null if none is given) and criteria is a list of performance-measurement strings; "
    '"qualifications": a list of strings; "competencies": a list of {"title","description"}. '
    "Never include any person's name, signature, email or phone number."
)
_KPI_SYSTEM = (
    "You propose measurable KPIs for the key responsibilities of a job. Respond with a single JSON object and nothing else: "
    '{"kpis":[{"responsibility_index":int,"label":str,"description":str,"unit":str,"direction":str,"source":str,"frequency":str}]}. '
    f"unit is one of {KPI_UNITS}; direction is one of {KPI_DIRECTIONS}; source is one of {KPI_SOURCES}; frequency is one of {KPI_FREQUENCIES}. "
    "Propose 1 to 2 KPIs per responsibility, using the criteria given. Do NOT output weights or targets."
)


class AIUnavailable(Exception):
    """No AI provider is configured on the server."""


class AIFailed(Exception):
    """The provider errored or timed out."""


class AIOutputInvalid(Exception):
    """The provider answered, but not with something usable."""


def complete_json(system_prompt: str, user_prompt: str) -> tuple[str, str, str]:
    """(raw_text, provider, model). The single seam tests replace; everything else is deterministic."""
    name = first_configured_provider()
    if name is None:
        raise AIUnavailable("No AI provider is configured on the server. Enter this by hand, or ask an administrator to add an API key.")
    model = default_model_for(name)
    try:
        raw = get_provider(name)._complete_json(  # noqa: SLF001 - see module docstring
            system_prompt=system_prompt, user_prompt=user_prompt, model=model,
            timeout_seconds=get_settings().ai_request_timeout_seconds,
        )
    except AIProviderNotConfiguredError as exc:
        raise AIUnavailable(str(exc)) from exc
    except AIProviderError as exc:
        logger.warning("Performance AI call failed (%s)", type(exc).__name__)
        raise AIFailed("The AI provider couldn't complete that request. Please try again, or enter it by hand.") from exc
    return raw, name, model


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise AIOutputInvalid("The AI's answer wasn't valid JSON.") from None
    if not isinstance(data, dict):
        raise AIOutputInvalid("The AI's answer wasn't a JSON object.")
    return data


def _hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _cached(db: Session, kind: str, input_hash: str) -> AIOutput | None:
    return db.query(AIOutput).filter_by(kind=kind, input_hash=input_hash).order_by(AIOutput.created_at.desc()).first()


def _store(db: Session, kind: str, input_hash: str, provider: str, model: str, prompt_version: str, output: dict, username: str) -> None:
    db.add(AIOutput(kind=kind, input_hash=input_hash, provider=provider, model=model, prompt_version=prompt_version, output=output, created_by=username))
    db.commit()


def _ai_meta(provider: str, model: str, prompt_version: str, cached: bool) -> dict[str, Any]:
    return {"provider": provider, "model": model, "prompt_version": prompt_version, "cached": cached, "label": "AI-generated; review before saving"}


# ---- JD extraction ---------------------------------------------------------------


def _coerce_jd(data: dict[str, Any]) -> dict[str, Any]:
    """Tolerate the common ways models bend the format (weights like '30%', competencies as strings)."""
    def num(v):
        if isinstance(v, str):
            m = re.search(r"\d+(?:\.\d+)?", v)
            return float(m.group()) if m else 0
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    jd = dict(data)
    resps = []
    for r in jd.get("responsibilities") or []:
        if isinstance(r, dict):
            crit = r.get("criteria")
            resps.append({**r, "weight_pct": num(r.get("weight_pct")), "criteria": [crit] if isinstance(crit, str) else crit})
    jd["responsibilities"] = resps
    comps = []
    for c in jd.get("competencies") or []:
        comps.append({"title": c} if isinstance(c, str) else c)
    jd["competencies"] = comps
    for key in ("qualifications",):
        if isinstance(jd.get(key), str):
            jd[key] = [jd[key]]
    return jd


def parse_jd_draft(raw: str) -> tuple[dict[str, Any], list[str]]:
    """(draft_jd, warnings). The draft is cleaned to the JD schema; weight problems are warnings, not errors."""
    data = _coerce_jd(_parse_json_object(raw))
    warnings: list[str] = []
    jd = validate_jd(data, warnings)
    if not jd or not (jd.get("title") or jd.get("responsibilities")):
        raise AIOutputInvalid("No job description could be found in that document.")
    return jd, warnings


def extract_jd_draft(db: Session, redacted_text: str, username: str, *, regenerate: bool = False) -> dict[str, Any]:
    input_hash = _hash(redacted_text)
    if not regenerate:
        hit = _cached(db, "jd_extraction", input_hash)
        if hit:
            return {**hit.output, "ai": _ai_meta(hit.provider, hit.model, hit.prompt_version, True)}
    raw, provider, model = complete_json(_JD_SYSTEM, f"JOB DESCRIPTION TEXT:\n\n{redacted_text}")
    jd, warnings = parse_jd_draft(raw)
    output = {"jd": jd, "warnings": warnings}
    _store(db, "jd_extraction", input_hash, provider, model, PROMPT_JD_EXTRACT, output, username)
    return {**output, "ai": _ai_meta(provider, model, PROMPT_JD_EXTRACT, False)}


# ---- KPI suggestion --------------------------------------------------------------


def split_weight(total: Decimal, parts: int) -> list[Decimal]:
    """Splits `total` into `parts` two-decimal shares that add up to exactly `total` (remainder to the first)."""
    base = (total / parts).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    shares = [base] * parts
    shares[0] += total - base * parts
    return shares


def _unique_key(label: str, used: set[str]) -> str:
    base = slugify(label) or "kpi"
    if not base[0].isalpha():
        base = f"kpi_{base}"
    key, n = base[:56], 2
    while key in used:
        key = f"{base[:52]}_{n}"
        n += 1
    used.add(key)
    return key


def _build_kpis(resps: list[dict[str, Any]], proposals: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    used: set[str] = set()
    out: list[dict[str, Any]] = []
    for i, r in enumerate(resps):
        props = proposals.get(i) or [{"label": r["title"]}]
        for share, p in zip(split_weight(Decimal(str(r["weight_pct"])), len(props)), props):
            label = str(p.get("label") or r["title"]).strip()[:200] or r["title"]
            out.append({
                "key": _unique_key(label, used), "label": label,
                "description": (str(p.get("description")).strip()[:1000] or None) if p.get("description") else None,
                "unit": p.get("unit") if p.get("unit") in KPI_UNITS else "rating",
                "direction": p.get("direction") if p.get("direction") in KPI_DIRECTIONS else "higher",
                "weight": float(share),
                "source": p.get("source") if p.get("source") in KPI_SOURCES else "appraisal_rating",
                "frequency": p.get("frequency") if p.get("frequency") in KPI_FREQUENCIES else "quarterly",
                "cap_pct": None, "jd_section": r["title"],
            })
    return out


def parse_kpi_proposals(raw: str, n_resps: int) -> dict[int, list[dict[str, Any]]]:
    data = _parse_json_object(raw)
    items = data.get("kpis")
    if not isinstance(items, list):
        raise AIOutputInvalid("The AI's answer had no KPI list.")
    proposals: dict[int, list[dict[str, Any]]] = {}
    for it in items:
        idx = it.get("responsibility_index") if isinstance(it, dict) else None
        if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < n_resps or not str(it.get("label", "")).strip():
            continue  # ignore entries that don't map onto a real responsibility
        proposals.setdefault(idx, [])
        if len(proposals[idx]) < 3:
            proposals[idx].append(it)
    if not proposals:
        raise AIOutputInvalid("The AI proposed no usable KPIs.")
    return proposals


def suggest_kpis(db: Session, responsibilities: list[dict[str, Any]], username: str, *, regenerate: bool = False) -> dict[str, Any]:
    """Proposed KPI rows for the admin to edit. Weights always come from the responsibilities, never from the AI."""
    resps = [{"title": r["title"], "weight_pct": r["weight_pct"], "criteria": r.get("criteria", [])} for r in responsibilities]
    total = sum((Decimal(str(r["weight_pct"])) for r in resps), Decimal(0))
    warnings = [] if abs(total - 100) <= Decimal("0.01") else [f"Responsibility weights add up to {total.normalize():f}, not 100; fix them before saving."]
    prompt_payload = [{"index": i, "title": r["title"], "criteria": r["criteria"]} for i, r in enumerate(resps)]  # no weights go out
    input_hash = _hash(prompt_payload)

    meta: dict[str, Any] | None = None
    proposals: dict[int, list[dict[str, Any]]] | None = None
    fallback_reason: str | None = None
    hit = None if regenerate else _cached(db, "kpi_suggestion", input_hash)
    try:
        if hit:
            proposals = {int(k): v for k, v in hit.output["proposals"].items()}
            meta = _ai_meta(hit.provider, hit.model, hit.prompt_version, True)
        else:
            raw, provider, model = complete_json(_KPI_SYSTEM, "KEY RESPONSIBILITIES:\n" + json.dumps(prompt_payload, ensure_ascii=False))
            proposals = parse_kpi_proposals(raw, len(resps))
            _store(db, "kpi_suggestion", input_hash, provider, model, PROMPT_KPI_SUGGEST, {"proposals": {str(k): v for k, v in proposals.items()}}, username)
            meta = _ai_meta(provider, model, PROMPT_KPI_SUGGEST, False)
    except (AIUnavailable, AIFailed, AIOutputInvalid) as exc:
        fallback_reason = str(exc)
        proposals = None

    return {
        "kpis": _build_kpis(resps, proposals or {}),
        "ai_used": proposals is not None,
        "ai": meta,
        "fallback_reason": fallback_reason,
        "warnings": warnings,
    }
