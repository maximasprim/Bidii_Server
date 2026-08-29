"""
Renders a JobOpening + its jd_content (see app/schemas/job_description.py)
into a PDF matching Bidii Credit's fixed formal Job Description layout —
the same layout as the company's existing "JD - Regional Manager" template
(letterhead, info table, Key Responsibilities/%-of-Time/Performance-Criteria
table, Other Responsibilities table, a fixed Essential-Knowledge box, a
fixed company-wide Performance and Behavioral Competencies section, and an
Acceptance/Approvals signature block).

Only the role-specific sections (role purpose, key responsibilities,
qualifications, experience & skills, reports-to, and the "other
responsibilities" lines) come from jd_content / the job record itself.
Everything else — headings, table structure, the six behavioral
competencies, and the signature block text — is fixed on purpose, so every
generated JD keeps the same company format ("the JD format should exactly
be the same, just tailored for the different roles").
"""

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

if TYPE_CHECKING:
    from app.models.job_opening import JobOpening

LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "bidii_logo.png"

# Fixed company approvers on every generated JD, matching the existing
# template this was modeled on. Edit here if the actual signatories change
# — there's nowhere else in the app that needs updating.
APPROVER_1_NAME = "Harrison Mwai"
APPROVER_1_TITLE = "General Manager - Commercial"
APPROVER_2_NAME = "Rose Wachira"
APPROVER_2_TITLE = "CEO"

# Fixed company-wide behavioral competencies — identical on every JD,
# regardless of role, matching the existing template exactly.
BEHAVIORAL_COMPETENCIES = [
    (
        "Putting customers first",
        "understands the value of profitable customers. Listening to and understanding customer needs. "
        "Delivering outstanding customer service.",
    ),
    (
        "Performing through our people",
        "motivates people & teams to perform. Values & adapts to different cultures. Recruits high "
        "performers. Develops & coaches the team to succeed.",
    ),
    (
        "Delivering results",
        "can do. Sets & prioritizes challenging targets. Decisive, makes decisions. Focused, manages own "
        "time & other resources. Is cost conscious without reducing profitability and compromising on "
        "quality. Manages risks.",
    ),
    (
        "Managing a changing environment",
        "analytical, simplifies the complex & ambiguous. Thinks laterally & creatively. Displays sound "
        "judgment, solves problems.",
    ),
    (
        "Making a personal difference",
        "positive & courageous. Open, trustworthy & trusting. Resilient, takes personal responsibility. "
        "Curious, seeks opportunities to learn.",
    ),
    ("Communicating for impact", "communicates with enthusiasm & clarity. Inspires and influences others."),
]

_styles = getSampleStyleSheet()
_body = ParagraphStyle("JDBody", parent=_styles["Normal"], fontSize=9.5, leading=13)
_bold_body = ParagraphStyle("JDBoldBody", parent=_body, fontName="Helvetica-Bold")
_cell_heading = ParagraphStyle("JDCellHeading", parent=_body, fontName="Helvetica-Bold", spaceAfter=2)
_section_title = ParagraphStyle(
    "JDSectionTitle", parent=_styles["Normal"], fontSize=11, fontName="Helvetica-Bold", spaceAfter=6
)
_doc_title = ParagraphStyle("JDDocTitle", parent=_styles["Normal"], fontSize=13, fontName="Helvetica-Bold")
_small = ParagraphStyle("JDSmall", parent=_body, fontSize=8, textColor=colors.grey)


def _bullets(items: list[str]) -> str:
    return "<br/>".join(f"&#8226; {item}" for item in items) if items else "—"


def _numbered(items: list[str]) -> str:
    return "<br/>".join(f"{i}) {item}" for i, item in enumerate(items, start=1)) if items else "—"


def _header_footer(canvas, doc, job_title: str):
    canvas.saveState()
    if LOGO_PATH.exists():
        logo_width = 1.4 * cm
        logo_height = 1.4 * cm
        canvas.drawImage(
            str(LOGO_PATH),
            doc.leftMargin,
            doc.pagesize[1] - doc.topMargin + 0.3 * cm,
            width=logo_width,
            height=logo_height,
            preserveAspectRatio=True,
            mask="auto",
        )
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(doc.leftMargin, doc.bottomMargin - 0.6 * cm, f"JD – {job_title}")
    canvas.setFont("Helvetica", 8)
    canvas.drawCentredString(doc.pagesize[0] / 2, doc.bottomMargin - 0.6 * cm, "HR Document – JD - 2025")
    canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, doc.bottomMargin - 0.6 * cm, str(canvas.getPageNumber()))
    canvas.restoreState()


