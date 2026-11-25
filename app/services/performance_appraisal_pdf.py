"""
Printable appraisal PDF that mirrors the paper form (columns A-G), plus a blank version per job role.
All text is escaped before it reaches reportlab's mini-markup, and nothing user-supplied is used as a file name.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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


def _pct(v) -> str:
    return f"{v:g}%"


def _num(v, fmt="{:g}") -> str:
    return "" if v is None else fmt.format(v)


# The company's two paper forms (staff and admin) share one layout; this follows it: logo and title, a details box, Part A (columns A-G),
# the assessment guide, Part B (next three months), Part C (training) and a signature box per reviewer.
GUIDE_ROWS = [("Above 120%", "Outstanding", "5"), ("100-120%", "Excellent", "4"), ("80-100%", "Good", "3"), ("60-80%", "Average", "2"), ("Below 60%", "Poor", "1")]
FORM_NOTE = "****Notes: Staff to fill columns D, E and F. Manager to fill column G during the performance discussion session with the staff."
SIGN_NOTE = "On completion, this form should be signed below by the employee, the immediate manager and the Head of HR, General Manager and CEO as appropriate."
TRAINING_NOTE = "Please indicate two (2) training and development initiatives that you need to support the achievement of the job objectives going forward."
_COMMENT_HEADING = {"Staff member": "Appraisee's Comments.", "Immediate manager": "Immediate manager's Comments (appraiser).", "Customer Service Manager": "CSM Comments.",
                    "RM / GM": "RM/GM Comments.", "HR": "HR Comments.", "CEO": "CEO Comments (if applicable)."}


def comment_heading(label: str) -> str:
    """The paper form's heading for each reviewer's box, e.g. 'HR Comments.'"""
    return _COMMENT_HEADING.get(label, f"{label} Comments.")


_LOGO = Path(__file__).resolve().parent.parent / "assets" / "Bidii-logo.png"
_BOX = colors.HexColor("#222222")


def _worked(hire) -> str:
    """'3 yrs 2 mths' from a hire date, or '' when there is none."""
    if not hire:
        return ""
    today = date.today()
    months = max(0, (today.year - hire.year) * 12 + today.month - hire.month - (1 if today.day < hire.day else 0))
    years, rest = divmod(months, 12)
    return " ".join(x for x in (f"{years} yr{'s' if years != 1 else ''}" if years else "", f"{rest} mth{'s' if rest != 1 else ''}" if rest or not years else "") if x)


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.HexColor("#555555"))
    canvas.drawString(doc.leftMargin, 8 * mm, "Bidii Credit Limited - Staff Performance Appraisal")
    canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, 8 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _form_grid(rows, widths, extra=()):
    t = Table(rows, colWidths=widths, repeatRows=0)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, _BOX), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3), *extra]))
    return t


def render(d: dict[str, Any], blank: bool = False) -> bytes:
    """`d` is the dict from performance_appraisal_views.detail (or blank_detail for a role template)."""
    st = _styles()
    st["part"] = ParagraphStyle("part", parent=st["h2"], fontSize=9.5, textColor=colors.black, spaceBefore=10, spaceAfter=4, underlineWidth=0.5, keepWithNext=1)
    st["bold"] = ParagraphStyle("b", parent=st["cell"], fontName="Helvetica-Bold", fontSize=8, leading=10)
    st["ctr"] = ParagraphStyle("ctr", parent=st["bold"], alignment=1)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=15 * mm, title="Staff Performance Appraisal")
    W = A4[0] - 24 * mm
    who = d["profile"]
    story: list[Any] = []

    # logo + title
    logo = Image(str(_LOGO), width=17 * mm, height=13 * mm) if _LOGO.exists() else ""
    title = [Paragraph("BIDII CREDIT LIMITED", st["bold"]), Paragraph("Staff Performance Appraisal Form", st["bold"])]
    if who.get("role_name"):
        title.append(Paragraph(f"Job Role: {escape(str(who['role_name']))}", st["bold"]))
    top = Table([[logo, title]], colWidths=[24 * mm, W - 24 * mm])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("LINEBELOW", (0, 0), (-1, 0), 0.8, _BOX)]))
    story += [top, Spacer(1, 6)]

    # details box
    val = lambda v: _p("" if blank or v is None else v, st["cell"])  # noqa: E731
    lab = lambda t: _p(t, st["label"])  # noqa: E731
    box = Table(
        [[lab("Name:"), val(who["full_name"]), lab("Staff No.:"), val(who.get("employee_no")), lab("Position:"), _p(who.get("role_name"), st["cell"])],
         [lab("My Manager:"), val(who.get("manager_name")), lab("Branch/Dept:"), val(who.get("unit_name")), lab("Years/Months Worked:"), val(_worked(who.get("hire_date")))]],
        colWidths=[22 * mm, 40 * mm, 24 * mm, 28 * mm, 38 * mm, W - 152 * mm],
    )
    box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, _BOX), ("LINEBELOW", (1, 0), (1, 1), 0.4, colors.HexColor("#777777")), ("LINEBELOW", (3, 0), (3, 1), 0.4, colors.HexColor("#777777")),
                             ("LINEBELOW", (5, 0), (5, 1), 0.4, colors.HexColor("#777777")), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story += [box, Paragraph(f"<u>Part A: Performance Assessment Section</u>", st["part"]), Paragraph(f"<b>Assessment Period: {escape(str(d['period_label']))}</b>", st["cell"]), Spacer(1, 3)]

    # Part A - columns A to G
    letters = [_p(c, st["ctr"]) for c in "ABCDEFG"]
    heads = [_p(t, st["bold"]) for t in (
        "Job Objectives for the Period", "% Weight", "Measurement Criteria", "Self-Assessment of performance against KPIs (give examples where appropriate)",
        "Rate each KPI using the 5-level rating below", "Weighted Rating column (B X E)", "Manager Assessment of performance against KPIs (1-5)")]
    rows = [letters, heads]
    for i in d["items"]:
        rows.append([_p(i["label"], st["cell"]), _p(_pct(i["weight"]), st["cell"]), _p(i.get("criteria"), st["cell"]),
                     [Spacer(1, 15 * mm)] if blank else _p(i.get("self_text"), st["cell"]), _p("" if blank else _num(i.get("self_rating")), st["cell"]),
                     _p("" if blank else _num(i.get("self_weighted"), "{:.2f}"), st["cell"]), _p("" if blank else _num(i.get("manager_rating")), st["cell"])])
    rows.append([_p("OVERALL RATING", st["bold"]), _p(_pct(sum(i["weight"] for i in d["items"])), st["cell"]), _p("", st["cell"]), _p("", st["cell"]), _p("", st["cell"]),
                 _p("" if blank else _num(d.get("self_weighted_total"), "{:.2f}"), st["cell"]), _p("" if blank else _num(d.get("manager_weighted_total"), "{:.2f}"), st["cell"])])
    widths = [38 * mm, 13 * mm, 36 * mm, 42 * mm, 17 * mm, 18 * mm, W - 164 * mm]
    main = _form_grid(rows, widths, [("BACKGROUND", (0, -1), (-1, -1), _LIGHT)])
    main.repeatRows = 2
    story.append(main)
    if not blank and d.get("final_weighted_rating") is not None:
        story.append(Paragraph(f"Final weighted rating: <b>{d['final_weighted_rating']:.2f}</b> out of 5", st["cell"]))

    # guide
    story += [Spacer(1, 6), Paragraph(escape(RATING_GUIDE), st["cell"]), Spacer(1, 3), Paragraph("Assessment Guide (Column E)", st["cell"])]
    guide = _form_grid([[_p(x, st["cell"]) for x in ("Achievement", "Description", "Rating")]] + [[_p(x, st["cell"]) for x in r] for r in GUIDE_ROWS], [34 * mm, 34 * mm, 24 * mm])
    guide.hAlign = "LEFT"
    story += [guide, Spacer(1, 3), Paragraph(escape(FORM_NOTE), st["small"])]

    if d["competencies"]:
        story.append(Paragraph("<u>Behavioural competencies</u>", st["part"]))
        crow = [[_p("Competency", st["bold"]), _p("Self rating (1-5)", st["bold"]), _p("Manager rating (1-5)", st["bold"])]]
        crow += [[_p(c["title"], st["cell"]), _p("" if blank else _num(c.get("self_rating")), st["cell"]), _p("" if blank else _num(c.get("manager_rating")), st["cell"])] for c in d["competencies"]]
        story.append(_form_grid(crow, [W - 70 * mm, 35 * mm, 35 * mm]))

    # Part B
    if d["objectives"]:
        story.append(Paragraph("<u>Part B: Performance Objectives for the next 3 months</u>", st["part"]))
        orow = [[_p("", st["bold"])] + [_p(m, st["bold"]) for m in d["objective_months"]]]
        orow += [[_p(o["label"], st["cell"])] + [_p("" if blank else v, st["cell"]) for v in o["values"]] for o in d["objectives"]]
        story.append(_form_grid(orow, [W - 3 * 38 * mm] + [38 * mm] * 3))

    # Part C
    story.append(Paragraph("<u>Part C: Training Requirements</u>", st["part"]))
    needs = {n["slot"]: n.get("text") for n in d["development_needs"]} if d["development_needs"] else {}
    train = _form_grid([[_p(TRAINING_NOTE, st["cell"]), ""]] + [[_p(str(s), st["cell"]), _p("" if blank else (needs.get(s) or ""), st["cell"])] for s in (1, 2)], [10 * mm, W - 10 * mm],
                       [("SPAN", (0, 0), (1, 0)), ("TOPPADDING", (0, 1), (-1, -1), 8), ("BOTTOMPADDING", (0, 1), (-1, -1), 8)])
    story += [train, Spacer(1, 8), Paragraph(escape(SIGN_NOTE), st["bold"]), Spacer(1, 4)]

    # one box per reviewer: comment, then Signed / Date
    srows, styles = [], [("BOX", (0, 0), (-1, -1), 0.8, _BOX), ("LINEBELOW", (0, 0), (-1, -2), 0.6, _BOX), ("VALIGN", (0, 0), (-1, -1), "TOP")]
    for c in d["chain"]:
        signed = "" if blank else (c.get("signed_by") or "")
        when = "" if blank or not c.get("signed_at") else c["signed_at"].strftime("%d %b %Y")
        inner = Table([[_p(comment_heading(c["label"]), st["bold"]), ""], [_p("" if blank else c.get("comment"), st["cell"]), ""], [_p(f"Signed: {signed}", st["bold"]), _p(f"Date: {when}", st["bold"])]],
                      colWidths=[W * 0.62, W * 0.38 - 2], rowHeights=[None, 16 * mm if blank else None, None])
        inner.setStyle(TableStyle([("SPAN", (0, 0), (1, 0)), ("SPAN", (0, 1), (1, 1)), ("LEFTPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        srows.append([inner])
    if srows:
        sign = Table(srows, colWidths=[W])
        sign.setStyle(TableStyle(styles))
        story.append(sign)
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
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
