"""
Deterministic reader for Bidii Credit's house-format Job Description (.docx).

Why this exists: the company's JDs change constantly (new roles, different duties, a different
number of responsibilities or competencies) but the LAYOUT is stable - an info box, a
"Key Responsibilities | % of Time | Performance Measurement Criteria" table, an "Other
Responsibilities" table, a qualifications box and a competencies box. So this reader is driven by
the layout, never by the wording:

  * tables are FOUND by their header / label text, not by their position;
  * columns are found from the header row, so a re-ordered or extra column is fine;
  * rows are read as they come: 3 responsibilities or 9, 1 duty or 12, 4 competencies or 6;
  * labels are matched loosely ("Overall, Role Purpose", "Job Title", "Reports To:" ...).

It makes no network call and needs no AI key, so the result is exact and repeatable. If the file is
not in this layout (a plain-text JD, a scan, an old free-form draft) it returns None and the caller
falls back to whatever it did before - nothing is guessed.

It returns RAW values (strings and lists); validation / clipping is the caller's job
(app/services/performance_roles.py::validate_jd for role JDs), so there is one rule-book, not two.
"""

from __future__ import annotations

import io
import re
from typing import Any

READER_NAME = "docx-layout-reader"
READER_VERSION = "docx-layout-v1"

_INFO_LABELS = {
    "position title": "title",
    "job title": "title",
    "position": "title",
    "reports to": "reports_to",
    "reports": "reports_to",
    "branch": "branch",
    "department": "department",
    "grade": "grade",
    "date": "date",
}
_OTHER_LABELS = (
    ("decision making", "mandates"),
    ("planning responsibility", "planning"),
    ("planning", "planning"),
    ("relationship management", "relationships"),
)
_BULLET = re.compile(r"^\s*(?:\(?\d{1,2}[\).:]\s+|\(?\d{1,2}[\)]|[-–•▪→⇒*]+\s*)")
_PCT = re.compile(r"^\s*(\d{1,3}(?:[.,]\d+)?)\s*%?\s*$")
_MIN_HEADING = re.compile(r"^(minimum\s+)?qualifications?(\s+and\s+(skills|experience))?$")
_EXP_HEADING = re.compile(r"^(experience\s*(and|&)\s*skills|skills\s*(and|&)\s*experience|skills|experience)$")
_NAME_SPLIT = re.compile(r"^(.{3,70}?)\s+[–—-]\s+(.+)$")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%]+", " ", (text or "").lower())).strip()


