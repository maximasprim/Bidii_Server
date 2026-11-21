"""
Staff Performance - reading the legacy `2026_STAFF_PERFORMANCE.xlsx` layout (pure: no database).

What the workbook looks like (and what this reader does about it)
* One sheet per branch. Row 1 is a header: `Loan Officer | TARGET | JAN | % | FEB | ... | OCT`. Each MONTH header
  starts a block of columns that runs up to the next month header. In a block, the month cell is the AMOUNT (KES).
  Anything else in the block is one of: a formula percentage (IGNORED - always recomputed from amount and target),
  an unlabelled raw number from 0 to 100 (the officer's PAR %, recorded for some months), or text (a note such as
  `talk`, `PIP; 1 month`, `Lift PIP`, `promote`). A note belongs to the month block it trails.
* Layouts differ a little from sheet to sheet, so columns are located by header, never by fixed position.
* Cached percentages are never trusted (`#DIV/0!` where the target is blank or 0, formulas pointing at the wrong row,
  values like 14.95 sitting beside February). Rows whose formulas point at another row are reported.
* Subtotal/total rows (no name), team rows ("Debt Recovery Kitengela") and blank rows are recognised and reported.
* The `COMPANY` sheet's per-branch monthly totals and the `JAN2026`...`OCT` band-count sheets are read as
  RECONCILIATION references only.

Nothing here writes anything; it returns plain dicts so the result can be stored as JSON and shown in a preview.
"""

from __future__ import annotations

import io
import re
import zipfile
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_XLSX_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 120 * 1024 * 1024
MAX_ZIP_ENTRIES = 400
MAX_ISSUES_KEPT = 600

_MONTH_ABBR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_FULL = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "sept": 9, "october": 10, "november": 11, "december": 12,
}
_TEAM_ROW = re.compile(r"^\s*(debt|dept)\s*recovery\b", re.IGNORECASE)
_CELL_REF = re.compile(r"\$?([A-Z]{1,3})\$?(\d+)")
BAND_KEYS = ("gt100", "b80_99", "b60_79", "lt60")


class WorkbookError(ValueError):
    """The file can't be read as a workbook; the message is safe to show."""


