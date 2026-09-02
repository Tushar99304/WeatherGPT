"""
test_phase3_live.py — real end-to-end checks of validation + quality + advisory.

Marked `live` (needs internet). Assertions are INVARIANTS about how the layers must relate to
each other, so they hold whatever the sky and SACHET are doing today:

  * fresh, complete live data must validate and must not be labelled LOW without a reason;
  * an active, location-verified Severe/Extreme alert MUST produce risk HIGH, whatever the
    weather block says (alert priority is the product's core claim);
  * quality caps must be recorded, not silently applied;
  * deliberately stale data must fail validation and must abstain.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.live


def _ask(message: str):
    from backend.main import run_pipeline

    return asyncio.run(run_pipeline(message))


def test_live_fresh_evidence_validates_and_is_scored():
    ev, trace = _ask("What is the weather in Nagpur right now?")
    assert ev.status == "grounded", ev.abstain_reason
    assert ev.validation.ok and ev.validation.sufficient, ev.validation.failures
    assert ev.validation.fresh is True
    assert ev.evidence_quality in {"HIGH", "MEDIUM"}          # research source => not automatically HIGH
    assert ev.risk == ev.advisory.risk_level                  # one decision, two views
    assert ev.advisory.rules_fired, "the advisory must show which rule decided"
    assert "validate" in ev.validation.checks_run[-6:] or "validate" in [s["stage"] for s in trace["stages"]]
    # Phase 4 appended llm/grounding AFTER the three Phase-3 stages; the assertion stays exact —
    # it moved from "last 3" to "last 5" rather than becoming a subset test.
    assert [s["stage"] for s in trace["stages"]][-5:] == [
        "validate", "quality", "advise", "llm", "grounding",
    ]
    q = ev.quality_breakdown
    assert q["weights"] == {"authority": 40, "freshness": 30, "completeness": 20, "agreement": 10}
    assert 0 <= q["score"] <= 100
    # a live single-source answer must say agreement is not measurable rather than fake it
    assert any("only one comparable weather source" in n for n in q["notes"])


def test_alert_priority_rule_holds_on_whatever_sachet_published_today():
    ev, _ = _ask("Is there any weather alert for Mayurbhanj today?")
    severe_active = [
        a for a in ev.alerts.items
        if a.validity == "active" and a.relevance.status == "relevant"
        and (a.severity in {"Severe", "Extreme"} or a.urgency == "Immediate")
    ]
    if severe_active:
        assert ev.risk == "HIGH", f"active {severe_active[0].severity} alert did not raise the risk"
        assert "R1_active_severe_official_alert" in ev.advisory.rules_fired
        assert set(ev.advisory.alert_ids) <= {a.alert_id for a in ev.alerts.items}
        assert any(aid in {a.alert_id for a in severe_active} for aid in ev.advisory.alert_ids)
        assert "weather-related travel risk is high" in ev.advisory.headline.lower()
    else:
        # No Severe/Extreme (or Immediate) official alert is tied to this location. HIGH is still
        # legitimate, but ONLY when a strong deterministic weather hazard drives it via R3 — never
        # on an unverifiable or alert-based rationale. weather_hazards() returns
        # List[Tuple[str, bool, str]] = (label, is_strong, evidence_quote); the bool is index 1.
        from backend.services.advisory import weather_hazards

        hazards = weather_hazards(ev)
        strong_hazards = [h for h in hazards if h[1]]
        if ev.risk == "HIGH":
            assert strong_hazards, "HIGH with no severe official alert needs a strong weather hazard"
            assert "R3_weather_hazard_strong" in ev.advisory.rules_fired
        else:
            assert ev.risk in {"LOW", "MEDIUM", "UNCERTAIN"}
        # cited alert ids may only ever reference alerts actually attached to this evidence
        assert set(ev.advisory.alert_ids) <= {a.alert_id for a in ev.alerts.items}
    assert ev.evidence_quality in {"HIGH", "MEDIUM", "LOW"}


def test_quality_is_capped_when_the_alert_source_cannot_be_consulted(monkeypatch):
    from backend import config

    monkeypatch.setattr(config, "SIMULATE_ALERT_FAILURE", True)
    ev, _ = _ask("What is the weather in Pune right now?")
    assert ev.alert_state == "unavailable"
    assert ev.evidence_quality != "HIGH", "unverifiable alerts must cap quality below HIGH"
    assert any("rule 1" in c for c in ev.quality_breakdown["breakdown"]["caps_applied"])
    assert ev.risk != "LOW", "we must not call conditions low-risk while warnings are unverifiable"


def test_forced_stale_data_fails_validation_and_abstains(monkeypatch):
    from backend import config

    monkeypatch.setattr(config, "SIMULATE_STALE_DATA", True)
    ev, _ = _ask("What is the weather in Mumbai right now?")
    assert ev.validation.fresh is False, "the 6h-shifted provider timestamp must be caught"
    assert any("WEATHER_MAX_STALENESS_MIN" in f for f in ev.validation.failures)
    assert ev.evidence_quality == "LOW"
    assert ev.status == "abstain" and ev.risk == "UNCERTAIN"
    assert ev.weather is not None, "the numbers stay in the payload for transparency; they are just not trusted"
