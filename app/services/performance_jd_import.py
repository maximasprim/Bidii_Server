"""
Turns a house-layout JD read by app/services/jd_docx_reader.py into the same response the AI
extraction returns ({"jd", "warnings", "ai"}), so the JD builder screen needs no change.

Nothing is saved here and no AI is involved. The draft goes through the SAME validator as every
other JD (roles.validate_jd), whose complaints (for example "weights add up to 90") become
warnings exactly as they do for an AI draft - plus a few checks a reader can make that an AI cannot
be trusted to (a responsibility with no measurement criteria, a missing section).
"""

from __future__ import annotations

from typing import Any

from app.services import jd_docx_reader as reader
from app.services import performance_roles as roles

LOCAL_LABEL = "Read directly from the document layout (no AI used); review before saving"


def local_meta() -> dict[str, Any]:
    return {"provider": "local", "model": reader.READER_NAME, "prompt_version": reader.READER_VERSION, "cached": False, "label": LOCAL_LABEL}


def build_local_result(raw: dict[str, Any]) -> dict[str, Any]:
    missing = list(raw.get("_sections_missing") or [])
    data = {k: v for k, v in raw.items() if not k.startswith("_")}

    warnings: list[str] = []
    jd = roles.validate_jd(data, warnings)

    for i, resp in enumerate(jd.get("responsibilities", []), 1):
        if not resp.get("criteria"):
            warnings.append(f'Responsibility {i} ("{resp["title"]}") has no performance measurement criteria.')
    if not jd.get("purpose"):
        warnings.append("No overall role purpose was found.")
    if "qualifications" in missing or not jd.get("qualifications"):
        warnings.append("No qualifications were found in the document.")
    if "competencies" in missing or not jd.get("competencies"):
        warnings.append("No behavioural competencies were found in the document.")
    return {"jd": jd, "warnings": warnings, "ai": local_meta()}


def read_docx(data: bytes) -> dict[str, Any] | None:
    """The finished extraction result, or None when the file is not in the house layout."""
    raw = reader.read_house_jd(data)
    return None if raw is None else build_local_result(raw)
