"""A blank appraisal form for a job role as an editable Word file, laid out like the company's paper forms (staff and admin)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt

from app.services.performance_appraisal_pdf import FORM_NOTE, GUIDE_ROWS, SIGN_NOTE, TRAINING_NOTE, comment_heading

_LOGO = Path(__file__).resolve().parent.parent / "assets" / "Bidii-logo.png"
# the paper form's column heads, A to G
_HEADS = [
    "Job Objectives for the Period", "% Weight", "Measurement Criteria", "Self-Assessment of performance against KPIs (give examples where appropriate)",
    "Rate each KPI using the 5-level rating below", "Weighted Rating column (B X E)", "Manager Assessment of performance against KPIs (1-5)",
]
_WIDTHS = [38, 15, 36, 40, 17, 18, 22]  # mm, portrait A4 less margins


def _set(cell, text: str, bold: bool = False, size: float = 8.5, center: bool = False) -> None:
    cell.text = ""
    p = cell.paragraphs[0]
    run = p.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    if center:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER


def _shade(cell, fill: str = "E9EDF5") -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def _grid(doc, rows: int, cols: int, widths: list[float] | None = None):
    t = doc.add_table(rows=rows, cols=cols)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    if widths:
        t.autofit = False
        for row in t.rows:
            for i, w in enumerate(widths):
                row.cells[i].width = Mm(w)
        for col, w in zip(t._tbl.tblGrid.findall(qn("w:gridCol")), widths):  # Word reads the cell widths, LibreOffice the grid
            col.set(qn("w:w"), str(int(w * 56.7)))
    return t


def _part(doc, text: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    run = p.add_run(text)
    run.bold = True
    run.underline = True
    run.font.size = Pt(10)


def _note(doc, text: str, bold: bool = False, size: float = 8) -> None:
    run = doc.add_paragraph().add_run(text)
    run.bold = bold
    run.font.size = Pt(size)


def render(d: dict[str, Any], rating_guide: str) -> bytes:
    """`d` comes from performance_appraisal_pdf.blank_detail. Headings and columns follow the paper form so a filled copy can be uploaded and read."""
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(sec, side, Mm(12))
    doc.styles["Normal"].font.size = Pt(9)

    # logo and title
    head = doc.add_table(rows=1, cols=2)
    head.autofit = False
    head.rows[0].cells[0].width, head.rows[0].cells[1].width = Mm(26), Mm(160)
    if _LOGO.exists():
        head.rows[0].cells[0].paragraphs[0].add_run().add_picture(str(_LOGO), width=Mm(17))
    title = head.rows[0].cells[1]
    title.text = ""
    title.paragraphs[0].add_run("BIDII CREDIT LIMITED").bold = True
    title.add_paragraph().add_run("Staff Performance Appraisal Form").bold = True
    if d["profile"].get("role_name"):
        title.add_paragraph().add_run(f"Job Role: {d['profile']['role_name']}").bold = True

    who = d["profile"]
    box = _grid(doc, 2, 6, [22, 40, 24, 28, 38, 34])
    labels = [["Name:", "", "Staff No.:", "", "Position:", who.get("role_name") or ""], ["My Manager:", "", "Branch/Dept:", "", "Years/Months Worked:", ""]]
    for r, row in enumerate(labels):
        for c, text in enumerate(row):
            _set(box.rows[r].cells[c], text, bold=c % 2 == 0)
    _part(doc, "Part A: Performance Assessment Section")
    _note(doc, f"Assessment Period: {d['period_label']}", bold=True, size=9)

    items = d["items"]
    main = _grid(doc, 3 + len(items), 7, _WIDTHS)
    for i, letter in enumerate("ABCDEFG"):
        _set(main.rows[0].cells[i], letter, bold=True, center=True)
        _set(main.rows[1].cells[i], _HEADS[i], bold=True)
    for n, item in enumerate(items):
        cells = main.rows[2 + n].cells
        _set(cells[0], item["label"])
        _set(cells[1], f"{item['weight']:g}%")
        _set(cells[2], item.get("criteria") or "")
        for c in range(3, 7):
            _set(cells[c], "")
        cells[3].paragraphs[0].paragraph_format.space_after = Pt(36)  # room to write
    last = main.rows[-1].cells
    _set(last[0], "OVERALL RATING", bold=True)
    _set(last[1], f"{sum(i['weight'] for i in items):g}%")
    for c in range(2, 7):
        _set(last[c], "")
    for c in last:
        _shade(c)

    _note(doc, rating_guide, size=9)
    _note(doc, "Assessment Guide (Column E)", size=9)
    guide = _grid(doc, 1 + len(GUIDE_ROWS), 3, [34, 34, 24])
    guide.alignment = WD_TABLE_ALIGNMENT.LEFT
    for c, text in enumerate(("Achievement", "Description", "Rating")):
        _set(guide.rows[0].cells[c], text, bold=True)
    for r, row in enumerate(GUIDE_ROWS, start=1):
        for c, text in enumerate(row):
            _set(guide.rows[r].cells[c], text)
    _note(doc, FORM_NOTE)

    if d["competencies"]:
        _part(doc, "Behavioural competencies")
        comp = _grid(doc, 1 + len(d["competencies"]), 3, [116, 35, 35])
        for c, text in enumerate(("Competency", "Self rating (1-5)", "Manager rating (1-5)")):
            _set(comp.rows[0].cells[c], text, bold=True)
        for r, c_ in enumerate(d["competencies"], start=1):
            _set(comp.rows[r].cells[0], c_["title"])
            _set(comp.rows[r].cells[1], "")
            _set(comp.rows[r].cells[2], "")
    if d["objectives"]:
        _part(doc, "Part B: Performance Objectives for the next 3 months")
        obj = _grid(doc, 1 + len(d["objectives"]), 4, [72, 38, 38, 38])
        for c, text in enumerate(("", *d["objective_months"])):
            _set(obj.rows[0].cells[c], text, bold=True)
        for r, o in enumerate(d["objectives"], start=1):
            _set(obj.rows[r].cells[0], o["label"])
            for c in (1, 2, 3):
                _set(obj.rows[r].cells[c], "")
    _part(doc, "Part C: Training Requirements")
    train = _grid(doc, 3, 2, [12, 174])
    train.rows[0].cells[0].merge(train.rows[0].cells[1])
    _set(train.rows[0].cells[0], TRAINING_NOTE)
    for r in (1, 2):
        _set(train.rows[r].cells[0], str(r))
        _set(train.rows[r].cells[1], "")

    _note(doc, SIGN_NOTE, bold=True, size=9)
    sign = _grid(doc, max(1, len(d["chain"])), 1, [186])
    for r, step in enumerate(d["chain"]):
        row_pr = sign.rows[r]._tr.get_or_add_trPr()
        row_pr.append(OxmlElement("w:cantSplit"))  # a reviewer's box never breaks across pages
        cell = sign.rows[r].cells[0]
        cell.text = ""
        cell.paragraphs[0].add_run(comment_heading(step["label"])).bold = True
        cell.add_paragraph("\n\n")
        cell.add_paragraph().add_run("Signed:\t\t\t\t\t\t\t\tDate:").bold = True
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
