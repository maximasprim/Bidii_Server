"""
AI drafting for the JD builder. The AI provider is ALWAYS replaced by a fake here: no test can reach a
real model. Covers: pseudonymisation, drafts-only, strict-JSON handling, deterministic KPI weights,
rules fallback, caching, audit of provider/model/prompt, rate limits and upload abuse.
"""

import asyncio
import io
import json
import zipfile
from decimal import Decimal as D

import pytest
from reportlab.pdfgen import canvas

from app.models.performance import AIOutput, PerformanceRole, PerformanceRoleVersion
from app.services import performance_ai as ai
from app.services import performance_upload as up
from app.services.performance_ratelimit import AI_LIMIT
from performance_tests.org import build_org

BASE = "/api/admin/performance"

JD_LINES = [
    "Job description-Credit Risk Analyst",
    "Position Title: Credit Risk Analyst  Reports To: Head of Risk and Compliance",
    "Overall Role Purpose: To safeguard the loan portfolio.",
    "Key Responsibilities % of Time",
    "Credit Evaluation 30%",
    "Portfolio Monitoring 70%",
    "Contact the HR desk on hr.desk@bidii.example or +254 712 345 678 or ID 12345678.",
    "ACCEPTANCE",
    "I have read and understood my Job Description.",
    "Harrison Mwai",
    "General Manager - Commercial",
    "Rose Wachira",
    "CEO",
]

GOOD_JD = {
    "title": "Credit Risk Analyst", "reports_to": "Head of Risk and Compliance", "purpose": "Safeguard the portfolio.",
    "responsibilities": [
        {"title": "Credit Evaluation", "weight_pct": "30%", "description": "d", "criteria": "Accuracy"},
        {"title": "Portfolio Monitoring", "weight_pct": 70, "criteria": ["Timely", "Accurate"]},
    ],
    "qualifications": ["Degree"], "competencies": ["Delivering results", {"title": "Communicating", "description": "x"}],
}


def make_pdf(lines):
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for line in lines:
        c.drawString(40, y, line)
        y -= 18
    c.save()
    return buf.getvalue()


def make_blank_pdf():
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.rect(10, 10, 50, 50)
    c.save()
    return buf.getvalue()


def make_docx(lines):
    import docx

    d = docx.Document()
    for line in lines[:3]:
        d.add_paragraph(line)
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Credit Evaluation", "30%"
    for line in lines[3:]:
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


