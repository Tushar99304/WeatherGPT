"""
test_phase4_units.py — the grounded LLM layer and its verifier. Fully offline, no network.

Phase 4's whole claim is "the model may phrase, never decide", so these tests are written as
adversarial cases against that claim: every test either (a) proves a specific violation is caught,
or (b) proves a specific failure of Groq still leaves the user with a grounded sentence. The HTTP
call is always replaced by a stub (`_stub_groq`), so the suite runs on a plane, without a key, and
never touches api.groq.com.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any, Dict, List, Optional

import pytest

from backend import config
from backend.models import (
    Alert,
    AlertRelevance,
    AlertsEvidence,
    CurrentWeather,
    Evidence,
    ForecastDay,
    GeocodeResult,
    ResolvedLocation,
    Source,
    WeatherBundle,
)
from backend.services import grounding as G
from backend.services import llm as L
from backend.services import http_client
from backend.services import validation as V

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 1, 2, 30, tzinfo=UTC)
FRESH_LOCAL = "2026-09-01T07:45"            # 15 min old against NOW
LOC = ResolvedLocation(
    name="Pune", latitude=18.51957, longitude=73.85535, country="India", country_code="IN",
    admin1="Maharashtra", admin2="Pune", timezone="Asia/Kolkata", utc_offset_seconds=19800,
)


# --------------------------------------------------------------------------- #
# fixtures / builders
# --------------------------------------------------------------------------- #
def bundle(
    *,
    temp: float = 25.8,
    prob_tomorrow: Optional[float] = 100.0,
    precip_now: float = 0.0,
    wind: float = 12.4,
    with_current: bool = True,
    kind: str = "live",
) -> WeatherBundle:
    cur = (
        CurrentWeather(
            time=FRESH_LOCAL, utc_offset_seconds=19800, temperature_c=temp,
            apparent_temperature_c=temp + 2.2, humidity_pct=88.0, precipitation_mm=precip_now,
            wind_speed_kmh=wind, weather_code=3, condition="Overcast",
        )
        if with_current
        else None
    )
    tomorrow = (
        ForecastDay(
            date="2026-09-02", label="Tomorrow", is_forecast=True, temperature_max_c=29.0,
            temperature_min_c=22.0, precipitation_sum_mm=6.0,
            precipitation_probability_max_pct=prob_tomorrow, wind_speed_max_kmh=30.0,
            weather_code=61, condition="Light rain",
        )
        if prob_tomorrow is not None
        else None
    )
    return WeatherBundle(
        provider="open-meteo", kind=kind, requested_timeframe="now",
        retrieved_at_utc="2026-09-01T02:30:00Z", api_utc_offset_seconds=19800, current=cur,
        tomorrow=tomorrow, request_url="https://api.open-meteo.com/v1/forecast?fake=1",
    )


def alert_item(*, severity: str = "Severe", alert_id: str = "IN-50", validity: str = "active",
               relevance: str = "relevant") -> Alert:
    return Alert(
        alert_id=alert_id, source="NDMA SACHET", sender="Maharashtra-SDMA", event="Heavy Rain",
        headline="Heavy rain over Pune", description="Very heavy rain likely.",
        instruction="Avoid waterlogged areas", area_desc="Pune district of Maharashtra",
        severity=severity, urgency="Immediate", certainty="Likely",
        effective_at="2026-09-01T02:00:00Z", expires_at="2026-09-01T05:00:00Z", validity=validity,
        relevance=AlertRelevance(status=relevance, level="L1_exact_locality",
                                 reason="areaDesc names this place", matched_terms=["pune"]),
    )


def alerts_block(*items: Alert, state: str = "checked", error: Optional[str] = None) -> AlertsEvidence:
    return AlertsEvidence(
        state=state, mode="live", items=list(items), error=error,
        checked_at_utc="2026-09-01T02:29:00Z",
        feeds_considered=["https://sachet.ndma.gov.in/cap_public_website/rss/rss_maharashtra.xml"],
    )


def evidence(
    *,
    w: Optional[WeatherBundle] = None,
    alerts: Optional[AlertsEvidence] = None,
    with_sources: bool = True,
    timeframe: str = "now",
    intent: str = "forecast_current",
    score: bool = True,
) -> Evidence:
    """A validated, scored, advised Evidence object — the exact shape the LLM is allowed to see."""
    from backend.services import advisory as ADV
    from backend.services import quality as Q

    ev = Evidence(
        status="grounded",
        request={"message": "test", "intent": intent, "timeframe": timeframe, "target_date": None},
        location=LOC,
        weather=bundle() if w is None else w,
        alerts=alerts if alerts is not None else alerts_block(),
    )
    if with_sources:
        if ev.weather is not None:
            ev.sources.append(
                Source(name="Open-Meteo", type="forecast", timestamp=FRESH_LOCAL,
                       authority="research_repro", url="https://api.open-meteo.com/x")
            )
        if ev.alerts.state == "checked":
            ev.sources.append(
                Source(name="NDMA SACHET", type="official_alert", timestamp="2026-09-01T02:29:00Z",
                       authority="official")
            )
    ev.validation = V.validate_evidence(ev, now=NOW)
    if score:
        label, breakdown = Q.score_evidence(ev, ev.validation)
        ev.evidence_quality = label  # type: ignore[assignment]
        ev.quality_breakdown = {**ev.quality_breakdown, **breakdown}
        ev.advisory = ADV.advise(ev)
        ev.risk = ev.advisory.risk_level
    return ev


def reply(ev: Evidence, answer: str, **over: Any) -> Dict[str, Any]:
    """What a well-behaved model would return: copied fields, one hand-written sentence."""
    payload = {
        "answer": answer,
        "source": " + ".join(s.name for s in ev.sources) or "no usable source",
        "timestamp": (ev.sources[0].timestamp if ev.sources else None),
        "risk": ev.advisory.risk_level if ev.advisory else "UNCERTAIN",
        "evidence_quality": ev.evidence_quality or "LOW",
    }
    payload.update(over)
    return {k: v for k, v in payload.items() if v not in (None, "")}


def verdict(ev: Evidence, answer: str, **over: Any) -> Any:
    return G.verify(ev, reply(ev, answer, **over))


def assert_ok(rep) -> None:
    assert rep.verified, rep.failures


def assert_fails(rep, needle: str) -> None:
    assert not rep.verified, "expected a grounding failure, got a pass"
    assert any(needle.lower() in f.lower() for f in rep.failures), rep.failures


class _Resp:
    def __init__(self, body: Any, status: int = 200):
        self._body, self.status_code = body, status
        self.text = json.dumps(body) if isinstance(body, (dict, list)) else str(body)

    def json(self) -> Any:
        if isinstance(self._body, str):
            raise ValueError("no json")
        return self._body


def _stub_groq(monkeypatch, replies: List[Any]) -> List[Dict[str, Any]]:
    """Replace the ONE shared HTTP client call. Each element is either a content string, a dict
    (treated as the assistant message content) or an Exception to raise."""
    calls: List[Dict[str, Any]] = []

    async def fake_post_json(url: str, *, payload: Dict[str, Any], **kw: Any) -> Dict[str, Any]:
        calls.append({"url": url, "payload": payload, **kw})
        step = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(step, Exception):
            raise step
        content = step if isinstance(step, str) else json.dumps(step)
        return {"choices": [{"message": {"content": content}}], "model": payload.get("model")}

    monkeypatch.setattr(http_client, "post_json", fake_post_json)
    monkeypatch.setattr(L.config, "GROQ_API_KEY", "gsk_test_key_never_used_offline")
    monkeypatch.setattr(L.config, "LLM_ENABLED", True)
    monkeypatch.setattr(L.config, "SIMULATE_LLM_FAILURE", False)
    monkeypatch.setattr(L.config, "SIMULATE_LLM_HALLUCINATION", False)
    return calls


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    from backend.services import weather as W

    monkeypatch.setattr(W, "_utc_now", lambda: NOW)
    monkeypatch.setattr(L.config, "SIMULATE_LLM_FAILURE", False)
    monkeypatch.setattr(L.config, "SIMULATE_LLM_HALLUCINATION", False)


# =========================================================================== #
# 1. the contract the model is given
# =========================================================================== #
def test_01_system_prompt_carries_every_hard_rule():
    p = L.SYSTEM_PROMPT.lower()
    for needle in (
        "every number", "no tools", "do not change the risk", "must mention", "alert_id",
        "not the same as", "not available", "is_forecast", "as of", "will not guess",
        "never guarantee personal safety", "json",
    ):
        assert needle in p, needle


def test_02_messages_are_exactly_system_plus_one_user_turn():
    ev = evidence()
    msgs = L.build_messages(ev)
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert len(msgs) == 2, "no history is allowed — the model gets one turn"


def test_03_user_turn_is_the_evidence_dump_and_nothing_else():
    ev = evidence()
    sent = json.loads(L.build_messages(ev)[1]["content"])
    assert set(sent) == set(ev.model_dump(mode="json"))
    assert sent["advisory"]["risk_level"] == ev.advisory.risk_level
    assert "tools" not in sent and "instructions" not in sent


def test_04_no_secret_travels_in_the_prompt():
    ev = evidence()
    blob = json.dumps(L.build_messages(ev))
    assert "gsk_" not in blob and "authorization" not in blob.lower()
    assert "Bearer" not in blob


def test_05_regeneration_carries_the_failures_not_the_previous_answer():
    ev = evidence()
    msgs = L.build_messages(ev, ["answer states 31.4 c, but no such temperature value exists"])
    assert [m["role"] for m in msgs] == ["system", "user"]      # still ONE user turn, replaced
    assert "31.4 c" in msgs[1]["content"]
    assert "rejected by the grounding verifier" in msgs[1]["content"]
    assert json.loads(msgs[1]["content"].split("\n\n")[0])["sources"]      # evidence still first


# =========================================================================== #
# 2. numbers
# =========================================================================== #
def test_06_number_in_evidence_passes_25_8():
    ev = evidence()
    assert_ok(verdict(ev, f"It is {25.8} °C in Pune right now."))


def test_07_invented_number_fails_31_4():
    ev = evidence()
    rep = verdict(ev, "It is 31.4 °C right now.")
    assert_fails(rep, "31.4")
    assert "31.4 °c" in rep.numbers_rejected or "31.4" in rep.numbers_rejected[0]
    assert rep.numbers_checked >= 1


def test_08_probability_100_passes_and_80_fails():
    ev = evidence()                                   # tomorrow carries 100 %
    assert_ok(verdict(ev, "Tomorrow the forecast shows a 100% chance of rain."))
    rep = verdict(ev, "Tomorrow the forecast shows an 80% chance of rain.")
    assert_fails(rep, "80")


def test_09_rounding_tolerance_is_0_1_and_no_looser():
    ev = evidence()                                   # current temp 25.8
    assert_ok(verdict(ev, "It is about 25.9 °C right now."))
    assert_fails(verdict(ev, "It is about 26.4 °C right now."), "26.4")


def test_10_dates_ids_and_offsets_are_not_measurements():
    ev = evidence(alerts=alerts_block(alert_item()))
    assert_ok(verdict(
        ev,
        "As of 2026-09-01T07:45 (UTC+05:30) the reading stood at 25.8 °C; the Severe alert IN-50 "
        "covers the next 3 hours on a 15-minute interval grid.",
    ))


def test_11_score_denominator_is_not_a_measurement():
    ev = evidence()
    assert_ok(verdict(ev, "It is 25.8 °C now. Evidence quality is HIGH (86/100)."))


def test_12_right_number_wrong_unit_fails():
    ev = evidence()
    # 25.8 is a temperature here; claiming it as rainfall is a fabrication of a different kind
    assert_fails(verdict(ev, "It rained 25.8 mm today."), "25.8")


def test_13_two_sentences_both_grounded_pass():
    ev = evidence()
    assert_ok(verdict(ev, "It is 25.8 °C with wind at 12.4 km/h right now."))


# =========================================================================== #
# 3. source and timestamp
# =========================================================================== #
def test_14_source_must_be_one_we_consulted():
    ev = evidence()
    assert_ok(verdict(ev, "It is 25.8 °C now.", source="Open-Meteo"))
    assert_fails(verdict(ev, "It is 25.8 °C now.", source="IMD"), "not in evidence.sources")


def test_15_multi_source_credit_is_allowed_and_limited_to_real_sources():
    ev = evidence()
    assert_ok(verdict(ev, "It is 25.8 °C now.", source="Open-Meteo + NDMA SACHET"))
    assert_ok(verdict(ev, "It is 25.8 °C now.", source="Open-Meteo and NDMA SACHET"))
    assert_fails(verdict(ev, "It is 25.8 °C now.", source="Open-Meteo + IMD"), "IMD")


def test_16_timestamp_is_the_as_of_not_the_forecast_day():
    ev = evidence()
    assert_ok(verdict(ev, "It is 25.8 °C now.", timestamp=FRESH_LOCAL))
    assert_ok(verdict(ev, "It is 25.8 °C now.", timestamp="2026-09-01T02:30:00Z"))   # retrieved_at
    assert_fails(verdict(ev, "It is 25.8 °C now.", timestamp="2026-09-02"), "covers")
    assert_fails(verdict(ev, "It is 25.8 °C now.", timestamp="2025-01-01T00:00"), "no timestamp")


def test_17_evidence_without_a_timestamp_may_not_invent_one():
    from backend.models import AlertsEvidence

    ev = evidence(with_sources=False, score=False, alerts=AlertsEvidence())
    ev.weather = None                                    # nothing retrieved, nothing to date
    assert ev.alerts.checked_at_utc is None
    assert G.allowed_timestamps(ev) == []
    assert_fails(
        G.verify(ev, {"answer": "It is 25.8 °C now, updated 2026-09-01T07:45.",
                      "risk": "UNCERTAIN", "evidence_quality": "LOW",
                      "timestamp": "2026-09-01T07:45"}),
        "no timestamp to quote",
    )


# =========================================================================== #
# 4. alerts
# =========================================================================== #
def test_18_active_alert_must_be_mentioned():
    ev = evidence(alerts=alerts_block(alert_item()))
    rep = verdict(ev, "It is 25.8 °C right now, pleasant weather.")
    assert_fails(rep, "does not mention an alert")
    assert_ok(verdict(ev, "A Severe Heavy Rain alert is active for Pune; it is 25.8 °C right now."))


def test_19_severity_may_not_be_softened():
    ev = evidence(alerts=alerts_block(alert_item(severity="Severe")))
    assert_fails(
        verdict(ev, "An official moderate Heavy Rain alert is active for Pune."), "severity"
    )


def test_20_alert_ids_must_exist_in_the_evidence():
    ev = evidence(alerts=alerts_block(alert_item(alert_id="IN-50")))
    assert_ok(verdict(ev, "Severe Heavy Rain alert IN-50 is active for Pune."))
    assert_fails(
        verdict(ev, "Severe Heavy Rain alerts IN-50 and IN-999 are active for Pune."), "IN-999"
    )


def test_21_unavailable_alerts_are_not_no_alerts():
    ev = evidence(alerts=alerts_block(state="unavailable", error="HTTP 503"))
    assert_fails(verdict(ev, "It is 25.8 °C right now and there are no alerts."), "not be consulted")
    assert_ok(verdict(
        ev,
        "It is 25.8 °C right now; the official alert service could not be verified at this time, "
        "so alerts for this location are unknown.",
    ))


def test_22_checked_and_empty_must_not_be_reported_as_absence():
    ev = evidence()
    assert_fails(verdict(ev, "It is 25.8 °C now and no alerts exist."), "must not be shortened")
    assert_ok(verdict(
        ev,
        "It is 25.8 °C now. No active official alert was verifiably tied to this location at the "
        "time of the check, which is not the same as no alert existing.",
    ))


# =========================================================================== #
# 5. risk and quality are copied, never decided
# =========================================================================== #
def test_23_risk_mismatch_fails_in_both_directions():
    ev = evidence(alerts=alerts_block())
    assert_fails(verdict(ev, "It is 25.8 °C now.", risk="HIGH"), "cannot move the risk level")
    ev2 = evidence(alerts=alerts_block(alert_item(severity="Extreme")))
    assert_fails(verdict(ev2, "An Extreme Heavy Rain alert is active for Pune.", risk="LOW"),
                  "cannot move the risk level")


def test_24_risk_is_matched_case_insensitively_but_not_value_insensitively():
    ev = evidence()
    assert_ok(verdict(ev, "It is 25.8 °C now.", risk=ev.advisory.risk_level.lower()))
    assert_fails(verdict(ev, "It is 25.8 °C now.", risk="SOMETHING_ELSE"), "cannot move the risk")


def test_25_quality_label_is_not_the_models_to_choose():
    ev = evidence()
    assert_fails(verdict(ev, "It is 25.8 °C now.", evidence_quality="LOW"), "not the model")


def test_26_verification_never_mutates_the_evidence():
    ev = evidence(alerts=alerts_block(alert_item()))
    before = ev.model_dump()
    G.verify(ev, {"answer": "It is 400 °C and no alerts exist.", "source": "IMD",
                  "timestamp": "2020-01-01", "risk": "LOW", "evidence_quality": "HIGH"})
    assert ev.model_dump() == before


def test_27_risk_and_quality_mismatch_is_a_failure_not_a_rewrite():
    """The invariant the brief calls out: a mismatch fails grounding, it never overwrites either
    field. After rejection, the evidence still says what the advisory said."""
    ev = evidence(alerts=alerts_block(alert_item(severity="Extreme")))
    decided = ev.advisory.risk_level
    rep = verdict(ev, "A Severe Heavy Rain alert is active for Pune.", risk="LOW")
    assert not rep.verified
    assert ev.advisory.risk_level == decided
    assert ev.risk == decided


# =========================================================================== #
# 6. labelling, abstention, safety wording, shape
# =========================================================================== #
def test_28_forecast_value_presented_as_current_fails():
    ev = evidence()
    assert_fails(verdict(ev, "Right now there is a 100% chance of rain."), "current observation")
    assert_ok(verdict(ev, "Tomorrow the forecast shows a 100% chance of rain."))


def test_29_insufficient_evidence_must_be_admitted():
    ev = evidence()
    ev.validation.sufficient = False
    ev.validation.failures.append("freshness: provider timestamp is 360 min old")
    assert_fails(verdict(ev, "It is 25.8 °C and perfectly fine."), "could not be verified")
    assert_ok(verdict(ev, "I could not verify reliable weather information, so I won't guess."))
    assert "evidence_not_sufficient_note" in verdict(
        ev, "I could not verify reliable weather information."
    ).checks_run


def test_30_safety_wording_is_rejected():
    ev = evidence()
    rep = verdict(ev, "It is 25.8 °C now, so it is safe to travel and you need not evacuate.")
    assert not rep.verified, rep.failures


def test_31_missing_required_keys_are_reported_not_guessed():
    ev = evidence()
    rep = G.verify(ev, {"answer": "It is 25.8 °C now."})
    assert not rep.verified
    assert any("required field" in f for f in rep.failures)
    assert G.verify(ev, "not a dict at all").failures[0] == "response is not a JSON object"


# =========================================================================== #
# 7. explain(): every failure mode ends in a grounded answer
# =========================================================================== #
def test_32_no_key_uses_the_fallback_and_still_verifies(monkeypatch):
    monkeypatch.setattr(L.config, "GROQ_API_KEY", "")
    ev = evidence()
    calls = _stub_groq(monkeypatch, [])
    monkeypatch.setattr(L.config, "GROQ_API_KEY", "")
    ans, rep = asyncio.run(L.explain(ev))
    assert ans.origin == "deterministic_fallback" and rep.llm_status == "no_key"
    assert rep.verified and ans.text and calls == []
    assert ans.risk == ev.advisory.risk_level and ans.evidence_quality == ev.evidence_quality


def test_33_disabled_llm_is_not_called_at_all(monkeypatch):
    calls = _stub_groq(monkeypatch, [{"answer": "hi"}])
    monkeypatch.setattr(L.config, "LLM_ENABLED", False)
    ans, rep = asyncio.run(L.explain(evidence()))
    assert rep.llm_status == "disabled" and calls == []
    assert ans.origin == "deterministic_fallback"


def test_34_upstream_error_falls_back_without_retrying(monkeypatch):
    calls = _stub_groq(
        monkeypatch, [http_client.UpstreamError("groq", "timeout after 30s (ConnectTimeout)")]
    )
    ans, rep = asyncio.run(L.explain(evidence()))
    assert len(calls) == 1, "a timeout must not be doubled by a transport retry"
    assert rep.llm_status == "upstream_error" and ans.origin == "deterministic_fallback"
    assert "timeout" in rep.failures[0]
    assert rep.verified is True or ans.text          # the user still gets a grounded sentence


def test_35_malformed_json_falls_back(monkeypatch):
    calls = _stub_groq(monkeypatch, ["I think the weather is nice today, honestly."])
    ans, rep = asyncio.run(L.explain(evidence()))
    assert rep.llm_status == "malformed_json"
    assert ans.origin == "deterministic_fallback"
    assert len(calls) == config.LLM_MAX_ATTEMPTS


def test_36_good_json_is_accepted_and_the_request_is_exactly_specified(monkeypatch):
    ev = evidence()
    calls = _stub_groq(monkeypatch, [{
        "answer": "It is 25.8 °C right now in Pune, with wind at 12.4 km/h.",
        "source": "Open-Meteo + NDMA SACHET",
        "timestamp": FRESH_LOCAL,
        "risk": ev.advisory.risk_level,
        "evidence_quality": ev.evidence_quality,
    }])
    ans, rep = asyncio.run(L.explain(ev))
    assert ans.origin == "groq_llm" and rep.verified and rep.llm_status == "ok"
    req = calls[0]
    assert req["url"] == "https://api.groq.com/openai/v1/chat/completions"
    body = req["payload"]
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["temperature"] == 0.0
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == config.LLM_MAX_TOKENS
    assert body["include_reasoning"] is False
    assert body["reasoning_effort"] == "low"
    assert "tools" not in body and "functions" not in body
    assert req["headers"]["Authorization"] == "Bearer gsk_test_key_never_used_offline"
    assert req["timeout"] == config.LLM_TIMEOUT_S
    assert req["retries"] == 0, "the LLM call must not be retried at the transport layer"
    # the ONLY data the model saw is the evidence object
    assert json.loads(body["messages"][1]["content"]).keys() == ev.model_dump(mode="json").keys()


def test_37_one_regeneration_then_success(monkeypatch):
    ev = evidence()
    good = {
        "answer": "It is 25.8 °C right now.",
        "source": "Open-Meteo + NDMA SACHET",
        "timestamp": FRESH_LOCAL,
        "risk": ev.advisory.risk_level,
        "evidence_quality": ev.evidence_quality,
    }
    calls = _stub_groq(monkeypatch, [{
        "answer": "It is 31.4 °C right now.", "source": "Open-Meteo + NDMA SACHET",
        "timestamp": FRESH_LOCAL, "risk": ev.advisory.risk_level,
        "evidence_quality": ev.evidence_quality,
    }, good])
    ans, rep = asyncio.run(L.explain(ev))
    assert len(calls) == 2, "exactly one regeneration"
    assert ans.origin == "groq_llm" and rep.verified and rep.regenerated and rep.attempts == 2
    second = json.loads(json.dumps(calls[1]["payload"]["messages"][1]["content"]))
    assert "no such temperature value exists" in second or "31.4" in second


def test_38_two_failures_then_deterministic_fallback(monkeypatch):
    ev = evidence()
    bad = {
        "answer": "It is 31.4 °C now and an 80% chance of rain.",
        "source": "Open-Meteo + NDMA SACHET", "timestamp": FRESH_LOCAL,
        "risk": ev.advisory.risk_level, "evidence_quality": ev.evidence_quality,
    }
    calls = _stub_groq(monkeypatch, [bad])
    ans, rep = asyncio.run(L.explain(ev))
    assert len(calls) == config.LLM_MAX_ATTEMPTS
    assert ans.origin == "deterministic_fallback" and rep.llm_status == "grounding_failed"
    assert rep.regenerated and any("31.4" in f for f in rep.failures)
    assert "31.4" not in ans.text and "25.8" in ans.text
    assert any("[rejected model reply]" in f and "31.4" in f for f in rep.failures), rep.failures
    assert ans.grounding is not None and ans.grounding.attempts == config.LLM_MAX_ATTEMPTS


def test_39_risk_change_by_the_model_is_rejected_then_replaced(monkeypatch):
    ev = evidence(alerts=alerts_block(alert_item(severity="Extreme")))
    decided = ev.advisory.risk_level
    calls = _stub_groq(monkeypatch, [{
        "answer": "A Severe Heavy Rain alert is active for Pune; it is 25.8 °C now.",
        "source": "Open-Meteo + NDMA SACHET", "timestamp": FRESH_LOCAL,
        "risk": "LOW", "evidence_quality": ev.evidence_quality,
    }])
    ans, rep = asyncio.run(L.explain(ev))
    assert len(calls) == config.LLM_MAX_ATTEMPTS
    assert rep.llm_status == "grounding_failed" and any("risk" in f for f in rep.failures)
    assert ans.risk == decided                       # the decision the model tried to move
    assert "Severe" in ans.text or "alert" in ans.text.lower()


def test_40_alert_omission_is_rejected_and_the_fallback_mentions_it(monkeypatch):
    ev = evidence(alerts=alerts_block(alert_item()))
    calls = _stub_groq(monkeypatch, [{
        "answer": "It is 25.8 °C right now and the weather is calm.",
        "source": "Open-Meteo", "timestamp": FRESH_LOCAL,
        "risk": ev.advisory.risk_level, "evidence_quality": ev.evidence_quality,
    }])
    ans, rep = asyncio.run(L.explain(ev))
    assert len(calls) == config.LLM_MAX_ATTEMPTS, "silence about an alert gets one chance to be fixed"
    assert rep.llm_status == "grounding_failed"
    assert any("does not mention an alert" in f for f in rep.failures)
    assert ans.origin == "deterministic_fallback"
    assert ans.alert_mentioned and "Severe" in ans.text


def test_41_hallucination_switch_is_caught_and_never_displayed(monkeypatch):
    ev = evidence()
    calls = _stub_groq(monkeypatch, [{
        "answer": "It is 25.8 °C right now.", "source": "Open-Meteo", "timestamp": FRESH_LOCAL,
        "risk": ev.advisory.risk_level, "evidence_quality": ev.evidence_quality,
    }])
    monkeypatch.setattr(L.config, "SIMULATE_LLM_HALLUCINATION", True)
    ans, rep = asyncio.run(L.explain(ev))
    assert len(calls) == config.LLM_MAX_ATTEMPTS
    assert ans.origin == "deterministic_fallback"
    for bad in ("987.6", "12345", "IMD"):
        assert bad not in ans.text, f"{bad} reached the user"
    assert any("987.6" in f for f in rep.failures), "the verifier must name what it rejected"


def test_42_unverified_evidence_never_reaches_the_model(monkeypatch):
    ev = evidence()
    ev.validation.sufficient = False
    ev.validation.failures.append("freshness: provider timestamp is 360 min old")
    ev.status = "abstain"
    calls = _stub_groq(monkeypatch, [{"answer": "It is 25.8 °C, definitely fine."}])
    ans, rep = asyncio.run(L.explain(ev))
    assert calls == [], "the LLM must not be asked to dress up unverified evidence"
    assert rep.llm_status == "skipped" and ans.origin == "deterministic_fallback"
    assert "could not verify" in ans.text and "25.8" not in ans.text


def test_43_latency_and_model_are_recorded(monkeypatch):
    ev = evidence()
    calls = _stub_groq(monkeypatch, [{
        "answer": "It is 25.8 °C right now.", "source": "Open-Meteo", "timestamp": FRESH_LOCAL,
        "risk": ev.advisory.risk_level, "evidence_quality": ev.evidence_quality,
    }])
    _ans, rep = asyncio.run(L.explain(ev))
    assert len(calls) == 1, "an accepted answer must cost exactly one request"
    assert rep.model == config.GROQ_MODEL and rep.latency_ms is not None and rep.latency_ms >= 0
    _ans2, rep2 = asyncio.run(L.explain(evidence()))
    monkeypatch.setattr(L.config, "GROQ_API_KEY", "")
    _ans3, rep3 = asyncio.run(L.explain(evidence()))
    assert rep3.model is None and rep3.llm_status == "no_key"


# =========================================================================== #
# 8. the deterministic fallback is held to the same standard
# =========================================================================== #
@pytest.mark.parametrize("kwargs", [
    {},
    {"alerts": alerts_block(alert_item())},
    {"alerts": alerts_block(state="unavailable", error="HTTP 503")},
    {"alerts": alerts_block(), "timeframe": "tomorrow", "intent": "forecast_rain",
     "w": bundle(temp=25.8, prob_tomorrow=100.0)},
    {"w": bundle(with_current=False, prob_tomorrow=None), "timeframe": "past",
     "intent": "past_conditions", "kind": "historical"},
])
def test_44_fallback_payload_passes_the_same_verifier(kwargs: Dict[str, Any]) -> None:
    if kwargs.get("w") is None and kwargs.get("kind"):
        kwargs = dict(kwargs)
        kwargs["w"] = bundle(with_current=False, prob_tomorrow=None)
    ev = evidence(**{k: v for k, v in kwargs.items() if k != "kind"})
    payload = L.deterministic_payload(ev)
    rep = G.verify(ev, payload)
    assert rep.verified, (ev.evidence_quality, ev.validation.failures, rep.failures)
    assert payload["risk"] == (ev.advisory.risk_level if ev.advisory else "UNCERTAIN")


def test_45_fallback_never_invents_a_value_when_a_block_is_missing():
    ev = evidence(w=bundle(with_current=False, prob_tomorrow=None), score=False)
    ev.validation = V.validate_evidence(ev, now=NOW)
    payload = L.deterministic_payload(ev)
    assert any(ch.isdigit() for ch in payload["answer"]) is not None
    assert "no usable values" in payload["answer"] or "no numeric values" in payload["answer"] \
        or "could not verify" in payload["answer"]


def test_46_fallback_is_the_same_object_twice(monkeypatch):
    monkeypatch.setattr(L.config, "GROQ_API_KEY", "")
    ev = evidence()
    a1, r1 = asyncio.run(L.explain(ev))
    a2, r2 = asyncio.run(L.explain(ev))
    # latency is the only field allowed to differ between two runs of the same code
    assert a1.model_dump(exclude={"grounding"}) == a2.model_dump(exclude={"grounding"})
    assert r1.model_dump(exclude={"latency_ms"}) == r2.model_dump(exclude={"latency_ms"})


# =========================================================================== #
# 9. wiring: stages, response shape, /health, transport
# =========================================================================== #
def _patch_pipeline_offline(monkeypatch, alerts: Optional[AlertsEvidence] = None):
    from backend import main

    async def fake_resolve(text, context=None, **kw):
        return GeocodeResult(status="ok", query=text or "pune", location=LOC)

    class _P:
        async def fetch(self, lat, lon, **kw):
            return bundle()

    monkeypatch.setattr(main.geocoding, "resolve", fake_resolve)
    monkeypatch.setattr(main.weather, "get_provider", lambda: _P())
    monkeypatch.setattr(
        main.alerts_service, "check_alerts",
        lambda loc, **kw: _coro(alerts if alerts is not None else alerts_block()),
    )


async def _coro(x):
    return x


def test_47_pipeline_appends_llm_and_grounding_stages(monkeypatch):
    from backend.main import run_pipeline

    _patch_pipeline_offline(monkeypatch)
    monkeypatch.setattr(L.config, "GROQ_API_KEY", "")
    ev, trace = asyncio.run(run_pipeline("What is the weather in Pune right now?"))
    stages = [s["stage"] for s in trace["stages"]]
    assert stages == ["parse", "geocode", "retrieve_weather", "retrieve_alerts", "evidence",
                      "validate", "quality", "advise", "llm", "grounding"]
    llm_stage = next(s for s in trace["stages"] if s["stage"] == "llm")
    grounding_stage = next(s for s in trace["stages"] if s["stage"] == "grounding")
    assert llm_stage["status"] in {"ok", "skipped", "fallback", "failed"}
    assert llm_stage["provider"] == "groq" and "GROQ_API_KEY" in llm_stage["note"]
    assert grounding_stage["status"] in {"ok", "failed"}
    assert grounding_stage["verified"] is True
    assert len(grounding_stage["checks_run"]) >= 11
    answer = trace["answer"]
    assert answer["origin"] == "deterministic_fallback" and answer["text"]
    assert answer["risk"] == ev.advisory.risk_level
    assert answer["evidence_quality"] == ev.evidence_quality
    # the answer is a projection of the evidence, not a new authority
    assert ev.model_dump()["advisory"]["risk_level"] == answer["risk"]


def test_48_query_endpoint_returns_the_grounded_answer(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.main import app

    _patch_pipeline_offline(monkeypatch)
    monkeypatch.setattr(L.config, "GROQ_API_KEY", "")
    with TestClient(app) as client:
        body = client.post("/api/query", json={"message": "weather in Pune now"}).json()
    assert body["status"] in {"grounded", "abstain"}
    ans = body["answer"]
    assert ans and ans["grounding"]["llm_status"] == "no_key"
    assert ans["grounding"]["verified"] is True
    assert body["evidence"]["advisory"]["risk_level"] == ans["risk"]
    assert "answer" not in body["pipeline"] or body["pipeline"]["answer"] == ans


def test_49_health_exposes_llm_config_without_the_key(monkeypatch):
    from fastapi.testclient import TestClient

    from backend import main

    monkeypatch.setattr(main.config, "GROQ_API_KEY", "gsk_secret_value")
    body = TestClient(main.app).get("/health").json()
    assert body["llm"] == {
        "configured": True, "provider": "groq", "model": config.GROQ_MODEL,
    }
    assert "gsk_secret_value" not in json.dumps(body)
    assert set(body["llm"]) == {"configured", "provider", "model"}


def test_50_post_json_raises_without_echoing_the_body_or_retrying(monkeypatch):
    seen: List[Any] = []

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen.append((url, json, headers))
            return _Resp("upstream exploded: rate limit", status=429)

    monkeypatch.setattr(http_client.httpx, "AsyncClient", _Client)
    with pytest.raises(http_client.UpstreamError) as exc:
        asyncio.run(http_client.post_json(
            "https://api.groq.com/openai/v1/chat/completions",
            payload={"messages": [{"role": "system", "content": "secret prompt"}]},
            headers={"Authorization": "Bearer gsk_x"}, service="groq",
        ))
    assert len(seen) == 1, "retries=0 means exactly one request"
    assert "429" in str(exc.value)
    assert "secret prompt" not in str(exc.value) and "gsk_x" not in str(exc.value)


def test_51_pipeline_survives_a_groq_outage_end_to_end(monkeypatch):
    """The product-level promise: with Groq unreachable the endpoint still answers."""
    from backend.main import run_pipeline

    _patch_pipeline_offline(monkeypatch)
    calls = _stub_groq(monkeypatch, [http_client.UpstreamError("groq", "HTTP 503")])
    ev, trace = asyncio.run(run_pipeline("weather in pune now"))
    llm_stage = next(s for s in trace["stages"] if s["stage"] == "llm")
    assert calls and llm_stage["status"] == "fallback"
    assert ev.status == "grounded" and trace["answer"]["origin"] == "deterministic_fallback"
    assert next(s for s in trace["stages"] if s["stage"] == "grounding")["status"] == "ok"


def test_52_checks_are_all_run_even_for_a_perfect_answer():
    ev = evidence()
    rep = verdict(ev, "It is 25.8 °C right now.", timestamp=FRESH_LOCAL)
    expected = {
        "required_fields", "source_identity", "timestamp_is_as_of", "alert_presence",
        "alert_ids_exist", "risk_matches_advisory", "evidence_quality_matches",
        "current_vs_forecast", "insufficient_admitted", "safety_wording",
    }
    assert expected <= set(rep.checks_run), rep.checks_run
    assert any(c.startswith("numbers(") for c in rep.checks_run)
