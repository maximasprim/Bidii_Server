"""JD / KPI / sign-off validation (pure, no database)."""

from decimal import Decimal as D

import pytest

from app.services import performance_roles as pr


def good(**over):
    payload = {
        "jd": {
            "title": "Analyst", "responsibilities": [
                {"title": "Alpha", "weight_pct": 60, "criteria": ["a"]},
                {"title": "Beta", "weight_pct": 40, "criteria": []},
            ],
        },
        "kpis": [
            {"label": "Alpha KPI", "unit": "rating", "direction": "higher", "weight": 70, "source": "appraisal_rating", "frequency": "quarterly", "jd_section": "Alpha"},
            {"key": "beta_kpi", "label": "Beta KPI", "unit": "kes", "direction": "higher", "weight": 30, "source": "kpi_entry", "frequency": "monthly"},
        ],
        "signoff_chain": ["staff", "manager", "rm_gm", "hr"],
        "requires_ceo_signoff": False,
    }
    payload.update(over)
    return payload


def check(payload):
    return pr.validate_version_payload(payload["jd"], payload["kpis"], payload["signoff_chain"], payload["requires_ceo_signoff"])


def errors_of(payload):
    with pytest.raises(pr.JDValidationError) as exc:
        check(payload)
    return exc.value.errors


def test_good_payload_is_cleaned_and_keys_are_generated():
    out = check(good())
    assert [k["key"] for k in out["kpis"]] == ["alpha_kpi", "beta_kpi"]  # slug from the label when no key given
    assert out["kpis"][0]["weight"] == D("70") and out["kpis"][0]["sort_order"] == 1
    assert out["jd"]["responsibilities"][0]["weight_pct"] == 60.0


def test_skeleton_with_no_responsibilities_or_kpis_is_allowed():
    out = check(good(jd={"title": "Head Office Staff"}, kpis=[]))
    assert out["kpis"] == [] and out["jd"]["responsibilities"] == []


@pytest.mark.parametrize("weights,ok", [((60, 40), True), ((60, 35), False), ((60, 41), False), (("60.004", "39.998"), True)])
def test_responsibility_weights_must_total_100(weights, ok):
    resps = [{"title": "A", "weight_pct": weights[0]}, {"title": "B", "weight_pct": weights[1]}]
    payload = good(jd={"title": "T", "responsibilities": resps}, kpis=[])
    if ok:
        check(payload)
    else:
        assert any("must total 100" in e for e in errors_of(payload))


@pytest.mark.parametrize("w,ok", [((70, 30), True), ((70, 29), False), ((50, 50.005), True), ((101, -1), False)])
def test_kpi_weights_must_total_100(w, ok):
    payload = good()
    payload["kpis"][0]["weight"], payload["kpis"][1]["weight"] = w
    if ok:
        check(payload)
    else:
        assert errors_of(payload)


def test_error_message_names_the_total():
    payload = good()
    payload["kpis"][0]["weight"] = 65
    assert any("add up to 95" in e for e in errors_of(payload))


@pytest.mark.parametrize("bad", [True, "abc", None, -5, 101, float("nan"), [], {}])
def test_bad_kpi_weight_values_rejected(bad):
    payload = good()
    payload["kpis"][0]["weight"] = bad
    assert errors_of(payload)


@pytest.mark.parametrize("key", ["Bad Key", "1abc", "UPPER", "a", "has-dash", "x" * 61])
def test_bad_kpi_keys_rejected(key):
    payload = good()
    payload["kpis"][0]["key"] = key
    assert errors_of(payload)


def test_duplicate_kpi_keys_rejected():
    payload = good()
    payload["kpis"][0]["key"] = payload["kpis"][1]["key"] = "same"
    assert any("used twice" in e for e in errors_of(payload))


@pytest.mark.parametrize("field,value", [("unit", "pounds"), ("direction", "up"), ("source", "magic"), ("frequency", "daily")])
def test_enum_fields_checked(field, value):
    payload = good()
    payload["kpis"][0][field] = value
    assert any(field in e for e in errors_of(payload))


@pytest.mark.parametrize("cap,ok", [(None, True), (100, True), (300, True), (99, False), (301, False), ("x", False)])
def test_cap_range(cap, ok):
    payload = good()
    payload["kpis"][0]["cap_pct"] = cap
    if ok:
        check(payload)
    else:
        assert errors_of(payload)


def test_kpi_must_link_to_a_real_responsibility_if_it_names_one():
    payload = good()
    payload["kpis"][0]["jd_section"] = "Nonexistent"
    assert any("not one of the key responsibilities" in e for e in errors_of(payload))


@pytest.mark.parametrize(
    "chain,ok",
    [
        (["staff", "manager"], True), (["staff", "manager", "csm", "rm_gm"], True), (["staff", "manager", "rm_gm", "hr"], True),
        (["manager", "staff"], False), (["staff"], False), ([], False), (["staff", "manager", "ceo"], False),
        (["staff", "manager", "hr", "hr"], False), (["staff", "manager", "boss"], False), ("staff", False),
    ],
)
def test_signoff_chain_rules(chain, ok):
    payload = good(signoff_chain=chain)
    if ok:
        check(payload)
    else:
        assert errors_of(payload)


def test_ceo_step_is_appended_only_when_required():
    class V:  # effective_signoff_chain reads two attributes
        signoff_chain = ["staff", "manager", "hr"]
        requires_ceo_signoff = False

    assert pr.effective_signoff_chain(V) == ["staff", "manager", "hr"]
    V.requires_ceo_signoff = True
    assert pr.effective_signoff_chain(V) == ["staff", "manager", "hr", "ceo"]


def test_requires_ceo_must_be_boolean():
    assert errors_of(good(requires_ceo_signoff="yes"))


def test_jd_field_rules():
    assert any("Position title is required" in e for e in errors_of(good(jd={"title": ""}, kpis=[])))
    assert errors_of(good(jd={"title": "x" * 151}, kpis=[]))
    assert errors_of(good(jd="nope", kpis=[]))
    assert errors_of(good(jd={"title": "T", "purpose": 5}, kpis=[]))
    dup = {"title": "T", "responsibilities": [{"title": "Same", "weight_pct": 50}, {"title": "same", "weight_pct": 50}]}
    assert any("both called" in e for e in errors_of(good(jd=dup, kpis=[])))
    many = {"title": "T", "responsibilities": [{"title": f"R{i}", "weight_pct": 0} for i in range(16)]}
    assert errors_of(good(jd=many, kpis=[]))
    comps = {"title": "T", "competencies": [{"title": "Same"}, {"title": "Same"}]}
    assert any("repeated" in e for e in errors_of(good(jd=comps, kpis=[])))


def test_all_problems_are_reported_together():
    payload = good(jd={"title": ""}, signoff_chain=["staff"], requires_ceo_signoff=1)
    payload["kpis"][0]["unit"] = "pounds"
    assert len(errors_of(payload)) >= 4


def test_slugify():
    assert pr.slugify("Credit Evaluation & Underwriting!") == "credit_evaluation_underwriting"
