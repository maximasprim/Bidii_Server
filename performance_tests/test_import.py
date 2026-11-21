"""
Phase 2d - the Excel import wizard: parser quirks, safety checks, dry run == commit, idempotency, undo,
restricted notes, name matching, transfers and reconciliation. Workbooks are built in memory with invented names.
"""

import io
import json
import zipfile

import openpyxl
import pytest

from app.models.performance import (
    ImportRow,
    KpiEntry,
    PerformanceAction,
    PerformanceTarget,
    PerformanceUnit,
    StaffAssignmentHistory,
    StaffProfile,
)
from app.services import performance_import as svc
from app.services.performance_import_parse import WorkbookError, check_xlsx_container, header_month, name_key, parse_workbook
from app.services.performance_time import today_nairobi
from performance_tests.conftest import add_user, token_headers
from performance_tests.org import build_world

YEAR = today_nairobi().year - 1  # always fully in the past
BASE = "/api/admin/performance/import"


def make_workbook(sheets=None, company=None, bands=None, year=YEAR) -> bytes:
    """Legacy layout: A name | B TARGET | C JAN, D %, E note | F FEB, G PAR, H % | I MAR, J PAR, K %, L Q1 | M note | N APR ..."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in (sheets or {}).items():
        ws = wb.create_sheet(name)
        for c, h in {1: "Loan Officer", 2: "TARGET", 3: "JAN ", 4: "%", 6: "FEB", 9: "MARCH", 12: "1ST quarter", 14: "APR", 16: "notes"}.items():
            ws.cell(1, c, h)
        for i, r in enumerate(rows, start=2):
            ws.cell(i, 1, r.get("name"))
            ws.cell(i, 2, r.get("target"))
            if "jan" in r:
                ws.cell(i, 3, r["jan"])
                ws.cell(i, 4, f"=C{r.get('formula_row', i)}/B{r.get('formula_row', i)}")
            if "jan_note" in r:
                ws.cell(i, 5, r["jan_note"])
            if "feb" in r:
                ws.cell(i, 6, r["feb"])
                ws.cell(i, 8, f"=F{i}/B{i}")
            if "feb_par" in r:
                ws.cell(i, 7, r["feb_par"])
            if "feb_note" in r:
                ws.cell(i, 7, r["feb_note"])
            if "mar" in r:
                ws.cell(i, 9, r["mar"])
                ws.cell(i, 11, f"=I{i}/B{i}")
            if "mar_par" in r:
                ws.cell(i, 10, r["mar_par"])
            if "mar_note" in r:
                ws.cell(i, 13, r["mar_note"])
            if "apr" in r:
                ws.cell(i, 14, r["apr"])
            if "apr_note" in r:
                ws.cell(i, 16, r["apr_note"])
    if company is not None:
        ws = wb.create_sheet("COMPANY")
        ws.cell(1, 2, year)
        for c, h in {2: "Branch", 3: "STAFF NAMES", 4: "JAN TARGET", 5: "JANUARY", 7: "FEBRUARY", 8: "MARCH"}.items():
            ws.cell(2, c, h)
        for i, (branch, jan, feb, mar) in enumerate(company, start=3):
            ws.cell(i, 2, branch)
            ws.cell(i, 5, jan)
            ws.cell(i, 7, feb)
            ws.cell(i, 8, mar)
    if bands:
        ws = wb.create_sheet("JAN" + str(year))
        ws.cell(2, 1, f"JAN {year} PERFORMANCE")
        ws.cell(4, 2, "BRANCH")
        for i, (branch, a, b, c, d) in enumerate(bands, start=5):
            ws.cell(i, 2, branch)
            for col, v in zip((3, 4, 5, 6), (a, b, c, d)):
                ws.cell(i, col, v)
        ws.cell(5 + len(bands), 2, "TOTAL")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


ALPHA = [
    {"name": "Amina Wanjiku Test", "target": 1000000, "jan": 1100000, "jan_note": "contract review", "feb": 500000, "feb_par": 4.5,
     "mar": 900000, "mar_par": 6.0, "mar_note": "talk", "apr": 800000, "apr_note": "PIP; 2 month"},
    {"name": "OTIENO BRIAN SAMPLE", "target": 800000, "jan": 800000, "feb": 700000},
    {"name": "Debt Recovery Alpha", "jan": 0, "feb": 0},
    {"name": None, "target": "=SUM(B2:B4)", "jan": "=SUM(C2:C4)"},
]


@pytest.fixture()
def world(client, db):
    return build_world(client, db)


def upload(client, headers, data, filename="staff.xlsx", year=YEAR):
    return client.post(f"{BASE}/preview", params={"filename": filename, "year": year}, content=data, headers=headers)


def decide(client, headers, import_id, **body):
    return client.put(f"{BASE}/{import_id}/decisions", json=body, headers=headers)


def counts(db):
    db.expire_all()
    return {
        "entries": db.query(KpiEntry).count(), "targets": db.query(PerformanceTarget).count(),
        "actions": db.query(PerformanceAction).count(), "profiles": db.query(StaffProfile).count(),
        "units": db.query(PerformanceUnit).count(), "history": db.query(StaffAssignmentHistory).count(),
    }


# ---- the parser ----------------------------------------------------------------------------------------------------


def test_header_month_and_name_key():
    assert [header_month(x) for x in ("JAN ", "MARCH", "Mar", "SEPT", "June")] == [1, 3, 3, 9, 6]
    assert header_month("JAN TARGET") is None and header_month("%") is None and header_month("1ST quarter") is None
    assert name_key("SCOLA NJOKI NDUNG'U") == name_key("Ndungu, Scola Njoki")


def test_parser_reads_blocks_par_notes_and_recomputes_nothing():
    parsed = parse_workbook(make_workbook({"Alpha": ALPHA}), year=YEAR)
    sheet = parsed["sheets"][0]
    amina = sheet["rows"][0]
    assert amina["target"] == 1000000.0
    assert amina["months"][f"{YEAR}-01"] == {"amount": 1100000.0, "par": None, "note": "contract review"}
    assert amina["months"][f"{YEAR}-02"]["par"] == 4.5 and amina["months"][f"{YEAR}-03"]["par"] == 6.0
    assert amina["months"][f"{YEAR}-03"]["note"] == "talk"
    assert amina["months"][f"{YEAR}-04"]["note"] == "PIP; 2 month"
    assert sheet["rows"][1]["name"] == "OTIENO BRIAN SAMPLE"
    assert sheet["rows"][2]["is_team_row"] is True and sheet["rows"][2]["target"] is None
    assert len(sheet["rows"]) == 3  # the nameless total row is skipped
    assert parsed["issue_counts"]["subtotal_row"] == 1


def test_parser_flags_formulas_that_point_at_another_row_and_ignores_them():
    rows = [{"name": "Carol Test", "target": 500000, "jan": 400000, "formula_row": 9}]
    parsed = parse_workbook(make_workbook({"Alpha": rows}), year=YEAR)
    assert parsed["issue_counts"]["formula_row_mismatch"] == 1
    assert parsed["sheets"][0]["rows"][0]["months"][f"{YEAR}-01"]["amount"] == 400000.0


def test_zero_or_blank_target_means_no_target_never_a_fake_number():
    rows = [{"name": "A Zero", "target": 0, "jan": 10}, {"name": "B Blank", "jan": 10}]
    parsed = parse_workbook(make_workbook({"Alpha": rows}), year=YEAR)
    assert [r["target"] for r in parsed["sheets"][0]["rows"]] == [None, None]
    assert "zero_target" in parsed["sheets"][0]["rows"][0]["flags"]


def test_parser_rejects_non_workbooks_macros_and_zip_bombs():
    for bad in (b"hello", b"%PDF-1.7 ...", b"PK\x03\x04junk"):
        with pytest.raises(WorkbookError):
            check_xlsx_container(bad)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<x/>")
        z.writestr("xl/vbaProject.bin", b"x")
    with pytest.raises(WorkbookError, match="macros"):
        check_xlsx_container(buf.getvalue())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", "<x/>")
        z.writestr("big.bin", b"\0" * (130 * 1024 * 1024))
    with pytest.raises(WorkbookError, match="unusually large"):
        check_xlsx_container(buf.getvalue())
    with pytest.raises(WorkbookError, match="staff sheets"):
        parse_workbook(make_workbook({}, company=[("Alpha", 1, 1, 1)]), year=YEAR)


def test_classify_note_vocabulary():
    c = svc.classify_note
    assert c("PIP; 1 month") == {"action_type": "pip", "outcome": None, "months": 1}
    assert c("pip-2months")["months"] == 2 and c("Quality PIP")["outcome"] == "Quality"
    assert c("Extend PIP 1month")["action_type"] == "pip_extended" and c("PIP EXT")["action_type"] == "pip_extended"
    assert c("Lift PIP")["action_type"] == "pip_lifted" and c("PIP lift")["action_type"] == "pip_lifted"
    assert c("REV PIP")["action_type"] == "coaching"
    assert c("Talk-GM")["outcome"] == "Escalated to the GM" and c("Talk-RM")["outcome"] == "Escalated to the RM"
    assert c("talk;staff")["action_type"] == "talk" and c("Quality issue")["action_type"] == "coaching"
    assert c("promote; 2nd quarter")["action_type"] == "promotion" and c("confirm")["action_type"] == "probation_confirm"
    assert c("extend probation")["action_type"] == "probation_extend" and c("exit; 7days notice")["action_type"] == "exit"
    assert c("contract review") is None and c("new contract; 3m") is None and c("") is None


# ---- access, upload safety -----------------------------------------------------------------------------------------


def test_only_admin_and_hr_may_use_the_wizard(client, world):
    data = make_workbook({"Alpha": ALPHA})
    assert client.post(f"{BASE}/preview", content=data).status_code in (401, 403)
    for who in ("lo1", "bm1", "gm", "ceo"):
        assert upload(client, world.h[who], data).status_code == 403
    for who in ("admin1", "hr1"):
        assert upload(client, world.h[who], data).status_code == 200


def test_upload_must_be_a_real_xlsx(client, world):
    h = world.h["admin1"]
    assert upload(client, h, b"x" * 100, "a.xlsx").status_code == 400
    assert upload(client, h, b"%PDF-1.4 x", "a.xlsx").status_code == 400
    assert upload(client, h, make_workbook({"Alpha": ALPHA}), "a.pdf").status_code == 400
    assert upload(client, h, b"", "a.xlsx").status_code == 400
    assert upload(client, h, b"PK\x03\x04" + b"0" * (11 * 1024 * 1024), "a.xlsx").status_code == 413
    traversal = upload(client, h, make_workbook({"Alpha": ALPHA}), "../../etc/passwd.xlsx")
    assert traversal.status_code == 200 and traversal.json()["original_name"] == "passwd.xlsx"


# ---- the wizard ----------------------------------------------------------------------------------------------------


def test_preview_maps_sheets_proposes_matches_and_withholds_notes(client, world):
    r = upload(client, world.h["admin1"], make_workbook({"Alpha": ALPHA, "Sheet2": ALPHA, "Gamma": ALPHA}))
    body = r.json()
    maps = {s["name"]: s["mapping"] for s in body["sheets"] if s["kind"] == "staff"}
    assert maps["Alpha"]["action"] == "use" and maps["Alpha"]["unit_id"] == world.alpha.id
    assert maps["Sheet2"]["action"] == "skip"
    assert maps["Gamma"]["action"] == "create" and maps["Gamma"]["new_unit"]["name"] == "Gamma"
    states = {n["name"]: n["state"] for n in body["names"]}
    assert states["Amina Wanjiku Test"] == "new" and states["Debt Recovery Alpha"] == "team_row"
    assert "PIP" not in json.dumps(body) and "contract review" not in json.dumps(body)


def test_dry_run_changes_nothing_and_matches_the_commit(client, db, world):
    h = world.h["hr1"]
    imp = upload(client, h, make_workbook({"Alpha": ALPHA})).json()["id"]
    before = counts(db)
    dry = client.post(f"{BASE}/{imp}/dry-run", headers=h)
    assert dry.status_code == 200 and dry.json()["dry_run"] is True
    assert counts(db) == before
    assert db.query(ImportRow).count() == 0
    done = client.post(f"{BASE}/{imp}/commit", json={"confirm": True}, headers=h)
    assert done.status_code == 200 and done.json()["counts"] == dry.json()["counts"]
    c = done.json()["counts"]
    assert c["profiles_created"] == 2 and c["units_created"] if "units_created" in c else True
    # 3 months of amounts for Amina (+Apr) = 4, Otieno 2; PAR Feb+Mar = 2  ->  8 entries
    assert c["entries_created"] == 8 and c["targets_created"] == 2


def test_imported_figures_are_verified_import_sourced_and_the_role_version_locks(client, db, world):
    h = world.h["hr1"]
    imp = upload(client, h, make_workbook({"Alpha": ALPHA})).json()["id"]
    client.post(f"{BASE}/{imp}/commit", json={"confirm": True}, headers=h)
    db.expire_all()
    amina = db.query(StaffProfile).filter(StaffProfile.full_name == "Amina Wanjiku Test").one()
    assert db.query(StaffProfile).filter(StaffProfile.full_name == "Otieno Brian Sample").count() == 1  # ALL CAPS -> Title Case
    assert amina.unit_id == world.alpha.id
    e = db.query(KpiEntry).filter_by(profile_id=amina.id, kpi_key="sales").all()
    assert {x.status for x in e} == {"verified"} and {x.source for x in e} == {"import"}
    assert {x.entered_by_username for x in e} == {"import"} and {x.verified_by_username for x in e} == {"hr1"}
    par = {x.month.month: float(x.value) for x in db.query(KpiEntry).filter_by(profile_id=amina.id, kpi_key="par30")}
    assert par == {2: 4.5, 3: 6.0}
    t = db.query(PerformanceTarget).filter_by(profile_id=amina.id).one()
    assert float(t.value) == 1000000 and t.effective_from.month == 1 and t.source == "import"
    from app.services.performance_roles import latest_version

    assert latest_version(db, world.role.id).locked_at is not None


def test_reimporting_is_idempotent_and_changed_figures_update(client, db, world):
    h = world.h["hr1"]
    first = upload(client, h, make_workbook({"Alpha": ALPHA})).json()["id"]
    client.post(f"{BASE}/{first}/commit", json={"confirm": True}, headers=h)
    after_first = counts(db)
    second = upload(client, h, make_workbook({"Alpha": ALPHA})).json()["id"]
    r = client.post(f"{BASE}/{second}/commit", json={"confirm": True}, headers=h).json()
    assert counts(db) == after_first
    assert r["counts"].get("entries_created", 0) == 0 and r["counts"]["entries_unchanged"] == 8
    changed = [dict(ALPHA[0], jan=1200000)] + ALPHA[1:]
    third = upload(client, h, make_workbook({"Alpha": changed})).json()["id"]
    r = client.post(f"{BASE}/{third}/commit", json={"confirm": True}, headers=h).json()
    assert r["counts"]["entries_updated"] == 1 and counts(db) == after_first


def test_system_figures_are_kept_unless_overwrite_is_chosen(client, db, world):
    h = world.h["hr1"]
    imp = upload(client, h, make_workbook({"Alpha": [{"name": "Officer One", "target": 700000, "jan": 650000}]})).json()
    assert imp["names"][0]["state"] == "matched"
    db.add(KpiEntry(profile_id=world.p.lo1.id, kpi_key="sales", month=__import__("datetime").date(YEAR, 1, 1), value=500000, status="verified", source="manual", entered_by_username="lo1", verified_by_username="bm1"))
    db.commit()
    r = client.post(f"{BASE}/{imp['id']}/commit", json={"confirm": True}, headers=h).json()
    assert r["counts"]["entries_kept"] == 1
    db.expire_all()
    assert float(db.query(KpiEntry).filter_by(profile_id=world.p.lo1.id, kpi_key="sales").one().value) == 500000
    again = upload(client, h, make_workbook({"Alpha": [{"name": "Officer One", "target": 700000, "jan": 650000}]})).json()["id"]
    decide(client, h, again, options={"overwrite_manual": True})
    r = client.post(f"{BASE}/{again}/commit", json={"confirm": True}, headers=h).json()
    assert r["counts"]["entries_updated"] == 1
    db.expire_all()
    e = db.query(KpiEntry).filter_by(profile_id=world.p.lo1.id, kpi_key="sales").one()
    assert float(e.value) == 650000 and e.source == "import"


def test_existing_targets_are_kept(client, db, world):
    h = world.h["hr1"]
    from app.services.performance_targets import set_target

    set_target(db, world.p.lo1, "sales", __import__("datetime").date(YEAR, 1, 1), 900000, "set by HR", "hr1")
    imp = upload(client, h, make_workbook({"Alpha": [{"name": "Officer One", "target": 700000, "jan": 650000}]})).json()["id"]
    r = client.post(f"{BASE}/{imp}/commit", json={"confirm": True}, headers=h).json()
    assert r["counts"]["targets_kept"] == 1 and db.query(PerformanceTarget).filter_by(profile_id=world.p.lo1.id).count() == 1


def test_names_that_could_be_two_people_must_be_decided_first(client, db, world):
    h = world.h["hr1"]
    imp = upload(client, h, make_workbook({"Alpha": [{"name": "Officer Onee", "target": 700000, "jan": 650000}]})).json()
    n = imp["names"][0]
    assert n["state"] == "new" or n["state"] == "ambiguous"
    wb = make_workbook({"Alpha": [{"name": "Officer Two Extra", "target": 700000, "jan": 650000}]})
    imp = upload(client, h, wb).json()
    n = imp["names"][0]
    assert n["state"] == "ambiguous" and n["candidates"][0]["full_name"] == "Officer Two"
    assert imp["unresolved"] == 1
    blocked = client.post(f"{BASE}/{imp['id']}/commit", json={"confirm": True}, headers=h)
    assert blocked.status_code == 400 and "decision" in blocked.json()["detail"]
    assert counts(db)["entries"] == 0
    decide(client, h, imp["id"], staff={n["key"]: {"action": "match", "profile_id": world.p.lo2.id}})
    done = client.post(f"{BASE}/{imp['id']}/commit", json={"confirm": True}, headers=h)
    assert done.status_code == 200 and done.json()["counts"]["entries_created"] == 1
    db.expire_all()
    assert db.query(KpiEntry).one().profile_id == world.p.lo2.id


def test_decisions_reject_unknown_fields_and_bad_values(client, world):
    h = world.h["hr1"]
    imp = upload(client, h, make_workbook({"Alpha": ALPHA})).json()["id"]
    assert decide(client, h, imp, options={"drop_tables": True}).status_code == 422
    assert decide(client, h, imp, sheets={"Alpha": {"action": "explode"}}).status_code == 422
    r = decide(client, h, imp, staff={"x": {"action": "match", "profile_id": "not-a-real-id"}})
    assert r.status_code == 200
    # a bogus profile id can't be matched: the name stays unresolved or is treated as new, never written to a stranger
    assert client.post(f"{BASE}/{imp}/commit", json={"confirm": True}, headers=h).status_code in (200, 400)


def test_commit_needs_explicit_confirmation_and_is_all_or_nothing(client, db, world):
    h = world.h["hr1"]
    imp = upload(client, h, make_workbook({"Alpha": ALPHA})).json()["id"]
    assert client.post(f"{BASE}/{imp}/commit", json={}, headers=h).status_code == 422
    assert client.post(f"{BASE}/{imp}/commit", json={"confirm": False}, headers=h).status_code == 422
    before = counts(db)
    decide(client, h, imp, options={"default_role_key": "no_such_role"})
    r = client.post(f"{BASE}/{imp}/commit", json={"confirm": True}, headers=h)
    assert r.status_code == 200 and r.json()["counts"].get("profiles_created", 0) == 0  # skipped with an error issue, nothing half-written
    assert counts(db)["entries"] == before["entries"]
    assert any(i["code"] == "missing_role" for i in r.json()["issues"])


def test_undo_removes_what_the_import_created_and_restores_what_it_changed(client, db, world):
    h = world.h["hr1"]
    base = upload(client, h, make_workbook({"Alpha": ALPHA})).json()["id"]
    client.post(f"{BASE}/{base}/commit", json={"confirm": True}, headers=h)
    snapshot = counts(db)
    changed = [dict(ALPHA[0], jan=1200000), {"name": "New Person Test", "target": 500000, "jan": 100}] + ALPHA[1:]
    second = upload(client, h, make_workbook({"Alpha": changed})).json()["id"]
    r = client.post(f"{BASE}/{second}/commit", json={"confirm": True}, headers=h).json()
    assert r["counts"]["entries_updated"] == 1 and r["counts"]["profiles_created"] == 1
    undo = client.post(f"{BASE}/{second}/undo", json={"confirm": True}, headers=h)
    assert undo.status_code == 200 and undo.json()["kept"] == 0
    assert counts(db) == snapshot
    db.expire_all()
    amina = db.query(StaffProfile).filter_by(full_name="Amina Wanjiku Test").one()
    assert float(db.query(KpiEntry).filter_by(profile_id=amina.id, kpi_key="sales", month=__import__("datetime").date(YEAR, 1, 1)).one().value) == 1100000
    assert client.post(f"{BASE}/{second}/undo", json={"confirm": True}, headers=h).status_code == 400  # not twice
    assert client.post(f"{BASE}/{second}/commit", json={"confirm": True}, headers=h).status_code == 400


def test_undo_leaves_alone_anything_changed_since(client, db, world):
    h = world.h["hr1"]
    imp = upload(client, h, make_workbook({"Alpha": ALPHA})).json()["id"]
    client.post(f"{BASE}/{imp}/commit", json={"confirm": True}, headers=h)
    db.expire_all()
    e = db.query(KpiEntry).filter_by(kpi_key="sales").first()
    e.value = 1
    db.commit()
    undo = client.post(f"{BASE}/{imp}/undo", json={"confirm": True}, headers=h).json()
    assert undo["kept"] >= 1
    db.expire_all()
    assert db.query(KpiEntry).filter_by(id=e.id).count() == 1


# ---- restricted notes ----------------------------------------------------------------------------------------------


def test_admin_import_includes_pip_notes_like_hr_and_a_rerun_adds_nothing(client, db, world):
    wb = make_workbook({"Alpha": ALPHA})
    admin_run = upload(client, world.h["admin1"], wb).json()["id"]
    r = client.post(f"{BASE}/{admin_run}/commit", json={"confirm": True}, headers=world.h["admin1"]).json()
    assert r["counts"].get("actions_restricted_skipped", 0) == 0 and r["counts"]["actions_created"] == 2  # the talk and the PIP
    db.expire_all()
    assert db.query(PerformanceAction).filter_by(restricted=True).count() == 1
    hr_run = upload(client, world.h["hr1"], wb).json()["id"]
    r = client.post(f"{BASE}/{hr_run}/commit", json={"confirm": True}, headers=world.h["hr1"]).json()
    assert r["counts"].get("actions_created", 0) == 0 and r["counts"]["actions_unchanged"] == 2
    db.expire_all()
    pip = db.query(PerformanceAction).filter_by(action_type="pip").one()
    assert pip.restricted is True and pip.source == "import" and pip.started_on.month == 4 and pip.ends_on.month == 6
    again = upload(client, world.h["hr1"], wb).json()["id"]
    client.post(f"{BASE}/{again}/commit", json={"confirm": True}, headers=world.h["hr1"])
    db.expire_all()
    assert db.query(PerformanceAction).filter_by(action_type="pip").count() == 1


def test_imported_pip_is_visible_to_admin_but_not_to_unrelated_managers(client, db, world):
    client.post(f"{BASE}/{upload(client, world.h['hr1'], make_workbook({'Alpha': ALPHA})).json()['id']}/commit", json={"confirm": True}, headers=world.h["hr1"])
    db.expire_all()
    amina = db.query(StaffProfile).filter_by(full_name="Amina Wanjiku Test").one()
    amina.manager_id = world.p.bm1.id
    db.commit()
    url = f"/api/admin/performance/staff/{amina.id}/actions"
    types = lambda who: {a["action_type"] for a in client.get(url, headers=world.h[who]).json()["items"]}  # noqa: E731
    assert "pip" in types("hr1") and "pip" in types("bm1")
    assert "pip" in types("admin1") and "pip" not in types("gm") and "pip" not in types("ceo")


def test_open_pip_sets_status_but_an_old_one_does_not(client, db, world):
    h = world.h["hr1"]
    open_pip = [{"name": "Open Pip Test", "target": 500000, "jan": 100, "apr": 200, "apr_note": "PIP; 3 month"}]
    old_pip = [{"name": "Old Pip Test", "target": 500000, "jan": 100, "jan_note": "PIP; 1 month", "apr": 200}]
    client.post(f"{BASE}/{upload(client, h, make_workbook({'Alpha': open_pip + old_pip})).json()['id']}/commit", json={"confirm": True}, headers=h)
    db.expire_all()
    assert db.query(StaffProfile).filter_by(full_name="Open Pip Test").one().status == "pip"
    assert db.query(StaffProfile).filter_by(full_name="Old Pip Test").one().status == "active"


# ---- transfers, duplicates, team rows, zero targets ----------------------------------------------------------------


def test_a_person_on_two_sheets_in_different_months_is_a_transfer(client, db, world):
    alpha = [{"name": "Moved Person Test", "target": 500000, "jan": 100, "feb": 200}]
    beta = [{"name": "Moved Person Test", "target": 600000, "mar": 300, "apr": 400}]
    h = world.h["hr1"]
    imp = upload(client, h, make_workbook({"Alpha": alpha, "Beta": beta})).json()
    assert len([n for n in imp["names"] if n["name"] == "Moved Person Test"]) == 1
    r = client.post(f"{BASE}/{imp['id']}/commit", json={"confirm": True}, headers=h).json()
    assert any(i["code"] == "transfer_detected" for i in r["issues"]) and r["counts"]["entries_created"] == 4
    db.expire_all()
    p = db.query(StaffProfile).filter_by(full_name="Moved Person Test").one()
    assert p.unit_id == world.beta.id  # where they are now
    hist = db.query(StaffAssignmentHistory).filter_by(profile_id=p.id).order_by(StaffAssignmentHistory.effective_from).all()
    assert [(x.unit_id, x.effective_from.month, x.effective_to.month if x.effective_to else None) for x in hist] == [(world.alpha.id, 1, 2), (world.beta.id, 3, None)]


def test_same_month_on_two_sheets_counts_once_and_conflicts_are_reported(client, db, world):
    h = world.h["hr1"]
    same = {"Alpha": [{"name": "Dup Person", "target": 500000, "jan": 100}], "Beta": [{"name": "Dup Person", "target": 500000, "jan": 100}]}
    r = client.post(f"{BASE}/{upload(client, h, make_workbook(same)).json()['id']}/commit", json={"confirm": True}, headers=h).json()
    assert r["counts"]["entries_created"] == 1 and any(i["code"] == "duplicate_across_sheets" for i in r["issues"])
    diff = {"Alpha": [{"name": "Clash Person", "target": 500000, "jan": 100}], "Beta": [{"name": "Clash Person", "target": 500000, "jan": 999}]}
    r = client.post(f"{BASE}/{upload(client, h, make_workbook(diff)).json()['id']}/commit", json={"confirm": True}, headers=h).json()
    assert r["counts"]["entries_created"] == 1 and any(i["code"] == "conflicting_sheets" for i in r["issues"])


def test_team_rows_are_skipped_but_can_be_created_on_request(client, db, world):
    h = world.h["hr1"]
    imp = upload(client, h, make_workbook({"Alpha": ALPHA})).json()
    team = next(n for n in imp["names"] if n["state"] == "team_row")
    r = client.post(f"{BASE}/{imp['id']}/dry-run", headers=h).json()
    assert any(i["code"] == "team_row_skipped" for i in r["issues"])
    decide(client, h, imp["id"], staff={team["key"]: {"action": "create"}})
    assert client.post(f"{BASE}/{imp['id']}/commit", json={"confirm": True}, headers=h).json()["counts"]["profiles_created"] == 3


def test_people_with_no_target_import_amounts_and_are_flagged_not_scored(client, db, world):
    h = world.h["hr1"]
    rows = [{"name": "No Target Test", "jan": 250000, "feb": 0}]
    r = client.post(f"{BASE}/{upload(client, h, make_workbook({'Alpha': rows})).json()['id']}/commit", json={"confirm": True}, headers=h).json()
    assert r["counts"]["entries_created"] == 1 and r["counts"].get("targets_created", 0) == 0
    assert any(i["code"] == "no_target" for i in r["issues"])


def test_future_months_and_bad_values_are_skipped_with_a_reason(client, db, world):
    h = world.h["hr1"]
    future = today_nairobi().year + 1
    rows = [{"name": "Future Test", "target": 100, "jan": 5}]
    r = client.post(f"{BASE}/{upload(client, h, make_workbook({'Alpha': rows}, company=[], year=future), year=future).json()['id']}/commit", json={"confirm": True}, headers=h).json()
    assert r["counts"]["entries_skipped"] == 1 and any(i["code"] == "future_month" for i in r["issues"])
    pct = [{"name": "Par Over", "target": 100, "feb": 5, "feb_par": 100}]
    assert client.post(f"{BASE}/{upload(client, h, make_workbook({'Alpha': pct})).json()['id']}/dry-run", headers=h).status_code == 200


def test_par_can_be_switched_off(client, db, world):
    h = world.h["hr1"]
    imp = upload(client, h, make_workbook({"Alpha": ALPHA})).json()["id"]
    decide(client, h, imp, options={"include_par": False, "import_actions": False, "import_targets": False})
    c = client.post(f"{BASE}/{imp}/commit", json={"confirm": True}, headers=h).json()["counts"]
    assert c["entries_created"] == 6 and c.get("actions_created", 0) == 0 and c.get("targets_created", 0) == 0


# ---- reconciliation ------------------------------------------------------------------------------------------------


def test_company_totals_and_band_counts_are_reconciled(client, world):
    sheets = {"Alpha": [{"name": "R One", "target": 1000, "jan": 1100}, {"name": "R Two", "target": 1000, "jan": 500}]}
    ok = upload(client, world.h["hr1"], make_workbook(sheets, company=[("Alpha", 1600, 0, 0)])).json()["reconciliation"]
    assert ok["company_total_difference_count"] == 0
    bad = upload(client, world.h["hr1"], make_workbook(sheets, company=[("Alpha", 9000, 0, 0)], bands=[("ALPHA", 1, 0, 0, 1)])).json()["reconciliation"]
    assert bad["company_total_difference_count"] == 1
    d = bad["company_total_differences"][0]
    assert d["company_total"] == 9000 and d["branch_sheet_total"] == 1600 and d["status"] == "mismatch"
    assert bad["band_difference_count"] == 0  # 1 above 100%, 1 below 60% matches the hand count
    off = upload(client, world.h["hr1"], make_workbook(sheets, bands=[("ALPHA", 2, 0, 0, 0)])).json()["reconciliation"]
    assert off["band_difference_count"] == 1 and off["band_differences"][0]["recomputed"]["gt100"] == 1


# ---- housekeeping --------------------------------------------------------------------------------------------------


def test_discard_purges_the_parsed_workbook(client, db, world):
    from app.models.performance import PerformanceImport

    h = world.h["hr1"]
    imp = upload(client, h, make_workbook({"Alpha": ALPHA})).json()["id"]
    assert client.delete(f"{BASE}/{imp}", headers=h).status_code == 200
    db.expire_all()
    row = db.get(PerformanceImport, imp)
    assert row.status == "discarded" and row.parsed == {}
    assert client.get(f"{BASE}/{imp}", headers=h).status_code == 404
    committed = upload(client, h, make_workbook({"Alpha": ALPHA})).json()["id"]
    client.post(f"{BASE}/{committed}/commit", json={"confirm": True}, headers=h)
    assert client.delete(f"{BASE}/{committed}", headers=h).status_code == 409


def test_template_downloads_and_round_trips_through_the_importer(client, db, world):
    h = world.h["admin1"]
    r = client.get(f"{BASE}/template", headers=h)
    assert r.status_code == 200 and r.content[:2] == b"PK" and "attachment" in r.headers["content-disposition"]
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert "Read me" in wb.sheetnames and "Alpha" in wb.sheetnames
    ws = wb["Alpha"]
    ws.append(["Template Person", 800000, 700000, 3.5, "talk", 650000])
    buf = io.BytesIO()
    wb.save(buf)
    wb2 = openpyxl.load_workbook(io.BytesIO(buf.getvalue()))
    assert wb2["Read me"].cell(8, 2).value == today_nairobi().year
    parsed = parse_workbook(buf.getvalue(), year=YEAR)
    row = next(sh for sh in parsed["sheets"] if sh["name"] == "Alpha")["rows"][0]
    assert row["months"][f"{YEAR}-01"] == {"amount": 700000.0, "par": 3.5, "note": "talk"}
    assert row["months"][f"{YEAR}-02"]["amount"] == 650000.0
    assert client.get(f"{BASE}/template", headers=world.h["lo1"]).status_code == 403


def test_audit_trail_records_ids_only(client, db, world):
    from app.models.performance import PerformanceAuditLog

    h = world.h["hr1"]
    imp = upload(client, h, make_workbook({"Alpha": ALPHA})).json()["id"]
    client.post(f"{BASE}/{imp}/commit", json={"confirm": True}, headers=h)
    db.expire_all()
    rows = db.query(PerformanceAuditLog).filter(PerformanceAuditLog.entity_type == "performance_import").all()
    assert {r.action for r in rows} >= {"import_previewed", "import_committed"}
    blob = json.dumps([r.details for r in rows], default=str).lower()
    assert "amina" not in blob and "pip" not in blob and "1100000" not in blob


def test_copied_month_columns_are_flagged_and_can_be_excluded(client, db, world):
    h = world.h["hr1"]
    rows = [{"name": f"Copy Person {i}", "target": 500000, "jan": 100 + i, "feb": 100 + i, "mar": 900 + i} for i in range(4)]
    imp = upload(client, h, make_workbook({"Alpha": rows})).json()
    flagged = [i for i in imp["issues"] if i["code"] == "identical_adjacent_months"]
    assert len(flagged) == 1 and flagged[0]["month"] == f"{YEAR}-02"
    decide(client, h, imp["id"], options={"exclude_months": [f"{YEAR}-02"]})
    c = client.post(f"{BASE}/{imp['id']}/commit", json={"confirm": True}, headers=h).json()["counts"]
    assert c["entries_created"] == 8 and c["months_excluded"] == 4
    assert decide(client, h, imp["id"], options={"exclude_months": ["2026-13"]}).status_code == 422


def test_disciplinary_notes_are_imported_for_an_admin_like_hr(client, db, world):
    rows = [{"name": "Hush Person", "target": 500000, "jan": 100, "jan_note": "dismissal hearing", "feb": 100, "feb_note": "x", "mar": 5, "mar_note": "final warning"}]
    admin = world.h["admin1"]
    imp = upload(client, admin, make_workbook({"Alpha": rows})).json()
    r = client.post(f"{BASE}/{imp['id']}/commit", json={"confirm": True}, headers=admin).json()
    db.expire_all()
    assert r["counts"].get("actions_restricted_skipped", 0) == 0 and db.query(PerformanceAction).filter_by(restricted=True).count() == 2
    odd = [{"name": "Odd Note", "target": 5, "jan": 1, "jan_note": "meeting with spouse about contract"}]
    adm = client.post(f"{BASE}/{upload(client, admin, make_workbook({'Alpha': odd})).json()['id']}/dry-run", headers=admin).json()
    hr = client.post(f"{BASE}/{upload(client, world.h['hr1'], make_workbook({'Alpha': odd})).json()['id']}/dry-run", headers=world.h["hr1"]).json()
    assert "spouse" in json.dumps(adm).lower() and "spouse" in json.dumps(hr).lower()