class FakeAI:
    def __init__(self):
        self.calls, self.prompts, self.reply = 0, [], json.dumps(GOOD_JD)

    def __call__(self, system_prompt, user_prompt):
        self.calls += 1
        self.prompts.append(system_prompt + "\n" + user_prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply, "gemini", "fake-model-1"


@pytest.fixture()
def fake(monkeypatch):
    f = FakeAI()
    monkeypatch.setattr(ai, "complete_json", f)
    return f


@pytest.fixture()
def org(client, db):
    return build_org(client, db)


def extract(client, org, data, name="jd.pdf", who="admin1", **params):
    return client.post(f"{BASE}/ai/extract-jd", params={"filename": name, **params}, content=data, headers=org.h[who])


# ---- the extraction happy path and what it sends out -----------------------------------------------


def test_extraction_returns_a_cleaned_draft_and_saves_nothing(client, db, org, fake):
    r = extract(client, org, make_pdf(JD_LINES))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jd"]["title"] == "Credit Risk Analyst"
    assert [x["weight_pct"] for x in body["jd"]["responsibilities"]] == [30.0, 70.0]  # "30%" coerced
    assert body["jd"]["responsibilities"][0]["criteria"] == ["Accuracy"]
    assert [c["title"] for c in body["jd"]["competencies"]] == ["Delivering results", "Communicating"]
    assert body["ai"] == {"provider": "gemini", "model": "fake-model-1", "prompt_version": "perf-jd-extract-v1", "cached": False, "label": "AI-generated; review before saving"}
    assert db.query(PerformanceRoleVersion).count() == 0  # a draft: never auto-saved


def test_nothing_personal_reaches_the_provider(client, org, fake):
    assert extract(client, org, make_pdf(JD_LINES)).status_code == 200
    sent = fake.prompts[0]
    for secret in ("Harrison", "Mwai", "Rose", "Wachira", "hr.desk@bidii.example", "712 345 678", "12345678", "I have read and understood"):
        assert secret not in sent, secret
    assert "Credit Evaluation 30%" in sent  # the JD content itself does go


def test_docx_works_too(client, org, fake):
    r = extract(client, org, make_docx(JD_LINES), name="jd.docx")
    assert r.status_code == 200
    sent = fake.prompts[0]
    assert "Credit Evaluation | 30%" in sent  # table cells included, in document order (before the approvals block)
    assert sent.index("Credit Evaluation | 30%") < sent.index("Portfolio Monitoring 70%")  # the table sits before these paragraphs, as authored
    assert "Harrison" not in sent


def test_provider_and_prompt_are_recorded_and_results_cached(client, db, org, fake):
    pdf = make_pdf(JD_LINES)
    assert extract(client, org, pdf).json()["ai"]["cached"] is False
    row = db.query(AIOutput).one()
    assert (row.kind, row.provider, row.model, row.prompt_version) == ("jd_extraction", "gemini", "fake-model-1", "perf-jd-extract-v1")
    assert len(row.input_hash) == 64 and row.created_by == "admin1"
    again = extract(client, org, pdf)
    assert again.json()["ai"]["cached"] is True and fake.calls == 1
    assert extract(client, org, pdf, regenerate="true").json()["ai"]["cached"] is False and fake.calls == 2


# ---- AI misbehaviour ----------------------------------------------------------------------------------


@pytest.mark.parametrize("reply", ["not json at all", "[1, 2]", json.dumps({"unrelated": True}), json.dumps({"title": "", "responsibilities": []}), ""])
def test_unusable_ai_output_is_a_502_with_a_manual_entry_message(client, org, fake, reply):
    fake.reply = reply
    r = extract(client, org, make_pdf(JD_LINES))
    assert r.status_code == 502 and "manually" in r.json()["detail"]


def test_fenced_json_is_accepted(client, org, fake):
    fake.reply = "```json\n" + json.dumps(GOOD_JD) + "\n```"
    assert extract(client, org, make_pdf(JD_LINES)).status_code == 200


def test_provider_failure_and_not_configured(client, org, fake):
    fake.reply = ai.AIFailed("The AI provider couldn't complete that request.")
    assert extract(client, org, make_pdf(JD_LINES)).status_code == 502
    fake.reply = ai.AIUnavailable("No AI provider is configured on the server.")
    assert extract(client, org, make_pdf(JD_LINES)).status_code == 503


def test_with_no_ai_keys_the_real_seam_reports_unavailable_and_never_calls_out(client, org):
    r = extract(client, org, make_pdf(JD_LINES))  # NOT faked: the real complete_json, with blank keys
    assert r.status_code == 503 and "No AI provider is configured" in r.json()["detail"]


def test_weight_problems_in_an_ai_draft_are_warnings_not_failures(client, org, fake):
    bad = dict(GOOD_JD, responsibilities=[{"title": "A", "weight_pct": 30}, {"title": "B", "weight_pct": 30}])
    fake.reply = json.dumps(bad)
    r = extract(client, org, make_pdf(JD_LINES))
    assert r.status_code == 200 and any("must total 100" in w for w in r.json()["warnings"])


def test_both_real_providers_still_expose_the_method_this_module_relies_on():
    from app.services.ai_providers.gemini_provider import GeminiProvider
    from app.services.ai_providers.openai_provider import OpenAIProvider

    assert callable(getattr(OpenAIProvider, "_complete_json", None)) and callable(getattr(GeminiProvider, "_complete_json", None))


# ---- upload abuse ------------------------------------------------------------------------------------------


def _docx_with_huge_member():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w/>")
        z.writestr("pad.bin", b"\0" * (45 * 1024 * 1024))  # compresses to a few KB, unpacks past the cap
    return buf.getvalue()


@pytest.mark.parametrize(
    "data,name,status,fragment",
    [
        (b"hello this is not a document at all", "jd.pdf", 415, "Unsupported"),
        (b"MZ\x90\x00\x03\x00\x00\x00" + b"\0" * 64, "jd.pdf", 415, "Unsupported"),  # an .exe renamed
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64, "jd.doc", 415, "DOCX or PDF"),  # legacy .doc
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64, "jd.docx", 415, "DOCX or PDF"),  # legacy .doc disguised
        (b"%PDF-1.4 junk that is not a pdf", "jd.pdf", 422, "couldn't be read"),
        (b"", "jd.pdf", 400, "empty"),
        (b"%PDF-1.4\n", "jd.docx", 415, "extension"),
        (b"PK\x03\x04" + b"\0" * 40, "jd.pdf", 415, "extension"),
        (b"PK\x03\x04" + b"\0" * 40, "jd.docx", 415, "valid Word"),
        (b"%PDF-1.4\n", "jd.pdf%00.exe", 415, "extension"),
    ],
)
def test_bad_uploads_are_refused_before_any_ai_call(client, org, fake, data, name, status, fragment):
    r = extract(client, org, data, name=name)
    assert r.status_code == status and fragment in r.json()["detail"]
    assert fake.calls == 0


