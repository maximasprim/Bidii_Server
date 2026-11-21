"""A blank appraisal form for a job role as an editable Word file, in the paper form's columns."""

from __future__ import annotations

import io
from typing import Any

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.shared import Mm, Pt


def _table(doc, headers: list[str], rows: list[list[str]]):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Table Grid"
    for i, h in enumerate(headers):
        cell = t.rows[0].cells[i]
        cell.text = ""
        cell.paragraphs[0].add_run(h).bold = True
    for r in rows:
        cells = t.add_row().cells
        for i, v in enumerate(r):
            cells[i].text = v
    return t


def render(d: dict[str, Any], rating_guide: str) -> bytes:
    """`d` comes from performance_appraisal_pdf.blank_detail. Labels follow the paper form exactly so an upload extracts cleanly."""
    doc = Document()
    sec = doc.sections[0]
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = Mm(297), Mm(210)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(sec, side, Mm(12))
    doc.styles["Normal"].font.size = Pt(9)
    doc.add_heading("BIDII CREDIT - Staff Performance Appraisal", 1)
    doc.add_paragraph(rating_guide)
    who = d["profile"]
    _table(doc, ["Name", "Position", "Branch / team", "Immediate manager", "Period"], [["", who.get("role_name") or "", "", "", d["period_label"]]])

    doc.add_heading("Performance objectives and ratings", 2)
    rows = [[i["label"], f"{i['weight']:g}", i.get("criteria") or "", "", "", "", ""] for i in d["items"]]
    rows.append(["Total", f"{sum(i['weight'] for i in d['items']):g}", "", "", "", "", ""])
    _table(doc, ["A. Job objectives", "B. % weight", "C. Measurement criteria", "D. Self assessment", "E. Rating (1-5)", "F. Weighted rating (B x E)", "G. Manager assessment (1-5)"], rows)

    if d["competencies"]:
        doc.add_heading("Behavioural competencies", 2)
        _table(doc, ["Competency", "Self rating (1-5)", "Manager rating (1-5)"], [[c["title"], "", ""] for c in d["competencies"]])
    if d["objectives"]:
        doc.add_heading("Performance objectives for the next 3 months", 2)
        _table(doc, ["Objective", *d["objective_months"]], [[o["label"], "", "", ""] for o in d["objectives"]])
    doc.add_heading("Training and development needs", 2)
    _table(doc, ["Need"], [["1."], ["2."]])
    doc.add_heading("Comments and sign-off", 2)
    _table(doc, ["Step", "Name", "Date", "Comment"], [[c["label"], "", "", ""] for c in d["chain"]])
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
