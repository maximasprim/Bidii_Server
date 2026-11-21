"""Exports for the reports: .xlsx and .pdf. Cell text is neutralised against spreadsheet formula injection."""

from __future__ import annotations

import io
from typing import Any, Iterable, Sequence

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

_DANGEROUS = ("=", "+", "-", "@", "\t", "\r", "\n")


def safe_cell(value: Any) -> Any:
    """Text a spreadsheet could treat as a formula is prefixed with an apostrophe; numbers pass through untouched."""
    if isinstance(value, str) and value.lstrip(" ").startswith(_DANGEROUS) or (isinstance(value, str) and value.startswith(_DANGEROUS)):
        return "'" + value
    return value


def _clean(row: Iterable[Any]) -> list[Any]:
    return [safe_cell(v) for v in row]


def build_xlsx(title: str, subtitle: str, headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    ws.append(_clean([title]))
    ws.append(_clean([subtitle]))
    ws.append([])
    ws.append(_clean(headers))
    for c in ws[4]:
        c.font = Font(bold=True)
    ws["A1"].font = Font(bold=True, size=13)
    for r in rows:
        ws.append(_clean(r))
    for i in range(1, len(headers) + 1):
        width = max([len(str(headers[i - 1]))] + [len(str(r[i - 1])) for r in rows if i - 1 < len(r) and r[i - 1] is not None])
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 10), 48)
    ws.freeze_panes = "A5"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_pdf(title: str, subtitle: str, headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24, title=title)
    styles = getSampleStyleSheet()
    data = [[escape(str(h)) for h in headers]] + [["" if v is None else escape(str(v)) for v in r] for r in rows]
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
    ]))
    doc.build([Paragraph(escape(title), styles["Title"]), Paragraph(escape(subtitle), styles["Normal"]), Spacer(1, 10), table])
    return buf.getvalue()