def test_oversize_body_is_refused(client, org, fake):
    r = extract(client, org, b"%PDF-" + b"A" * up.MAX_UPLOAD_BYTES)
    assert r.status_code == 413 and fake.calls == 0


def test_declared_oversize_is_refused_without_reading(client, org, fake):
    r = client.post(f"{BASE}/ai/extract-jd", params={"filename": "jd.pdf"}, content=b"%PDF-small",
                    headers={**org.h["admin1"], "Content-Length": str(up.MAX_UPLOAD_BYTES * 4)})
    assert r.status_code == 413


def test_streaming_read_stops_at_the_cap_instead_of_buffering_everything():
    consumed = {"n": 0}

    async def gen():
        for _ in range(500):
            consumed["n"] += 1
            yield b"x" * (1024 * 1024)

    with pytest.raises(Exception) as exc:
        asyncio.run(up.read_limited(gen(), max_bytes=3 * 1024 * 1024))
    assert exc.value.status_code == 413
    assert consumed["n"] <= 5  # aborted after a handful of chunks, not 500 MB


def test_zip_bomb_docx_is_refused(client, org, fake):
    r = extract(client, org, _docx_with_huge_member(), name="jd.docx")
    assert r.status_code == 413 and fake.calls == 0


def test_scanned_or_textless_pdf_gets_a_clear_message(client, org, fake):
    r = extract(client, org, make_blank_pdf())
    assert r.status_code == 422 and "scan" in r.json()["detail"] and fake.calls == 0


def test_password_protected_pdf_is_refused(client, org, fake):
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter()
    for page in PdfReader(io.BytesIO(make_pdf(JD_LINES))).pages:
        w.add_page(page)
    w.encrypt("secret")
    buf = io.BytesIO()
    w.write(buf)
    r = extract(client, org, buf.getvalue())
    assert r.status_code == 422 and "password" in r.json()["detail"]


def test_client_content_type_is_ignored_and_filename_is_never_a_path(client, org, fake):
    pdf = make_pdf(JD_LINES)
    r = client.post(f"{BASE}/ai/extract-jd", params={"filename": "../../etc/passwd.pdf"}, content=pdf,
                    headers={**org.h["admin1"], "Content-Type": "text/plain"})
    assert r.status_code == 200  # the content is a real PDF; the name is only compared by extension
    lying = client.post(f"{BASE}/ai/extract-jd", params={"filename": "a.pdf"}, content=b"just text", headers={**org.h["admin1"], "Content-Type": "application/pdf"})
    assert lying.status_code == 415  # claiming application/pdf doesn't make it one


