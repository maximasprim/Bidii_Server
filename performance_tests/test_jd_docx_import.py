"""
JD import from a house-layout .docx.

The company's JDs change all the time but the LAYOUT does not, so the reader is layout-driven. These tests
use real JDs (performance_tests/fixtures/jds) AND deliberately alter their layout - more rows, fewer
competencies, renamed labels, bad weights - to prove it follows the layout, not these particular files.
The AI provider is never reached: in the "local" tests it would raise if it were.
"""

import copy
import io
import json
from pathlib import Path

import docx
import pytest

from app.models.performance import PerformanceAuditLog, PerformanceRoleVersion
from app.services import jd_docx_reader as reader
from app.services import performance_roles as roles
from performance_tests.test_ai_roles import BASE, JD_LINES, extract, fake, make_docx, make_pdf, org  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures" / "jds"


def jd_bytes(name: str) -> bytes:
    return (FIXTURES / f"{name}.docx").read_bytes()


def edited(name: str, fn) -> bytes:
    document = docx.Document(io.BytesIO(jd_bytes(name)))
    fn(document)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def body_of(client, org, data, **params):
    r = extract(client, org, data, name="jd.docx", **params)
    assert r.status_code == 200, r.text
    return r.json()


# what each real fixture must read as: (title, reports_to, weights, criteria-per-responsibility, competency count)
EXPECTED = {
    "JD - Regional Manager": ("Regional Manager", "General Manager - Commercial", [10, 50, 20, 5, 15], [1, 5, 2, 1, 4], 6),
    "JD - Office Assistant": ("Office Assistant", "HR Officer", [35, 20, 15, 10, 20], [2, 2, 2, 2, 2], 4),
    "JD - Chief Operating Officer": ("Chief Operating Officer", "Chief Executive Officer", [20, 40, 20, 10, 10], [1, 3, 1, 2, 4], 6),
    "JD - Debt Collection & Recovery Manager": ("Debt Collection & Recovery Manager", "Head – Credit, Risk and Compliance /General Manager", [60, 15, 10, 15], [5, 3, 2, 2], 6),
    "JD - Direct Sales Executive (Revised 2021)": ("Direct Sales Executives (DSEs) /BDO", "Sales Team Leader", [10, 40, 30, 10, 10], [1, 5, 2, 1, 2], 4),
}


# ---- real JDs are read exactly, with no AI ---------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_real_jd_is_read_exactly_without_ai(client, db, org, name):
    # NOTE: no `fake` fixture - if this reached the AI layer the real complete_json would raise (no key configured)
    title, reports_to, weights, criteria, n_comp = EXPECTED[name]
    body = body_of(client, org, jd_bytes(name))
    jd = body["jd"]
    assert (jd["title"], jd["reports_to"]) == (title, reports_to)
    assert [r["weight_pct"] for r in jd["responsibilities"]] == weights
    assert [len(r["criteria"]) for r in jd["responsibilities"]] == criteria
    assert len(jd["competencies"]) == n_comp
    assert jd["purpose"] and jd["qualifications"]
    assert body["ai"]["provider"] == "local" and body["ai"]["cached"] is False and "no AI" in body["ai"]["label"]
    assert body["warnings"] == []  # these are clean JDs
    assert db.query(PerformanceRoleVersion).count() == 0  # a draft: nothing saved


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_what_the_reader_returns_passes_the_roles_validator_unchanged(client, org, name):
    jd = body_of(client, org, jd_bytes(name))["jd"]
    errors: list[str] = []
    again = roles.validate_jd(jd, errors)
    assert errors == []
    assert [r["weight_pct"] for r in again["responsibilities"]] == [r["weight_pct"] for r in jd["responsibilities"]]


def test_competency_tier_is_whatever_the_document_has(client, org):
    four = body_of(client, org, jd_bytes("JD - Office Assistant"))["jd"]["competencies"]
    assert [c["title"] for c in four] == ["Putting customers first", "Delivering results", "Making a personal difference", "Communicating for impact"]
    assert four[0]["description"].startswith("Understands the value")  # capitalised like the system defaults
    assert all(c["key"] for c in four)


def test_no_person_names_come_back(client, org):
    text = json.dumps(body_of(client, org, jd_bytes("JD - Regional Manager")))
    for name in ("Harrison", "Mwai", "Rose", "Wachira", "Geoffrey", "Wandeto"):
        assert name not in text


def test_it_is_audited_and_costs_no_ai_call(client, db, org, fake):
    body_of(client, org, jd_bytes("JD - Regional Manager"))
    assert fake.calls == 0
    row = db.query(PerformanceAuditLog).filter(PerformanceAuditLog.action == "jd_read_from_docx").one()
    assert row.details == {"reader": "docx-layout-v1"}


def test_force_ai_skips_the_reader(client, org, fake):
    body = body_of(client, org, jd_bytes("JD - Regional Manager"), force_ai="true")
    assert fake.calls == 1 and body["ai"]["provider"] == "gemini"


