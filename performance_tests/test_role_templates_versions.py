"""Seeded templates, versioning (in place while unused, new version once locked), immutability."""

from decimal import Decimal as D

import pytest

from app.models.performance import PerformanceKpi, PerformanceRole, PerformanceRoleVersion
from app.services import performance_roles as pr
from app.services.performance_config import ensure_defaults
from app.services.performance_role_templates import TEMPLATES


@pytest.fixture()
def seeded(db):
    ensure_defaults(db)
    pr.seed_role_templates(db)
    return db


def role(db, key):
    return db.query(PerformanceRole).filter_by(key=key).one()


def weights(db, key):
    v = pr.latest_version(db, role(db, key).id)
    return [int(k.weight) for k in pr.kpis_for(db, v.id)]


EXPECTED = {
    "loan_officer": [50, 20, 20, 5, 5],
    "credit_risk_analyst": [30, 25, 15, 15, 15],
    "branch_admin": [20, 20, 20, 30, 10],
    "branch_manager": [40, 20, 15, 15, 10],
    "check_off_agent": [55, 15, 15, 10, 5],
    "debt_recovery": [40, 20, 25, 10, 5],
}


@pytest.mark.parametrize("key,expected", EXPECTED.items())
def test_seeded_kpi_weights_match_the_approved_design(seeded, key, expected):
    assert sorted(weights(seeded, key), reverse=True) == sorted(expected, reverse=True)
    assert sum(weights(seeded, key)) == 100
    v = pr.latest_version(seeded, role(seeded, key).id)
    assert pr.is_configured(v, pr.kpis_for(seeded, v.id))


def test_weights_follow_the_source_documents_in_order(seeded):
    assert weights(seeded, "loan_officer") == [50, 20, 20, 5, 5]  # sales, customers, PAR, retention, self (the form)
    assert weights(seeded, "credit_risk_analyst") == [30, 25, 15, 15, 15]  # the JD's "% of time"
    assert weights(seeded, "branch_admin") == [20, 20, 20, 30, 10]  # the admins form


def test_skeleton_roles_are_present_but_not_configured(seeded):
    for key in ("hr_officer", "marketing_manager", "head_office_staff"):
        v = pr.latest_version(seeded, role(seeded, key).id)
        assert v is not None and v.version == 1
        assert pr.kpis_for(seeded, v.id) == [] and not pr.is_configured(v, [])
        assert len(v.jd["competencies"]) == 6


def test_seeding_is_idempotent_and_respects_existing_versions(db):
    ensure_defaults(db)
    assert pr.seed_role_templates(db) == len(TEMPLATES)
    assert pr.seed_role_templates(db) == 0
    assert db.query(PerformanceRoleVersion).count() == len(TEMPLATES)


def test_admin_edits_are_not_overwritten_by_a_later_seed(seeded):
    r = role(seeded, "loan_officer")
    cleaned = pr.validate_version_payload({"title": "Loan Officer (edited)"}, [], ["staff", "manager"], False)
    pr.save_version(seeded, r, cleaned, "admin")
    assert pr.seed_role_templates(seeded) == 0
    assert pr.latest_version(seeded, r.id).jd["title"] == "Loan Officer (edited)"


def test_custom_roles_get_no_template(db):
    ensure_defaults(db)
    db.add(PerformanceRole(key="custom_x", name="Custom X", is_system=False))
    db.commit()
    pr.seed_role_templates(db)
    assert pr.latest_version(db, role(db, "custom_x").id) is None


def test_template_facts_from_the_documents(seeded):
    cra = pr.latest_version(seeded, role(seeded, "credit_risk_analyst").id)
    assert [c["title"] for c in cra.jd["competencies"]][:2] == ["Putting customers first", "Performing through our people"]
    assert len(cra.jd["competencies"]) == 6 and len(cra.jd["responsibilities"]) == 5
    assert cra.jd["reports_to"] == "Head of Risk and Compliance"
    assert "HR" not in pr.effective_signoff_chain(pr.latest_version(seeded, role(seeded, "branch_admin").id)) and \
        "hr" not in pr.latest_version(seeded, role(seeded, "branch_admin").id).signoff_chain  # the admins form has no HR step
    assert "hr" in pr.latest_version(seeded, role(seeded, "loan_officer").id).signoff_chain
    lo = pr.latest_version(seeded, role(seeded, "loan_officer").id)
    assert lo.jd["objective_rows"] == ["New customers", "Sales", "Quality (PAR)"]
    par = next(k for k in pr.kpis_for(seeded, lo.id) if k.key == "par30")
    assert par.direction == "lower" and par.unit == "percent" and par.source == "kpi_entry"