def check_xlsx_container(data: bytes) -> None:
    """Cheap safety checks BEFORE openpyxl parses anything: real zip, sane size, no zip bomb, looks like a workbook."""
    if len(data) > MAX_XLSX_BYTES:
        raise WorkbookError("That file is too large (max 10 MB).")
    if not data.startswith(b"PK\x03\x04"):
        raise WorkbookError("That isn't an .xlsx workbook. Save the file as Excel Workbook (.xlsx) and try again.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
                raise WorkbookError("That workbook is unusually large inside; it can't be imported.")
            names = {i.filename for i in infos}
            if "xl/workbook.xml" not in names:
                raise WorkbookError("That isn't an .xlsx workbook.")
            if any(n.lower().endswith("vbaproject.bin") for n in names):
                raise WorkbookError("Workbooks with macros can't be imported. Save a copy as .xlsx (no macros).")
    except zipfile.BadZipFile:
        raise WorkbookError("That file is damaged or isn't an .xlsx workbook.") from None


def norm_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def header_month(value: Any) -> int | None:
    """JAN / 'JAN ' / MARCH / Mar / SEPT -> month number; 'JAN TARGET', '%', 'Q1', 'notes' -> None."""
    if not isinstance(value, str):
        return None
    t = norm_text(value)
    return _MONTH_ABBR.get(t) or _MONTH_FULL.get(t)


def name_key(name: str) -> str:
    """Order- and punctuation-insensitive key so "Ndungu, Scola" / "SCOLA NJOKI NDUNG'U" line up."""
    tokens = re.sub(r"[^a-z0-9\s]", "", name.lower()).split()
    return " ".join(sorted(tokens))


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


def _number(v: Any) -> Decimal | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        try:
            d = Decimal(str(v))
        except InvalidOperation:
            return None
        return d if d.is_finite() else None
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return Decimal(s)
    return None


def _is_formula(v: Any) -> bool:
    return isinstance(v, str) and v.startswith("=")


class _Issues:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {}
        self.dropped = 0

    def add(self, code: str, severity: str, message: str, sheet: str | None = None, row: int | None = None, month: str | None = None) -> None:
        self.counts[code] = self.counts.get(code, 0) + 1
        if len(self.items) >= MAX_ISSUES_KEPT:
            self.dropped += 1
            return
        self.items.append({"code": code, "severity": severity, "message": message, "sheet": sheet, "row": row, "month": month})


def _cell(ws, row: int, col: int) -> Any:
    return ws.cell(row, col).value


def _cached(ws_values, row: int, col: int) -> Any:
    return ws_values.cell(row, col).value if ws_values is not None else None


def find_header(ws) -> tuple[int, dict[int, int]] | None:
    """(header_row, {column: month}) for a staff sheet, or None if this isn't one."""
    for r in range(1, min(ws.max_row, 6) + 1):
        if norm_text(_cell(ws, r, 1)) == "loanofficer":
            months = {c: m for c in range(2, ws.max_column + 1) if (m := header_month(_cell(ws, r, c))) is not None}
            if len(months) >= 3:
                return r, months
    return None


def _parse_staff_sheet(ws, ws_values, header_row: int, month_cols: dict[int, int], year: int, issues: _Issues) -> dict[str, Any]:
    cols = sorted(month_cols)
    blocks = [(c, (cols[i + 1] if i + 1 < len(cols) else ws.max_column + 1), month_cols[c]) for i, c in enumerate(cols)]
    rows: list[dict[str, Any]] = []
    sheet = ws.title
    for r in range(header_row + 1, ws.max_row + 1):
        raw_name = _cell(ws, r, 1)
        has_other = any(_cell(ws, r, c) is not None for c in range(2, ws.max_column + 1))
        if raw_name is None or _is_formula(raw_name) or not str(raw_name).strip():
            if has_other:
                issues.add("subtotal_row", "info", "A total/subtotal row (no name) was skipped.", sheet, r)
            continue
        name = clean_name(str(raw_name))
        flags: list[str] = []

        t_raw = _cell(ws, r, 2)
        t = _number(_cached(ws_values, r, 2) if _is_formula(t_raw) else t_raw)
        if t_raw is not None and t is None:
            issues.add("bad_target", "warning", f'"{name}": the target cell isn\'t a number, so no target was read.', sheet, r)
            flags.append("bad_target")
        if t is not None and t < 0:
            issues.add("bad_target", "warning", f'"{name}": a negative target was ignored.', sheet, r)
            t = None
        target = float(t) if t is not None and t > 0 else None
        if t is not None and t == 0:
            flags.append("zero_target")

        months: dict[str, Any] = {}
        for start, end, month in blocks:
            key = f"{year:04d}-{month:02d}"
            entry: dict[str, Any] = {"amount": None, "par": None, "note": None}
            v_raw = _cell(ws, r, start)
            if _is_formula(v_raw):
                v_cached = _number(_cached(ws_values, r, start))
                if v_cached is None:
                    issues.add("formula_amount_unreadable", "warning", f'"{name}": the {key} amount is a formula with no usable value.', sheet, r, key)
                else:
                    entry["amount"] = float(v_cached)
                    issues.add("amount_from_formula", "info", f'"{name}": the {key} amount is a formula; its last calculated value was used.', sheet, r, key)
            else:
                v = _number(v_raw)
                if v is not None:
                    if v < 0:
                        issues.add("negative_amount", "warning", f'"{name}": a negative {key} amount was ignored.', sheet, r, key)
                    else:
                        entry["amount"] = float(v)
                elif isinstance(v_raw, str) and v_raw.strip():
                    entry["note"] = v_raw.strip()
                    issues.add("text_in_amount_cell", "warning", f'"{name}": text sits where the {key} amount should be; it was kept as a note.', sheet, r, key)

            notes: list[str] = [entry["note"]] if entry["note"] else []
            par_candidates: list[float] = []
            for c in range(start + 1, end):
                val = _cell(ws, r, c)
                if val is None:
                    continue
                if _is_formula(val):
                    refs = {int(m.group(2)) for m in _CELL_REF.finditer(val)}
                    if refs and refs != {r}:
                        issues.add("formula_row_mismatch", "warning", f'"{name}": a cached % formula points at another row; it is ignored (percentages are recomputed).', sheet, r, key)
                    continue
                n = _number(val)
                if n is not None:
                    if 0 <= n <= 100:
                        par_candidates.append(float(n))
                    else:
                        issues.add("unlabelled_number", "warning", f'"{name}": an unlabelled number ({n}) in the {key} block was ignored.', sheet, r, key)
                elif isinstance(val, str) and val.strip():
                    notes.append(val.strip())
            if len(par_candidates) == 1:
                entry["par"] = par_candidates[0]
            elif len(par_candidates) > 1:
                issues.add("ambiguous_par", "warning", f'"{name}": more than one unlabelled number in the {key} block, so no PAR was read.', sheet, r, key)
            if notes:
                entry["note"] = " / ".join(notes)[:300]
            if entry["amount"] is not None or entry["par"] is not None or entry["note"]:
                months[key] = entry
        rows.append(
            {
                "row": r,
                "name": name,
                "key": name_key(name),
                "target": target,
                "is_team_row": bool(_TEAM_ROW.match(name)),
                "flags": flags,
                "months": months,
            }
        )
    _flag_copied_months(rows, sheet, issues)
    return {"rows": rows}


def _flag_copied_months(rows: list[dict[str, Any]], sheet: str, issues: _Issues) -> None:
    """A month whose amounts repeat the previous month's for several people is usually a copied column."""
    months = sorted({m for r in rows for m in r["months"]})
    for prev, cur in zip(months, months[1:]):
        both = [(r["months"][prev].get("amount"), r["months"][cur].get("amount")) for r in rows if prev in r["months"] and cur in r["months"]]
        both = [(a, b) for a, b in both if a is not None and b is not None and a > 0 and b > 0]
        same = sum(1 for a, b in both if a == b)
        if same >= 3 and same >= 0.5 * len(both):
            issues.add(
                "identical_adjacent_months", "warning",
                f'On "{sheet}", {same} of {len(both)} people have exactly the same amount in {prev[-2:]} and {cur[-2:]}. One of those columns may have been copied; check them against the COMPANY totals before importing.',
                sheet, month=cur,
            )


def _company_reference(ws, ws_values, year: int, issues: _Issues) -> dict[str, dict[str, float]]:
    """{branch name: {YYYY-MM: KES}} from the COMPANY sheet's monthly branch totals."""
    header_row = None
    for r in range(1, min(ws.max_row, 6) + 1):
        if norm_text(_cell(ws, r, 2)) == "branch":
            header_row = r
            break
    if header_row is None:
        issues.add("company_unreadable", "warning", "The COMPANY sheet has no 'Branch' header, so branch totals can't be checked.", ws.title)
        return {}
    month_cols: dict[int, int] = {}
    for c in range(3, ws.max_column + 1):
        m = header_month(_cell(ws, header_row, c))
        if m is not None:
            month_cols[c] = m
    out: dict[str, dict[str, float]] = {}
    for r in range(header_row + 1, ws.max_row + 1):
        branch = _cell(ws, r, 2)
        if not isinstance(branch, str) or not branch.strip():
            continue
        vals: dict[str, float] = {}
        for c, m in month_cols.items():
            raw = _cell(ws, r, c)
            n = _number(_cached(ws_values, r, c) if _is_formula(raw) else raw)
            if n is not None:
                vals[f"{year:04d}-{m:02d}"] = float(n)
        out[clean_name(branch)] = vals
    return out


def _band_sheet(ws, issues: _Issues, year: int) -> dict[str, Any] | None:
    """{month: 'YYYY-MM', bands: {branch: {gt100,b80_99,b60_79,lt60}}} from a JAN2026-style count sheet."""
    month = header_month(re.sub(r"[^A-Za-z]", "", ws.title))
    if month is None:
        return None
    rows: dict[str, dict[str, int]] = {}
    in_table = False
    for r in range(1, ws.max_row + 1):
        label = _cell(ws, r, 2)
        if isinstance(label, str) and label.strip().upper() == "BRANCH":
            in_table = True
            continue
        if not in_table or not isinstance(label, str):
            continue
        if label.strip().upper() == "TOTAL":
            break
        counts = {}
        for key, col in zip(BAND_KEYS, (3, 4, 5, 6)):
            n = _number(_cell(ws, r, col))
            counts[key] = int(n) if n is not None else 0
        rows[clean_name(label)] = counts
    title = _cell(ws, 2, 1)
    if isinstance(title, str):
        m = re.match(r"\s*([A-Za-z]+)", title)
        title_month = header_month(m.group(1)) if m else None
        if title_month is not None and title_month != month:
            issues.add("band_sheet_title_mismatch", "info", f'Sheet "{ws.title}" is titled "{title.strip()}"; the sheet name was used for the month.', ws.title)
    return {"month": f"{year:04d}-{month:02d}", "bands": rows}


def detect_year(wb, wv) -> int:
    """The workbook's year: a 'Year' cell on the template's Read me sheet, else the COMPANY sheet's title, else a sheet name."""
    if "Read me" in wv.sheetnames:
        sheet = wv["Read me"]
        for r in range(1, 20):
            if norm_text(sheet.cell(r, 1).value) == "year":
                n = _number(sheet.cell(r, 2).value)
                if n is not None and 2000 <= n <= 2100:
                    return int(n)
    ws = wv["COMPANY"] if "COMPANY" in wv.sheetnames else None
    if ws is not None:
        for r in range(1, 4):
            for c in range(1, 4):
                n = _number(ws.cell(r, c).value)
                if n is not None and 2000 <= n <= 2100:
                    return int(n)
    for name in wb.sheetnames:
        m = re.search(r"(20\d\d)", name)
        if m and "company" not in name.lower():
            return int(m.group(1))
    return 0


def parse_workbook(data: bytes, year: int | None = None) -> dict[str, Any]:
    """Reads the whole workbook. Raises WorkbookError for anything that isn't a readable .xlsx."""
    check_xlsx_container(data)
    import openpyxl

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=False, read_only=False, keep_links=False)
        wv = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=False, keep_links=False)
    except Exception:
        raise WorkbookError("That workbook couldn't be read. Check that it opens in Excel, then try again.") from None

    issues = _Issues()
    yr = year or detect_year(wb, wv)
    if not yr:
        raise WorkbookError("I couldn't tell which year this workbook covers. Tell me the year and re-upload.")

    sheets: list[dict[str, Any]] = []
    company: dict[str, dict[str, float]] = {}
    bands: dict[str, dict[str, dict[str, int]]] = {}
    for ws in wb.worksheets:
        wsv = wv[ws.title]
        hidden = ws.sheet_state != "visible"
        header = find_header(ws)
        if header is not None:
            parsed = _parse_staff_sheet(ws, wsv, header[0], header[1], yr, issues)
            months_present = sorted({m for row in parsed["rows"] for m in row["months"]})
            sheets.append({"name": ws.title, "kind": "staff", "hidden": hidden, "rows": parsed["rows"], "months": months_present})
            if hidden:
                issues.add("hidden_sheet", "info", f'Sheet "{ws.title}" is hidden in the workbook; it is shown here so you can choose whether to import it.', ws.title)
            continue
        low = ws.title.strip().lower()
        if low == "company":
            company = _company_reference(ws, wsv, yr, issues)
            sheets.append({"name": ws.title, "kind": "company", "hidden": hidden})
            continue
        band = _band_sheet(ws, issues, yr)
        if band is not None and band["bands"]:
            bands[band["month"]] = band["bands"]
            sheets.append({"name": ws.title, "kind": "bands", "hidden": hidden})
            continue
        kind = "history" if re.search(r"20\d\d.*company|company.*20\d\d", low) else "other"
        sheets.append({"name": ws.title, "kind": kind, "hidden": hidden})
        issues.add("sheet_not_imported", "info", f'Sheet "{ws.title}" isn\'t a staff sheet, so nothing was read from it.', ws.title)

    if not any(s["kind"] == "staff" for s in sheets):
        raise WorkbookError("I found no staff sheets (a header row starting with 'Loan Officer'). Is this the right workbook?")
    return {
        "year": yr,
        "sheets": sheets,
        "company": company,
        "bands": bands,
        "issues": issues.items,
        "issue_counts": issues.counts,
        "issues_dropped": issues.dropped,
    }
