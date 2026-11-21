"""
Printable appraisal PDF that mirrors the paper form (columns A-G), plus a blank version per job role.
All text is escaped before it reaches reportlab's mini-markup, and nothing user-supplied is used as a file name.
"""

from __future__ import annotations

import io
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

RATING_GUIDE = "Rating guide:  5 Outstanding (>120%)   4 Excellent (100-120%)   3 Good (80-100%)   2 Average (60-80%)   1 Poor (<60%)"
_NAVY, _LIGHT = colors.HexColor("#1B2A4E"), colors.HexColor("#E9EDF5")


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Heading1"], fontSize=15, textColor=_NAVY, spaceAfter=2),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontSize=10.5, textColor=_NAVY, spaceBefore=8, spaceAfter=3),
        "cell": ParagraphStyle("c", parent=base["BodyText"], fontSize=7.6, leading=9.4),
        "head": ParagraphStyle("h", parent=base["BodyText"], fontSize=7.6, leading=9.4, textColor=colors.white, fontName="Helvetica-Bold"),
        "label": ParagraphStyle("l", parent=base["BodyText"], fontSize=7.6, leading=9.4, textColor=_NAVY, fontName="Helvetica-Bold"),
        "small": ParagraphStyle("s", parent=base["BodyText"], fontSize=7.2, leading=9, textColor=colors.HexColor("#555555")),
    }


def _p(text: Any, style) -> Paragraph:
    s = "" if text is None else str(text)
    return Paragraph(escape(s).replace("\n", "<br/>"), style)


def _grid(rows, widths, st, header=True):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), _NAVY))
    t.setStyle(TableStyle(style))
    return t


def _num(v, fmt="{:g}") -> str:
    return "" if v is None else fmt.format(v)