def test_templates_contain_no_personal_names(seeded):
    blob = " ".join(str(v.jd) for v in seeded.query(PerformanceRoleVersion))
    for name in ("Harrison", "Mwai", "Rose", "Wachira"):
        assert name not in blob  # approvals are job titles, never people


# ---- versioning ----------------------------------------------------------------------


def payload(title="Role", kpi_weights=(100,), label="K"):
    kpis = [{"key": f"k{i}", "label": f"{label}{i}", "unit": "rating", "direction": "higher", "weight": w,
             "source": "appraisal_rating", "frequency": "quarterly"} for i, w in enumerate(kpi_weights)]
    return pr.validate_version_payload({"title": title}, kpis, ["staff", "manager"], False)


def test_first_save_creates_version_1(db):
    ensure_defaults(db)
    r = PerformanceRole(key="new_role", name="New Role")
    db.add(r)
    db.commit()
    v, created = pr.save_version(db, r, payload("A"), "admin")
    assert (v.version, created) == (1, True)


def test_edits_update_in_place_while_unused(seeded):
    r = role(seeded, "loan_officer")
    before = pr.latest_version(seeded, r.id)
    v, created = pr.save_version(seeded, r, payload("Edited", (60, 40), "X"), "admin")
    assert created is False and v.id == before.id and v.version == 1
    assert [k.key for k in pr.kpis_for(seeded, v.id)] == ["k0", "k1"]  # replaced, not appended
    assert seeded.query(PerformanceKpi).filter_by(role_version_id=v.id).count() == 2


def test_once_locked_an_edit_creates_a_new_version_and_old_one_is_unchanged(seeded):
    r = role(seeded, "loan_officer")
    v1 = pr.latest_version(seeded, r.id)
    original_jd = dict(v1.jd)
    original_kpis = [(k.key, D(k.weight)) for k in pr.kpis_for(seeded, v1.id)]
    pr.lock_version(seeded, v1.id)
    v2, created = pr.save_version(seeded, r, payload("Changed", (50, 50), "Y"), "admin", "Q3 review")
    assert created is True and v2.version == 2 and v2.note == "Q3 review"
    assert pr.latest_version(seeded, r.id).id == v2.id
    # the version used by scored periods is exactly as it was
    seeded.refresh(v1)
    assert v1.jd == original_jd
    assert [(k.key, D(k.weight)) for k in pr.kpis_for(seeded, v1.id)] == original_kpis
    assert pr.get_version(seeded, r.id, 1).id == v1.id


def test_new_version_stays_editable_until_it_is_locked_too(seeded):
    r = role(seeded, "loan_officer")
    pr.lock_version(seeded, pr.latest_version(seeded, r.id).id)
    v2, _ = pr.save_version(seeded, r, payload("v2"), "admin")
    v2_again, created = pr.save_version(seeded, r, payload("v2 tweaked"), "admin")
    assert created is False and v2_again.id == v2.id and v2_again.version == 2


def test_lock_is_idempotent(seeded):
    v = pr.latest_version(seeded, role(seeded, "loan_officer").id)
    pr.lock_version(seeded, v.id)
    first = v.locked_at
    pr.lock_version(seeded, v.id)
    seeded.refresh(v)
    assert v.locked_at == first


def test_seeded_loan_officer_has_five_kpis(seeded):
    v = pr.latest_version(seeded, role(seeded, "loan_officer").id)
    assert seeded.query(PerformanceKpi).filter_by(role_version_id=v.id).count() == 5