@pytest.mark.parametrize("who", ["lo1", "bm1", "gm", "ceo"])
def test_ai_endpoints_are_admin_or_hr_only(client, org, fake, who):
    assert extract(client, org, make_pdf(JD_LINES), who=who).status_code == 403
    body = {"responsibilities": [{"title": "A", "weight_pct": 100}]}
    assert client.post(f"{BASE}/ai/suggest-kpis", json=body, headers=org.h[who]).status_code == 403
    assert fake.calls == 0


def test_hr_can_use_the_ai_endpoints(client, org, fake):
    assert extract(client, org, make_pdf(JD_LINES), who="hr1").status_code == 200
    fake.reply = json.dumps({"kpis": [{"responsibility_index": 0, "label": "K"}]})
    body = {"responsibilities": [{"title": "A", "weight_pct": 100}]}
    assert client.post(f"{BASE}/ai/suggest-kpis", json=body, headers=org.h["hr1"]).status_code == 200


# ---- rate limiting -------------------------------------------------------------------------------------------------


def test_ai_endpoints_are_rate_limited_per_user(client, org, fake):
    body = {"responsibilities": [{"title": "A", "weight_pct": 100}]}
    fake.reply = json.dumps({"kpis": [{"responsibility_index": 0, "label": "K"}]})
    codes = [client.post(f"{BASE}/ai/suggest-kpis", json=dict(body, regenerate=True), headers=org.h["admin1"]).status_code for _ in range(AI_LIMIT + 1)]
    assert codes[:AI_LIMIT] == [200] * AI_LIMIT and codes[-1] == 429
    limited = client.post(f"{BASE}/ai/suggest-kpis", json=body, headers=org.h["admin1"])
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0
    # extraction shares the budget, even for rejected uploads (they cost a request too)
    assert extract(client, org, b"junk").status_code == 429


def test_rate_limit_is_per_user(client, db, org, fake):
    from performance_tests.conftest import add_user, token_headers

    other = token_headers(add_user(db, "admin2", "admin"))
    body = {"responsibilities": [{"title": "A", "weight_pct": 100}]}
    fake.reply = json.dumps({"kpis": [{"responsibility_index": 0, "label": "K"}]})
    for _ in range(AI_LIMIT + 1):
        client.post(f"{BASE}/ai/suggest-kpis", json=dict(body, regenerate=True), headers=org.h["admin1"])
    assert client.post(f"{BASE}/ai/suggest-kpis", json=body, headers=other).status_code == 200


# ---- KPI suggestion ----------------------------------------------------------------------------------------------------


RESPS = [{"title": "Sales", "weight_pct": 60, "criteria": ["Monthly disbursement"]}, {"title": "Quality", "weight_pct": 40, "criteria": ["PAR"]}]


def suggest(client, org, resps=RESPS, **extra):
    return client.post(f"{BASE}/ai/suggest-kpis", json={"responsibilities": resps, **extra}, headers=org.h["admin1"])


def test_weights_are_computed_from_the_responsibilities_never_taken_from_the_ai(client, org, fake):
    fake.reply = json.dumps({"kpis": [
        {"responsibility_index": 0, "label": "Disbursed", "unit": "kes", "direction": "higher", "source": "kpi_entry", "frequency": "monthly", "weight": 99},
        {"responsibility_index": 0, "label": "Collections", "unit": "kes", "weight": 1},
        {"responsibility_index": 1, "label": "PAR", "unit": "percent", "direction": "lower"},
    ]})
    body = suggest(client, org).json()
    assert body["ai_used"] is True and body["warnings"] == []
    assert [(k["label"], k["weight"], k["jd_section"]) for k in body["kpis"]] == [("Disbursed", 30.0, "Sales"), ("Collections", 30.0, "Sales"), ("PAR", 40.0, "Quality")]
    assert sum(D(str(k["weight"])) for k in body["kpis"]) == D(100)
    assert '"weight' not in fake.prompts[0].split("KEY RESPONSIBILITIES:")[1]  # no weights even go out


