"""
Renders a CareerApplication + its interview_prep_content (see
app/schemas/interview_prep.py) into a printable PDF the interview panel
can bring into the room - candidate/role header, a short AI-drafted
summary, key strengths and areas to probe, then every question section
(the fixed standard sections plus, when present, the AI-drafted
Role-Specific & Technical one) as a numbered checklist with space to
write notes. Deliberately much simpler than app/services/jd_pdf.py's
fixed letterhead layout - this is a working document for one interview,
not a formal company record - but reuses the same reportlab building
blocks and the same company logo header for visual consistency with
every other generated PDF in this app.
"""

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

if TYPE_CHECKING:
    from app.models.career_application import CareerApplication
    from app.models.job_opening import JobOpening

LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "Bidii-logo.png"

_styles = getSampleStyleSheet()
_body = ParagraphStyle("IPBody", parent=_styles["Normal"], fontSize=9.5, leading=13)
_bold_body = ParagraphStyle("IPBoldBody", parent=_body, fontName="Helvetica-Bold")
_section_title = ParagraphStyle(
    "IPSectionTitle", parent=_styles["Normal"], fontSize=11, fontName="Helvetica-Bold", spaceAfter=6
)
_doc_title = ParagraphStyle("IPDocTitle", parent=_styles["Normal"], fontSize=13, fontName="Helvetica-Bold")
_question_style = ParagraphStyle("IPQuestion", parent=_body, fontName="Helvetica-Bold")
_hint_style = ParagraphStyle("IPHint", parent=_body, fontSize=8.5, textColor=colors.grey)


def _bullets(items: list[str]) -> str:
    return "<br/>".join(f"&#8226; {item}" for item in items) if items else "-"


def _header_footer(canvas, doc, candidate_name: str):
    canvas.saveState()
    if LOGO_PATH.exists():
        logo_width = 1.4 * cm
        logo_height = 1.4 * cm
        canvas.drawImage(
            str(LOGO_PATH),
            (doc.pagesize[0] - logo_width) / 2,
            doc.pagesize[1] - doc.topMargin + 0.3 * cm,
            width=logo_width,
            height=logo_height,
            preserveAspectRatio=True,
            mask="auto",
        )
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(doc.leftMargin, doc.bottomMargin - 0.6 * cm, f"Interview Prep - {candidate_name}")
    canvas.setFont("Helvetica", 8)
    canvas.drawCentredString(
        doc.pagesize[0] / 2, doc.bottomMargin - 0.6 * cm, f"Confidential - Internal Use Only - {date.today().year}"
    )
    canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, doc.bottomMargin - 0.6 * cm, str(canvas.getPageNumber()))
    canvas.restoreState()


def render_interview_prep_pdf(
    *, application: "CareerApplication", job: "JobOpening | None", content: dict, output_path
) -> None:
    """
    Writes the PDF to output_path - a filesystem path (str) or a
    file-like object (e.g. io.BytesIO), both of which reportlab's
    SimpleDocTemplate accepts directly. `content` is the dict form of
    app.schemas.interview_prep.InterviewPrepContent (already validated by
    the caller - this function trusts its shape). `job` is None for a
    "General application" with no linked posting - handled by simply
    omitting the department/location row rather than failing.
    """
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2.6 * cm,
        bottomMargin=2 * cm,
        title=f"Interview Prep - {application.full_name}",
    )

    story = []

    story.append(Paragraph(f"Interview Prep - {application.full_name}", _doc_title))
    story.append(Spacer(1, 10))

    info_rows = [["Role Applied For:", application.role, "Date:", date.today().strftime("%d %b %Y")]]
    if job is not None:
        info_rows.append(["Department:", job.department, "Location:", job.location])
    info_table = Table(info_rows, colWidths=[3.2 * cm, 5.3 * cm, 3.2 * cm, 5.3 * cm])
    info_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(info_table)
    story.append(Spacer(1, 14))

    if content.get("candidate_summary"):
        summary_box = [
            Paragraph("<b>Candidate Summary</b>", _bold_body),
            Spacer(1, 3),
            Paragraph(content["candidate_summary"], _body),
        ]
        summary_table = Table([[summary_box]], colWidths=[17 * cm])
        summary_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.75, colors.black),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(summary_table)
        story.append(Spacer(1, 14))

    if content.get("key_strengths") or content.get("areas_to_probe"):
        sp_rows = [
            [
                Paragraph("<b>Key Strengths</b>", _bold_body),
                Paragraph("<b>Areas to Probe</b>", _bold_body),
            ],
            [
                Paragraph(_bullets(content.get("key_strengths", [])), _body),
                Paragraph(_bullets(content.get("areas_to_probe", [])), _body),
            ],
        ]
        sp_table = Table(sp_rows, colWidths=[8.5 * cm, 8.5 * cm])
        sp_table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(sp_table)
        story.append(Spacer(1, 16))

    for section in content.get("sections", []):
        section_block = [Paragraph(section.get("title", ""), _section_title)]
        for i, q in enumerate(section.get("questions", []), start=1):
            section_block.append(Paragraph(f"{i}. {q.get('question', '')}", _question_style))
            if q.get("why_it_matters"):
                section_block.append(Paragraph(q["why_it_matters"], _hint_style))
            section_block.append(Spacer(1, 4))
            section_block.append(
                Paragraph("Notes: _______________________________________________________________", _body)
            )
            section_block.append(Spacer(1, 10))
        # KeepTogether keeps a section's heading from being orphaned at the
        # bottom of a page while its questions land on the next one - same
        # reasoning as app/services/jd_pdf.py's "Other Responsibilities"
        # block. A long section can still split mid-way if it genuinely
        # doesn't fit on one page - only the heading is protected.
        story.append(KeepTogether(section_block[:2]))
        story.extend(section_block[2:])
        story.append(Spacer(1, 6))

    doc.build(
        story,
        onFirstPage=lambda c, d: _header_footer(c, d, application.full_name),
        onLaterPages=lambda c, d: _header_footer(c, d, application.full_name),
    )