def _lines(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", part.replace(" ", " ")).strip() for part in re.split(r"[\r\n\v]+", text or "")]


def _strip_bullet(line: str) -> str:
    previous = None
    while previous != line:
        previous, line = line, _BULLET.sub("", line, count=1).strip()
    return line


def _cell_lines(cell) -> list[str]:
    out: list[str] = []
    for paragraph in cell.paragraphs:
        for line in _lines(paragraph.text):
            line = _strip_bullet(line)
            if line:
                out.append(line)
    return out


def _cell_text(cell) -> str:
    return " ".join(_lines(cell.text)).strip()


def _distinct(row) -> list[Any]:
    """The row's cells with merged cells counted once (python-docx repeats them)."""
    seen: list[Any] = []
    cells = []
    for cell in row.cells:
        if any(cell._tc is s for s in seen):
            continue
        seen.append(cell._tc)
        cells.append(cell)
    return cells


def _table_text(table) -> str:
    return _norm(" ".join(_cell_text(c) for row in table.rows for c in _distinct(row)))


def _first_row_text(table) -> str:
    return _norm(" ".join(_cell_text(c) for c in _distinct(table.rows[0]))) if table.rows else ""


def _is_label(text: str) -> bool:
    n = _norm(text).rstrip(" :")
    return n in _INFO_LABELS or n.startswith("overall") and "purpose" in n


def _read_info(table) -> dict[str, str]:
    info: dict[str, str] = {}
    for row in table.rows:
        cells = _distinct(row)
        for i, cell in enumerate(cells):
            raw = _cell_text(cell)
            label = _norm(raw)
            if label.startswith("overall") and "purpose" in label:
                after = re.split(r":", raw, maxsplit=1)[1].strip() if ":" in raw else ""
                if not after and i + 1 < len(cells):
                    after = _cell_text(cells[i + 1])
                info.setdefault("purpose", after)
                continue
            key = _INFO_LABELS.get(label)
            if key is None:
                continue
            value = ""
            if i + 1 < len(cells) and not _is_label(_cell_text(cells[i + 1])):
                value = _cell_text(cells[i + 1])
            info.setdefault(key, value)
    return info


def _parse_weight(text: str) -> float | None:
    m = _PCT.match(text or "")
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    return value if 0 <= value <= 100 else None


def _is_heading(paragraph) -> bool:
    return any((r.underline or r.bold) and r.text.strip() for r in paragraph.runs)


def _read_responsibilities(table) -> list[dict[str, Any]]:
    # Columns are identified by their ORDER among the visible (un-merged) cells, never by Word's underlying
    # grid index: a header row and its body rows often use different grid spans in real documents.
    header = _distinct(table.rows[0])
    roles: dict[str, int] = {}
    for ordinal, cell in enumerate(header):
        n = _norm(_cell_text(cell))
        if "key responsibilit" in n and "resp" not in roles:
            roles["resp"] = ordinal
        elif ("time" in n or "%" in n) and "pct" not in roles:
            roles["pct"] = ordinal
        elif ("criteria" in n or "measurement" in n) and "crit" not in roles:
            roles["crit"] = ordinal
    if "resp" not in roles:
        return []

    out: list[dict[str, Any]] = []
    for row in list(table.rows)[1:]:
        cells = _distinct(row)
        if not cells:
            continue
        if len(cells) == len(header):
            left = cells[roles["resp"]]
            pct_cell = cells[roles["pct"]] if "pct" in roles else None
            crit_cells = [cells[roles["crit"]]] if "crit" in roles else []
        else:
            # A row with a different number of visible cells: find the '% of time' cell by its content.
            pct_at = next((i for i, c in enumerate(cells) if i > 0 and _parse_weight(_cell_text(c)) is not None), None)
            left = cells[0]
            pct_cell = cells[pct_at] if pct_at is not None else None
            crit_cells = cells[pct_at + 1:] if pct_at is not None else cells[1:]

        left_lines = _cell_lines(left)
        crit_lines = [x for c in crit_cells for x in _cell_lines(c)]
        weight = _parse_weight(_cell_text(pct_cell)) if pct_cell is not None else None

        if not left_lines and not crit_lines and weight is None:
            continue  # spacer row
        first_par = next((p for p in left.paragraphs if _strip_bullet(" ".join(_lines(p.text)))), None)
        starts_new = weight is not None or not out or (first_par is not None and _is_heading(first_par))
        if starts_new and left_lines:
            out.append({"title": left_lines[0], "weight_pct": weight, "duties": left_lines[1:], "criteria": crit_lines})
        elif out:  # a continuation row of the previous responsibility
            out[-1]["duties"].extend(left_lines)
            out[-1]["criteria"].extend(crit_lines)
            if weight is not None and out[-1]["weight_pct"] is None:
                out[-1]["weight_pct"] = weight
    return out


def _read_other(tables) -> dict[str, str]:
    out: dict[str, str] = {}
    for table in tables:
        for row in table.rows:
            cells = _distinct(row)
            if len(cells) < 2:
                continue
            label = _norm(_cell_text(cells[0]))
            for prefix, key in _OTHER_LABELS:
                if label.startswith(prefix):
                    out.setdefault(key, "\n".join(x for c in cells[1:] for x in _cell_lines(c)))
                    break
    return out


def _read_qualifications(table) -> list[str]:
    lines: list[str] = []
    for row in table.rows:
        for cell in _distinct(row):
            lines.extend(_cell_lines(cell))
    quals: list[str] = []
    for line in lines:
        n = _norm(line)
        if n.startswith("essential role related"):
            continue
        if _MIN_HEADING.match(n) or _EXP_HEADING.match(n):
            continue  # a section heading, not an item
        quals.append(line.rstrip(" ;"))
    return quals


def _read_competencies(table) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in list(table.rows)[1:]:
        for cell in _distinct(row):
            for line in _cell_lines(cell):
                m = _NAME_SPLIT.match(line)
                title, desc = (m.group(1), m.group(2)) if m else (line, "")
                title = title.strip(" :-–")
                desc = desc.strip()
                if desc:
                    desc = desc[0].upper() + desc[1:]
                if title:
                    out.append({"title": title, "description": desc})
    return out


def read_house_jd(data: bytes) -> dict[str, Any] | None:
    """
    Reads a house-layout JD. Returns None unless the file really is in that layout (an info box with a
    position title AND a responsibilities table with at least one numeric '% of Time'); it never
    raises on a malformed file - the caller then takes its normal path.
    """
    try:
        import docx  # python-docx

        document = docx.Document(io.BytesIO(data))
        tables = list(document.tables)
    except Exception:  # noqa: BLE001 - any parse failure just means "not this layout"
        return None

    try:
        info_table = next((t for t in tables if re.search(r"\b(position|job) title\b", _table_text(t)) and "reports" in _table_text(t)), None)
        resp_table = next((t for t in tables if t.rows and "key responsibilit" in _first_row_text(t)
                           and ("time" in _first_row_text(t) or "%" in _first_row_text(t))), None)
        if info_table is None or resp_table is None:
            return None

        info = _read_info(info_table)
        responsibilities = _read_responsibilities(resp_table)
        if not info.get("title") or not any(r["weight_pct"] is not None for r in responsibilities):
            return None

        other = _read_other(tables)
        qual_table = next((t for t in tables if t is not resp_table and t is not info_table and
                           re.search(r"minimum qualification|essential role related|experience and skills", _table_text(t)) and
                           "competenc" not in _first_row_text(t)), None)
        comp_table = next((t for t in tables if t.rows and "competenc" in _first_row_text(t)), None)

        missing = []
        if qual_table is None:
            missing.append("qualifications")
        if comp_table is None:
            missing.append("competencies")

        return {
            "title": info.get("title", ""), "reports_to": info.get("reports_to", ""), "branch": info.get("branch", ""),
            "department": info.get("department", ""), "grade": info.get("grade", ""), "date": info.get("date", ""),
            "purpose": info.get("purpose", ""), "mandates": other.get("mandates", ""), "planning": other.get("planning", ""),
            "relationships": other.get("relationships", ""),
            "responsibilities": [
                {"title": r["title"], "weight_pct": r["weight_pct"], "description": "\n".join(r["duties"]), "criteria": r["criteria"]}
                for r in responsibilities
            ],
            "qualifications": _read_qualifications(qual_table) if qual_table is not None else [],
            "competencies": _read_competencies(comp_table) if comp_table is not None else [],
            "_sections_missing": missing,
        }
    except Exception:  # noqa: BLE001 - a layout we can't make sense of is "not this layout", never a 500
        return None