def render(d: dict[str, Any], blank: bool = False) -> bytes:
    """`d` is the dict from performance_appraisal_views.detail (or blank_detail for a role template)."""
    st = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm, title="Staff Performance Appraisal")
    who = d["profile"]
    story: list[Any] = [Paragraph("BIDII CREDIT - Staff Performance Appraisal", st["title"]), Paragraph(escape(RATING_GUIDE), st["small"]), Spacer(1, 4)]

    head = [
        [_p("Name", st["label"]), _p("" if blank else who["full_name"], st["cell"]), _p("Position", st["label"]), _p(who.get("role_name"), st["cell"]), _p("Period", st["label"]), _p(d["period_label"], st["cell"])],
        [_p("Branch / team", st["label"]), _p("" if blank else who.get("unit_name"), st["cell"]), _p("Immediate manager", st["label"]), _p("" if blank else who.get("manager_name"), st["cell"]), _p("Status", st["label"]), _p("Blank form" if blank else d["status"].replace("_", " "), st["cell"])],
    ]
    head_table = _grid(head, [28 * mm, 55 * mm, 32 * mm, 55 * mm, 22 * mm, 60 * mm], st, header=False)
    head_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), _LIGHT), ("BACKGROUND", (2, 0), (2, -1), _LIGHT), ("BACKGROUND", (4, 0), (4, -1), _LIGHT)]))
    story += [head_table, Spacer(1, 6)]

    cols = ["A. Job objectives", "B. % weight", "C. Measurement criteria", "D. Self assessment", "E. Rating (1-5)", "F. Weighted rating (B x E)", "G. Manager assessment (1-5)"]
    rows = [[_p(c, st["head"]) for c in cols]]
    for i in d["items"]:
        rows.append([_p(i["label"], st["cell"]), _p(_num(i["weight"]), st["cell"]), _p(i.get("criteria"), st["cell"]),
                     _p("" if blank else i.get("self_text"), st["cell"]), _p("" if blank else _num(i.get("self_rating")), st["cell"]),
                     _p("" if blank else _num(i.get("self_weighted"), "{:.2f}"), st["cell"]), _p("" if blank else _num(i.get("manager_rating")), st["cell"])])
    rows.append([_p("Total", st["cell"]), _p(_num(sum(i["weight"] for i in d["items"])), st["cell"]), _p("", st["cell"]), _p("", st["cell"]), _p("", st["cell"]),
                 _p("" if blank else _num(d.get("self_weighted_total"), "{:.2f}"), st["cell"]), _p("" if blank else _num(d.get("manager_weighted_total"), "{:.2f}"), st["cell"])])
    story.append(_grid(rows, [38 * mm, 16 * mm, 52 * mm, 78 * mm, 18 * mm, 26 * mm, 29 * mm], st))
    if not blank and d.get("final_weighted_rating") is not None:
        story.append(Paragraph(f"Final weighted rating: <b>{d['final_weighted_rating']:.2f}</b> out of 5", st["cell"]))

    if d["competencies"]:
        story.append(Paragraph("Behavioural competencies", st["h2"]))
        crow = [[_p("Competency", st["head"]), _p("Self rating (1-5)", st["head"]), _p("Manager rating (1-5)", st["head"])]]
        crow += [[_p(c["title"], st["cell"]), _p("" if blank else _num(c.get("self_rating")), st["cell"]), _p("" if blank else _num(c.get("manager_rating")), st["cell"])] for c in d["competencies"]]
        story.append(_grid(crow, [120 * mm, 40 * mm, 40 * mm], st))

    if d["objectives"]:
        story.append(Paragraph("Performance objectives for the next 3 months", st["h2"]))
        orow = [[_p("Objective", st["head"])] + [_p(m, st["head"]) for m in d["objective_months"]]]
        orow += [[_p(o["label"], st["cell"])] + [_p("" if blank else v, st["cell"]) for v in o["values"]] for o in d["objectives"]]
        story.append(_grid(orow, [60 * mm, 50 * mm, 50 * mm, 50 * mm], st))

    story.append(Paragraph("Training and development needs", st["h2"]))
    needs = {n["slot"]: n.get("text") for n in d["development_needs"]} if d["development_needs"] else {}
    story.append(_grid([[_p(f"{s}. " + ("" if blank else (needs.get(s) or "")), st["cell"])] for s in (1, 2)], [200 * mm], st, header=False))

    story.append(Paragraph("Comments and sign-off", st["h2"]))
    srow = [[_p("Step", st["head"]), _p("Name / account", st["head"]), _p("Date", st["head"]), _p("Comment", st["head"])]]
    for c in d["chain"]:
        srow.append([_p(c["label"], st["cell"]), _p("" if blank else c.get("signed_by"), st["cell"]), _p("" if blank or not c.get("signed_at") else c["signed_at"].strftime("%d %b %Y"), st["cell"]), _p("" if blank else c.get("comment"), st["cell"])])
    story.append(_grid(srow, [45 * mm, 50 * mm, 28 * mm, 100 * mm], st))
    doc.build(story)
    return buf.getvalue()


def blank_detail(role_name: str, version, kpis: list, period_label: str = "") -> dict[str, Any]:
    """A fill-in form for a job role: its KPI rows, competencies and objective rows, nothing entered."""
    jd = version.jd or {}
    crit = {r.get("title"): "\n".join(r.get("criteria", [])) for r in jd.get("responsibilities", [])}
    chain = list(version.signoff_chain or []) + (["ceo"] if version.requires_ceo_signoff else [])
    labels = {"staff": "Staff member", "manager": "Immediate manager", "csm": "Customer Service Manager", "rm_gm": "RM / GM", "hr": "HR", "ceo": "CEO"}
    return {
        "profile": {"full_name": "", "role_name": role_name, "unit_name": "", "manager_name": ""}, "period_label": period_label or "____________", "status": "blank",
        "items": [{"label": k.label, "weight": float(k.weight), "criteria": crit.get(k.jd_section) or k.description} for k in kpis],
        "competencies": [{"title": c["title"]} for c in jd.get("competencies", [])],
        "objective_months": ["Month 1", "Month 2", "Month 3"], "objectives": [{"label": o, "values": ["", "", ""]} for o in jd.get("objective_rows", [])],
        "development_needs": [], "chain": [{"label": labels.get(s, s)} for s in chain], "self_weighted_total": None, "manager_weighted_total": None, "final_weighted_rating": None,
    }