def test_invalid_ai_values_fall_back_to_safe_defaults_and_bad_indexes_are_ignored(client, org, fake):
    fake.reply = json.dumps({"kpis": [
        {"responsibility_index": 0, "label": "K", "unit": "pounds", "direction": "sideways", "source": "magic", "frequency": "hourly"},
        {"responsibility_index": 7, "label": "Ghost"}, {"responsibility_index": -1, "label": "Ghost2"}, {"responsibility_index": True, "label": "Ghost3"},
        {"responsibility_index": 1, "label": ""},
    ]})
    body = suggest(client, org).json()
    k = body["kpis"][0]
    assert (k["unit"], k["direction"], k["source"], k["frequency"]) == ("rating", "higher", "appraisal_rating", "quarterly")
    assert [x["label"] for x in body["kpis"]] == ["K", "Quality"]  # responsibility 1 had no usable proposal -> rules KPI
    assert [x["weight"] for x in body["kpis"]] == [60.0, 40.0]


def test_rules_fallback_when_ai_is_unavailable(client, org):
    body = suggest(client, org).json()  # real seam, no keys
    assert body["ai_used"] is False and body["ai"] is None and "No AI provider" in body["fallback_reason"]
    assert [(k["label"], k["weight"]) for k in body["kpis"]] == [("Sales", 60.0), ("Quality", 40.0)]


def test_rules_fallback_when_ai_output_is_garbage(client, org, fake):
    fake.reply = "totally not json"
    body = suggest(client, org).json()
    assert body["ai_used"] is False and len(body["kpis"]) == 2


def test_suggested_kpis_validate_when_saved(client, db, org, fake):
    """The suggestion is a draft in exactly the shape the save endpoint accepts."""
    fake.reply = json.dumps({"kpis": [{"responsibility_index": 0, "label": "One"}, {"responsibility_index": 1, "label": "Two"}]})
    kpis = suggest(client, org).json()["kpis"]
    rid = db.query(PerformanceRole).filter_by(key="hr_officer").one().id
    jd = {"title": "HR Officer", "responsibilities": RESPS}
    r = client.put(f"{BASE}/roles/{rid}/jd", json={"jd": jd, "kpis": kpis, "signoff_chain": ["staff", "manager"], "requires_ceo_signoff": False}, headers=org.h["admin1"])
    assert r.status_code == 200 and r.json()["configured"] is True


@pytest.mark.parametrize("total,parts", [("10", 3), ("100", 3), ("33.33", 2), ("1", 7), ("40", 1)])
def test_split_weight_is_exact(total, parts):
    shares = ai.split_weight(D(total), parts)
    assert sum(shares) == D(total) and len(shares) == parts and all(s >= 0 for s in shares)


def test_unbalanced_responsibilities_produce_a_warning_not_a_crash(client, org, fake):
    fake.reply = json.dumps({"kpis": [{"responsibility_index": 0, "label": "A"}]})
    body = suggest(client, org, [{"title": "A", "weight_pct": 30}, {"title": "B", "weight_pct": 30}]).json()
    assert any("not 100" in w for w in body["warnings"])


def test_suggestions_are_cached_and_recorded(client, db, org, fake):
    fake.reply = json.dumps({"kpis": [{"responsibility_index": 0, "label": "A"}, {"responsibility_index": 1, "label": "B"}]})
    first, second = suggest(client, org).json(), suggest(client, org).json()
    assert first["ai"]["cached"] is False and second["ai"]["cached"] is True and fake.calls == 1
    assert second["kpis"] == first["kpis"]
    row = db.query(AIOutput).filter_by(kind="kpi_suggestion").one()
    assert (row.provider, row.model, row.prompt_version) == ("gemini", "fake-model-1", "perf-kpi-suggest-v1")
    assert suggest(client, org, regenerate=True).json()["ai"]["cached"] is False and fake.calls == 2


def test_responsibility_input_is_validated(client, org, fake):
    assert suggest(client, org, []).status_code == 400
    assert suggest(client, org, [{"title": "", "weight_pct": 100}]).status_code == 400
    assert suggest(client, org, [{"title": "A", "weight_pct": "lots"}]).status_code == 400
    assert fake.calls == 0
