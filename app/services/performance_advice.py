"""
AI advice on top of the rules - the AI only ADVISES. It never sets a score, rating, status or PIP.

* Input is pseudonymised and already computed: an alias ("Staff A"), the role name, the period, the score/category/achievement the system
  calculated, the monthly achievement series, per-KPI achievement, the rule outputs and (optionally) the projection. No names, phone numbers or ID numbers.
* Output must be strict JSON matching ADVICE_SHAPE. Any number written in the summary, causes, actions or training that does not appear in the
  input is grounds to REJECT the whole answer (timeframes are exempt, they are advice, not facts).
* On any failure (no provider, error, invalid JSON, a number that wasn't supplied) the caller shows the rule text instead.
* Cached by input hash (a "regenerate" flag bypasses it); provider, model and prompt version are stored in ai_outputs and shown with the label.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.orm import Session

from app.services import performance_ai as ai

PROMPT_VERSION = "perf-advice-v1"
LABEL = "AI-generated; review before acting"
OWNERS = {"manager", "staff", "hr", "gm", "rm", "branch manager"}
CONFIDENCE = {"low", "medium", "high"}
_NUM = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?")

_SYSTEM = (
    "You advise a manager on how to support a staff member, a team or a branch, using ONLY the numbers and rule findings supplied. "
    "Do not invent figures. Do not decide ratings, scores, warnings or PIPs; you may suggest conversations and support. "
    "Respond with one JSON object and nothing else: "
    '{"summary": str, "root_causes": [str, ...up to 4], "actions": [{"action": str, "owner": str, "timeframe": str}, ...3 to 5], "training": [str, ...up to 3], "confidence": "low"|"medium"|"high"}. '
    f"owner is one of {sorted(OWNERS)}. Refer to the subject only by the alias given. Write plainly, one or two sentences per item."
)


def _numbers_in(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(round(float(obj), 1))
    elif isinstance(obj, str):
        for m in _NUM.findall(obj):
            out.add(round(float(m.replace(",", ".")), 1))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers_in(v, out)


def allowed_numbers(payload: dict[str, Any]) -> set[float]:
    nums: set[float] = {float(n) for n in range(0, 13)}  # small counts ("two steps", "3 actions") are harmless
    _numbers_in(payload, nums)
    return nums


def _texts(advice: dict[str, Any]) -> list[str]:
    return [advice["summary"], *advice["root_causes"], *[a["action"] for a in advice["actions"]], *advice["training"]]


def validate(raw: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = ai._parse_json_object(raw)  # noqa: SLF001
    try:
        summary = str(data["summary"]).strip()
        causes = [str(x).strip() for x in data.get("root_causes", [])][:4]
        training = [str(x).strip() for x in data.get("training", [])][:3]
        conf = str(data.get("confidence", "")).lower()
        actions = []
        for a in data["actions"]:
            owner = str(a["owner"]).strip().lower()
            actions.append({"action": str(a["action"]).strip()[:300], "owner": owner, "timeframe": str(a.get("timeframe", "")).strip()[:60]})
    except (KeyError, TypeError, AttributeError):
        raise ai.AIOutputInvalid("The AI's answer didn't have the expected shape.") from None
    if not summary or not (3 <= len(actions) <= 5) or conf not in CONFIDENCE or any(a["owner"] not in OWNERS or not a["action"] for a in actions):
        raise ai.AIOutputInvalid("The AI's answer didn't have the expected shape.")
    advice = {"summary": summary[:600], "root_causes": [c[:300] for c in causes if c], "actions": actions, "training": [t[:200] for t in training if t], "confidence": conf}
    allowed = allowed_numbers(payload)
    for text in _texts(advice):
        for m in _NUM.findall(text):
            if round(float(m.replace(",", ".")), 1) not in allowed:
                raise ai.AIOutputInvalid("The AI's answer contained a figure that isn't in the data, so it was rejected.")
    return advice


def fallback(title: str, detail: str) -> dict[str, Any]:
    return {"source": "rules", "summary": f"{title}. {detail}", "root_causes": [], "actions": [], "training": [], "confidence": None, "label": "Rules-based suggestion (no AI used)"}


def advise(db: Session, payload: dict[str, Any], *, title: str, detail: str, username: str, regenerate: bool = False) -> dict[str, Any]:
    """AI advice for `payload`, or the rule text if anything goes wrong. Never raises for an AI problem."""
    input_hash = ai._hash(payload)  # noqa: SLF001
    if not regenerate:
        hit = ai._cached(db, "advice", input_hash)  # noqa: SLF001
        if hit:
            return {**hit.output, "source": "ai", "ai": ai._ai_meta(hit.provider, hit.model, hit.prompt_version, True), "label": LABEL}  # noqa: SLF001
    try:
        raw, provider, model = ai.complete_json(_SYSTEM, "DATA:\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
        advice = validate(raw, payload)
    except (ai.AIUnavailable, ai.AIFailed, ai.AIOutputInvalid) as exc:
        return {**fallback(title, detail), "why_fallback": str(exc)}
    ai._store(db, "advice", input_hash, provider, model, PROMPT_VERSION, advice, username)  # noqa: SLF001
    return {**advice, "source": "ai", "ai": ai._ai_meta(provider, model, PROMPT_VERSION, False), "label": LABEL}  # noqa: SLF001
