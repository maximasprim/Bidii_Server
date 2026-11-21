"""
Filled appraisal form -> a PROPOSED set of fields that a human reviews. "AI drafts, a human confirms."

* Nothing here changes an appraisal. `propose` returns a draft stored on the appraisal as `extraction` with
  extraction_status "extracted"; only `apply_confirmed` (called after a person has reviewed and edited it) writes ratings and text.
* The document text is pseudonymised before it leaves the server: the appraisee and their managers become "Staff A" / "Manager B",
  e-mails and long numbers are masked, and the provider only sees the KPI labels, weights and keys of THIS appraisal.
* The AI's answer is strict JSON, validated against this appraisal's own rows. Unknown keys are dropped, ratings outside 1-5 are
  dropped, every row carries a confidence (0-1) and anything under LOW_CONFIDENCE is flagged for the reviewer. No honest fallback
  exists for reading a form, so a failure leaves the file attached and says "enter it manually".
* Provider, model and prompt version are recorded on the draft and in ai_outputs; results are cached by input hash.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from sqlalchemy.orm import Session

from app.models.performance import Appraisal, StaffProfile
from app.services import performance_ai as ai
from app.services import performance_appraisals as pa
from app.services.performance_upload import MAX_AI_TEXT_CHARS

PROMPT_VERSION = "perf-appraisal-extract-v1"
LOW_CONFIDENCE = 0.7
MAX_TEXT = 4000
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d\s-]{7,}\d)(?!\d)")
_LONG_NUMBER = re.compile(r"\b\d{7,}\b")

_SYSTEM = (
    "You read the text of a FILLED staff appraisal form and copy what is written into a fixed structure. "
    "Use ONLY what the text says; never invent, estimate or calculate anything. If something is absent, omit it. "
    "Respond with one JSON object and nothing else, with exactly these keys: "
    '"items": [{"kpi_key": str, "self_text": str|null, "self_rating": int|null, "manager_rating": int|null, "confidence": number}], '
    '"competencies": [{"key": str, "self_rating": int|null, "manager_rating": int|null, "confidence": number}], '
    '"objectives": [{"label": str, "values": [str, str, str], "confidence": number}], '
    '"development_needs": [{"slot": 1 or 2, "text": str, "confidence": number}], '
    '"comments": {"appraisee": str|null, "manager": str|null, "rm_gm": str|null, "hr": str|null, "ceo": str|null}. '
    "Ratings are whole numbers 1 to 5 exactly as written on the form. confidence is between 0 and 1 and reflects how clearly the "
    "text states that value. Use the kpi_key / key / label values given in the STRUCTURE exactly. Never include names, emails or phone numbers."
)


def pseudonymise(text: str, names: dict[str, str]) -> str:
    """Replaces each person's name (and its parts of 3+ letters) with their alias; masks e-mails, phone and long numbers."""
    out = text
    for name, alias in names.items():
        for part in sorted({name, *[p for p in re.split(r"\s+", name) if len(p) >= 3]}, key=len, reverse=True):
            out = re.sub(rf"\b{re.escape(part)}\b", alias, out, flags=re.IGNORECASE)
    out = _EMAIL.sub("[email]", out)
    out = _PHONE.sub("[number]", out)
    out = _LONG_NUMBER.sub("[number]", out)
    out = re.sub(r"[ \t]+", " ", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()[:MAX_AI_TEXT_CHARS]


def aliases(db: Session, profile: StaffProfile, resolver: pa.ActorResolver) -> dict[str, str]:
    names = {profile.full_name: "Staff A"}
    seen = {profile.id}
    cur = profile.manager_id
    for tag in ("Manager B", "Manager C", "Manager D", "Manager E"):
        if not cur or cur in seen:
            break
        seen.add(cur)
        other = db.get(StaffProfile, cur)
        if other is None:
            break
        names.setdefault(other.full_name, tag)
        cur = other.manager_id
    return names


def _structure(parts: pa.Parts) -> dict[str, Any]:
    return {
        "items": [{"kpi_key": i.kpi_key, "label": i.label, "weight": float(i.weight)} for i in parts.items],
        "competencies": [{"key": c.competency_key, "title": c.title} for c in parts.competencies],
        "objectives": [o.label for o in parts.objectives],
    }


def _conf(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


def _rating(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return v if isinstance(v, int) and 1 <= v <= 5 else None


def _text(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    t = re.sub(r"\s+", " ", v).strip()
    return t[:MAX_TEXT] or None


def validate(raw: str, parts: pa.Parts) -> tuple[dict[str, Any], list[str]]:
    """Strict check against THIS appraisal's rows. Raises AIOutputInvalid when nothing usable is in the answer."""
    data = ai._parse_json_object(raw)  # noqa: SLF001
    warnings: list[str] = []
    keys = {i.kpi_key for i in parts.items}
    comp_keys = {c.competency_key for c in parts.competencies}
    labels = {o.label for o in parts.objectives}
    out: dict[str, Any] = {"items": [], "competencies": [], "objectives": [], "development_needs": [], "comments": {}}

    seen: set[str] = set()
    for row in data.get("items") or []:
        if not isinstance(row, dict) or row.get("kpi_key") not in keys or row["kpi_key"] in seen:
            warnings.append("A row that isn't on this form was ignored.")
            continue
        seen.add(row["kpi_key"])
        sr, mr = _rating(row.get("self_rating")), _rating(row.get("manager_rating"))
        if (row.get("self_rating") is not None and sr is None) or (row.get("manager_rating") is not None and mr is None):
            warnings.append("A rating outside 1 to 5 was ignored.")
        out["items"].append({"kpi_key": row["kpi_key"], "self_text": _text(row.get("self_text")), "self_rating": sr, "manager_rating": mr, "confidence": _conf(row.get("confidence"))})
    seen = set()
    for row in data.get("competencies") or []:
        if not isinstance(row, dict) or row.get("key") not in comp_keys or row["key"] in seen:
            warnings.append("A competency that isn't on this form was ignored.")
            continue
        seen.add(row["key"])
        sr, mr = _rating(row.get("self_rating")), _rating(row.get("manager_rating"))
        out["competencies"].append({"key": row["key"], "self_rating": sr, "manager_rating": mr, "confidence": _conf(row.get("confidence"))})
    for row in data.get("objectives") or []:
        vals = row.get("values") if isinstance(row, dict) else None
        if not isinstance(row, dict) or row.get("label") not in labels or not isinstance(vals, list) or len(vals) != 3:
            warnings.append("An objective row that isn't on this form was ignored.")
            continue
        out["objectives"].append({"label": row["label"], "values": [(_text(v) or "")[:300] for v in vals], "confidence": _conf(row.get("confidence"))})
    for row in data.get("development_needs") or []:
        if isinstance(row, dict) and row.get("slot") in (1, 2) and _text(row.get("text")):
            out["development_needs"].append({"slot": row["slot"], "text": _text(row["text"]), "confidence": _conf(row.get("confidence"))})
    comments = data.get("comments")
    if isinstance(comments, dict):
        out["comments"] = {k: _text(comments.get(k)) for k in ("appraisee", "manager", "rm_gm", "hr", "ceo") if _text(comments.get(k))}
    if not (out["items"] or out["competencies"] or out["objectives"] or out["development_needs"]):
        raise ai.AIOutputInvalid("Nothing on that form could be matched to this appraisal.")
    return out, list(dict.fromkeys(warnings))


def flag_low_confidence(draft: dict[str, Any]) -> int:
    n = 0
    for section in ("items", "competencies", "objectives", "development_needs"):
        for row in draft[section]:
            row["low_confidence"] = row["confidence"] < LOW_CONFIDENCE
            n += row["low_confidence"]
    return n


OCR_FULL_CONFIDENCE = 85.0  # a scan read at or above this mean confidence is trusted as much as a typed document


def scale_for_ocr(draft: dict[str, Any], ocr_confidence: float) -> None:
    """A poor scan makes every reading less certain: scale each row's confidence by how well the page was read."""
    factor = max(0.0, min(1.0, ocr_confidence / OCR_FULL_CONFIDENCE))
    for section in ("items", "competencies", "objectives", "development_needs"):
        for row in draft[section]:
            row["confidence"] = round(row["confidence"] * factor, 3)


def propose(db: Session, ap: Appraisal, profile: StaffProfile, resolver: pa.ActorResolver, text: str, username: str, *, regenerate: bool = False, ocr_confidence: float | None = None) -> dict[str, Any]:
    parts = pa.load_parts(db, ap)
    safe = pseudonymise(text, aliases(db, profile, resolver))
    structure = _structure(parts)
    payload = {"text": safe, "structure": structure}
    input_hash = ai._hash(payload)  # noqa: SLF001
    cached = None if regenerate else ai._cached(db, "appraisal_extraction", input_hash)  # noqa: SLF001
    if cached:
        draft, provider, model, version, was_cached = cached.output["draft"], cached.provider, cached.model, cached.prompt_version, True
        warnings = cached.output.get("warnings", [])
    else:
        raw, provider, model = ai.complete_json(_SYSTEM, f"STRUCTURE:\n{ai.json.dumps(structure, ensure_ascii=False)}\n\nFORM TEXT:\n\n{safe}")
        draft, warnings = validate(raw, parts)
        ai._store(db, "appraisal_extraction", input_hash, provider, model, PROMPT_VERSION, {"draft": draft, "warnings": warnings}, username)  # noqa: SLF001
        version, was_cached = PROMPT_VERSION, False
    draft = copy.deepcopy(draft)
    warnings = list(warnings)
    if ocr_confidence is not None:
        scale_for_ocr(draft, ocr_confidence)
        warnings.append(f"This form was read from a picture (reading quality {ocr_confidence:.0f}%). Check every value against the original, especially handwriting.")
    flagged = flag_low_confidence(draft)
    return {**draft, "warnings": warnings, "ocr_confidence": round(ocr_confidence, 1) if ocr_confidence is not None else None, "low_confidence_count": flagged, "ai": ai._ai_meta(provider, model, version, was_cached)}  # noqa: SLF001


SELF_FIELDS = {"self_text", "self_rating"}
MANAGER_FIELDS = {"manager_rating"}


def apply_confirmed(db: Session, ap: Appraisal, reviewed: dict[str, Any], *, include_manager: bool) -> dict[str, int]:
    """
    Writes the REVIEWED values into the appraisal rows. `include_manager` is False for the appraisee themselves: they can confirm their
    own self-assessment but never the manager's column. Values are re-validated here, so the review screen can't smuggle in anything.
    """
    parts = pa.load_parts(db, ap)
    items, comps = {i.kpi_key: i for i in parts.items}, {c.competency_key: c for c in parts.competencies}
    objectives, needs = {o.label: o for o in parts.objectives}, {n.slot: n for n in parts.needs}
    counts = {"items": 0, "competencies": 0, "objectives": 0, "development_needs": 0}
    for row in reviewed.get("items") or []:
        item = items.get(row.get("kpi_key"))
        if item is None:
            continue
        if "self_text" in row:
            item.self_text = _text(row["self_text"])
        if "self_rating" in row:
            item.self_rating = _rating(row["self_rating"])
        if include_manager and "manager_rating" in row:
            item.manager_rating = _rating(row["manager_rating"])
        counts["items"] += 1
    for row in reviewed.get("competencies") or []:
        c = comps.get(row.get("key"))
        if c is None:
            continue
        if "self_rating" in row:
            c.self_rating = _rating(row["self_rating"])
        if include_manager and "manager_rating" in row:
            c.manager_rating = _rating(row["manager_rating"])
        counts["competencies"] += 1
    for row in reviewed.get("objectives") or []:
        o, vals = objectives.get(row.get("label")), row.get("values")
        if o is not None and isinstance(vals, list) and len(vals) == 3:
            o.values = [(_text(v) or "")[:300] for v in vals]
            counts["objectives"] += 1
    for row in reviewed.get("development_needs") or []:
        n = needs.get(row.get("slot"))
        if n is not None:
            n.text = _text(row.get("text"))
            counts["development_needs"] += 1
    return counts