# ---- the layout is what matters, not the content ----------------------------------------------------


def _responsibility_table(document):
    return next(t for t in document.tables if t.rows[0].cells[0].text.strip().startswith("Key Responsibilities"))


def test_an_added_responsibility_row_is_picked_up_and_the_new_total_is_flagged(client, org):
    def add_row(document):
        table = _responsibility_table(document)
        table._tbl.append(copy.deepcopy(table.rows[-1]._tr))

    body = body_of(client, org, edited("JD - Regional Manager", add_row))
    assert [r["weight_pct"] for r in body["jd"]["responsibilities"]] == [10, 50, 20, 5, 15, 15]
    assert any("115" in w and "must total 100" in w for w in body["warnings"])  # told, never silently "fixed"


def test_fewer_competencies_and_any_number_of_rows_are_fine(client, org):
    def trim(document):
        box = next(t for t in document.tables if "competenc" in t.rows[0].cells[0].text.lower())
        paragraphs = box.rows[1].cells[0].paragraphs
        for p in paragraphs[4:]:
            p._p.getparent().remove(p._p)
        table = _responsibility_table(document)
        table._tbl.remove(table.rows[-1]._tr)  # and one responsibility fewer

    jd = body_of(client, org, edited("JD - Regional Manager", trim))["jd"]
    assert len(jd["competencies"]) == 4 and len(jd["responsibilities"]) == 4


def test_labels_are_matched_loosely(client, org):
    def rename(document):
        info = document.tables[0]
        for row in info.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    for r in p.runs:
                        r.text = r.text.replace("Position Title", "Job Title").replace("Overall Role Purpose", "Overall, Role Purpose")

    jd = body_of(client, org, edited("JD - Regional Manager", rename))["jd"]
    assert jd["title"] == "Regional Manager" and jd["purpose"].startswith("To deliver")


def test_spacer_rows_are_ignored(client, org):
    def spacer(document):
        table = _responsibility_table(document)
        blank = copy.deepcopy(table.rows[1]._tr)
        for t in blank.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"):
            t.text = ""
        table.rows[2]._tr.addprevious(blank)

    jd = body_of(client, org, edited("JD - Regional Manager", spacer))["jd"]
    assert [r["weight_pct"] for r in jd["responsibilities"]] == [10, 50, 20, 5, 15]


def test_a_missing_weight_and_missing_criteria_come_back_as_warnings_not_errors(client, org):
    def damage(document):
        table = _responsibility_table(document)
        for cell in table.rows[1].cells:
            for p in cell.paragraphs:
                if p.text.strip() == "10%":
                    p.runs[0].text = "n/a"
                    for r in p.runs[1:]:
                        r.text = ""
        for p in table.rows[3].cells[2].paragraphs:
            for r in p.runs:
                r.text = ""

    body = body_of(client, org, edited("JD - Regional Manager", damage))
    joined = " | ".join(body["warnings"])
    assert "no performance measurement criteria" in joined
    assert "weight" in joined.lower() or "total 100" in joined


# ---- what it must NOT take over ---------------------------------------------------------------------


def test_a_free_form_docx_still_goes_to_ai_as_before(client, org, fake):
    r = extract(client, org, make_docx(JD_LINES), name="jd.docx")
    assert r.status_code == 200 and fake.calls == 1 and r.json()["ai"]["provider"] == "gemini"


def test_pdfs_still_go_to_ai_as_before(client, org, fake):
    r = extract(client, org, make_pdf(JD_LINES))
    assert r.status_code == 200 and fake.calls == 1


@pytest.mark.parametrize("data", [b"", b"not a zip at all", b"PK\x03\x04garbage", make_docx(JD_LINES)])
def test_reader_returns_none_for_anything_that_is_not_the_layout(data):
    assert reader.read_house_jd(data) is None


def test_reader_returns_none_for_a_docx_with_no_tables():
    d = docx.Document()
    d.add_paragraph("Job description - Something")
    buf = io.BytesIO()
    d.save(buf)
    assert reader.read_house_jd(buf.getvalue()) is None


def test_flat_one_column_responsibility_list_is_left_to_ai():
    """The old Debt Collection Officer / Office Assistant layout has no '% of time' column, so it is not read as the layout."""
    d = docx.Document()
    info = d.add_table(rows=2, cols=4)
    info.rows[0].cells[0].text, info.rows[0].cells[1].text, info.rows[0].cells[2].text, info.rows[0].cells[3].text = "Position Title:", "X", "Reports To:", "Y"
    flat = d.add_table(rows=2, cols=1)
    flat.rows[0].cells[0].text, flat.rows[1].cells[0].text = "Key Responsibilities", "- do things"
    buf = io.BytesIO()
    d.save(buf)
    assert reader.read_house_jd(buf.getvalue()) is None