def render_jd_pdf(*, job: "JobOpening", jd_content: dict, output_path) -> None:
    """
    Writes the PDF to output_path — a filesystem path (str) or a
    file-like object (e.g. io.BytesIO), both of which reportlab's
    SimpleDocTemplate accepts directly. jd_content is the dict form of
    app.schemas.job_description.JDContent (already validated by the
    caller — this function trusts its shape).
    """
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2.6 * cm,
        bottomMargin=2 * cm,
        title=f"Job Description - {job.title}",
    )

    story = []

    story.append(Paragraph(f"Job description-{job.title}", _doc_title))
    story.append(Spacer(1, 10))

    info_table = Table(
        [
            ["Position Title:", job.title, "Reports To:", jd_content.get("reports_to") or "—"],
            ["Branch", job.location, "Department", job.department],
            ["Grade", "", "Date", str(date.today().year)],
            [
                Paragraph(f"<b>Overall Role Purpose:</b> {jd_content.get('overall_role_purpose', '')}", _body),
                "",
                "",
                "",
            ],
        ],
        colWidths=[3.2 * cm, 5.3 * cm, 3.2 * cm, 5.3 * cm],
    )
    info_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
                ("SPAN", (0, 3), (-1, 3)),
                ("FONTNAME", (0, 0), (0, 2), "Helvetica-Bold"),
                ("FONTNAME", (2, 0), (2, 2), "Helvetica-Bold"),
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

    kr_rows = [
        [
            Paragraph("<b>Key Responsibilities</b>", _cell_heading),
            Paragraph("<b>% of Time</b>", _cell_heading),
            Paragraph("<b>Performance Measurement Criteria to Meet Objectives</b>", _cell_heading),
        ]
    ]
    for item in jd_content.get("key_responsibilities", []):
        left = Paragraph(f"<u>{item.get('heading', '')}</u><br/>{_numbered(item.get('bullets', []))}", _body)
        pct = Paragraph(f"{item.get('pct_time', 0)}%", _body)
        right = Paragraph(_numbered(item.get("criteria", [])), _body)
        kr_rows.append([left, pct, right])

    kr_table = Table(kr_rows, colWidths=[7.5 * cm, 2 * cm, 7.5 * cm], repeatRows=1)
    kr_table.setStyle(
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
    story.append(kr_table)
    story.append(Spacer(1, 14))

    story.append(Paragraph("Other Responsibilities", _section_title))
    other_rows = [
        [
            Paragraph("<b>Reporting Relationships:</b> Indicate the jobs that report to this position.", _body),
            Paragraph(jd_content.get("reporting_relationships") or "—", _body),
        ],
        [
            Paragraph("<b>Decision Making Mandates/Constraints:</b>", _body),
            Paragraph(jd_content.get("decision_making_mandates") or "—", _body),
        ],
        [
            Paragraph("<b>Planning Responsibility:</b>", _body),
            Paragraph(jd_content.get("planning_responsibility") or "—", _body),
        ],
        [
            Paragraph(
                "<b>Relationship Management:</b> departments that the position holder will need to relate/liaise "
                "with as part of this role:",
                _body,
            ),
            Paragraph(jd_content.get("relationship_management") or "—", _body),
        ],
    ]
    other_table = Table(other_rows, colWidths=[9 * cm, 8 * cm])
    other_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(other_table)

    story.append(PageBreak())

    qual_box_content = [
        Paragraph(
            "<b>Essential role related knowledge, skills, qualifications and experience at selection.</b>", _body
        ),
        Spacer(1, 4),
        Paragraph("<b>Minimum Qualifications</b>", _bold_body),
        Paragraph(_bullets(jd_content.get("minimum_qualifications", [])), _body),
        Spacer(1, 6),
        Paragraph("<b>Experience and Skills</b>", _bold_body),
        Paragraph(_bullets(jd_content.get("experience_and_skills", [])), _body),
    ]
    qual_table = Table([[qual_box_content]], colWidths=[17 * cm])
    qual_table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.75, colors.black),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(qual_table)
    story.append(Spacer(1, 14))

    competency_lines = [Paragraph("<b>Performance and Behavioral Competencies</b>", _body), Spacer(1, 4)]
    for name, desc in BEHAVIORAL_COMPETENCIES:
        competency_lines.append(Paragraph(f"&#8658;&nbsp; <b>{name}</b> – {desc}", _body))
        competency_lines.append(Spacer(1, 3))
    competency_table = Table([[competency_lines]], colWidths=[17 * cm])
    competency_table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.75, colors.black),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(competency_table)
    story.append(Spacer(1, 20))

    story.append(Paragraph("<b>ACCEPTANCE</b>", _body))
    story.append(Spacer(1, 6))
    story.append(
        Paragraph(
            "I have read and understood my Job Description. I understand that this Job Description together with "
            "agreed objectives will be used as a basis to evaluate my performance.",
            _body,
        )
    )
    story.append(Spacer(1, 14))
    story.append(Paragraph("<b>SIGNED BY JOB HOLDER</b>", _body))
    story.append(Spacer(1, 24))
    story.append(Paragraph("Name: _______________________  Signature: _______________  Date: _____________", _body))
    story.append(Spacer(1, 24))
    story.append(Paragraph("<b>APPROVALS</b>", _body))
    story.append(Spacer(1, 24))
    story.append(Paragraph(f"_______________________  Date: _____________<br/><b>{APPROVER_1_NAME}</b><br/>{APPROVER_1_TITLE}", _body))
    story.append(Spacer(1, 24))
    story.append(Paragraph(f"_______________________  Date: _____________<br/><b>{APPROVER_2_NAME}</b><br/>{APPROVER_2_TITLE}", _body))

    doc.build(
        story,
        onFirstPage=lambda c, d: _header_footer(c, d, job.title),
        onLaterPages=lambda c, d: _header_footer(c, d, job.title),
    )
