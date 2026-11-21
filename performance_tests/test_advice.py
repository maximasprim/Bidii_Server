"""Phase 4 - AI advice validation (pure; the provider is replaced)."""

import json

import pytest

from app.services import performance_advice as adv
from app.services import performance_ai as ai

PAYLOAD = {"subject": "Staff A", "score": 72.4, "achievement_pct": 68.0, "monthly_pct": [90, 70, 65], "rules": [{"title": "Hold a coaching conversation", "detail": "Below 70% for 2 months"}]}


def good(**over):
    base = {"summary": "Achievement is 68.0% and has slipped over three months.", "root_causes": ["Pipeline fell after January"],
            "actions": [{"action": "Hold a one-to-one this week", "owner": "manager", "timeframe": "within 5 days"}, {"action": "Agree a weekly pipeline review", "owner": "manager", "timeframe": "30 days"},
                        {"action": "Pair with a top performer", "owner": "hr", "timeframe": "this quarter"}], "training": ["Pipeline management"], "confidence": "medium"}
    return json.dumps({**base, **over})


def test_valid_answer_passes_and_timeframes_are_exempt():
    a = adv.validate(good(), PAYLOAD)
    assert len(a["actions"]) == 3 and a["confidence"] == "medium"


@pytest.mark.parametrize("bad", [
    good(summary="Achievement is 83% which is fine"),  # a number that was not supplied
    good(root_causes=["Sales fell by 15 points"]),
    good(actions=[{"action": "Do x", "owner": "manager", "timeframe": ""}] * 2),  # too few
    good(actions=[{"action": "Do x", "owner": "wizard", "timeframe": ""}] * 3),  # bad owner
    good(confidence="certain"), good(summary=""), "not json", "[]", json.dumps({"summary": "x"}),
])
def test_invalid_answers_are_rejected(bad):
    with pytest.raises(ai.AIOutputInvalid):
        adv.validate(bad, PAYLOAD)


def test_numbers_that_were_supplied_are_allowed():
    adv.validate(good(summary="Score 72.4 against 68.0 and 70, with 2 months below"), PAYLOAD)


def test_fallback_has_no_ai_label():
    f = adv.fallback("T", "D")
    assert f["source"] == "rules" and "no AI" in f["label"] and f["actions"] == []


def test_advise_falls_back_on_every_failure(db, monkeypatch):
    def boom(system, user):
        raise ai.AIUnavailable("none configured")

    monkeypatch.setattr(ai, "complete_json", boom)
    out = adv.advise(db, PAYLOAD, title="T", detail="D", username="u")
    assert out["source"] == "rules" and "none configured" in out["why_fallback"]
    monkeypatch.setattr(ai, "complete_json", lambda s, u: ("garbage", "p", "m"))
    assert adv.advise(db, PAYLOAD, title="T", detail="D", username="u")["source"] == "rules"
    monkeypatch.setattr(ai, "complete_json", lambda s, u: (good(summary="It is 99.9% bad"), "p", "m"))
    assert adv.advise(db, PAYLOAD, title="T", detail="D", username="u")["source"] == "rules"


def test_advise_caches_and_regenerates(db, monkeypatch):
    calls = []

    def f(s, u):
        calls.append(u)
        return good(), "fake", "fake-model"

    monkeypatch.setattr(ai, "complete_json", f)
    a = adv.advise(db, PAYLOAD, title="T", detail="D", username="u")
    b = adv.advise(db, PAYLOAD, title="T", detail="D", username="u")
    c = adv.advise(db, PAYLOAD, title="T", detail="D", username="u", regenerate=True)
    assert len(calls) == 2 and a["ai"]["cached"] is False and b["ai"]["cached"] is True and c["ai"]["cached"] is False
    assert a["label"] == adv.LABEL and a["ai"]["prompt_version"] == adv.PROMPT_VERSION
    assert "Staff A" in calls[0] and "Officer" not in calls[0]
