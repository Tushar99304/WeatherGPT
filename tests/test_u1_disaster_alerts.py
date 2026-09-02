"""
test_u1_disaster_alerts.py — U1: disaster scenarios + official NDMA/SACHET alert UX.
Fully offline, NO network: real recorded SACHET fixtures, hand-built Evidence objects, and the
/api/query pipeline with the three network services replaced by deterministic stubs.

What U1 adds on top of Phases 1-5A (and what these tests pin down):
  * the CAP `instruction` field (already parsed in Phase 2) is surfaced verbatim and attributed —
    in the deterministic advisory factors and in the deterministic fallback sentence;
  * an active severe/immediate official alert keeps absolute precedence (R1), is cited by id,
    leads the answer, and cannot be silently omitted or contradicted by the LLM;
  * disaster-oriented scenarios (heavy rain/flood, thunderstorm, lightning, strong wind, fog)
    run through the EXISTING deterministic evidence/advisory pipeline — no new thresholds;
  * expired / uncertain-relevance / not-relevant alerts are never presented as active;
  * the frontend renders a prominent official-alert banner whose data comes only from evidence.

Run:  python -m pytest tests/test_u1_disaster_alerts.py -v
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import shutil
import subprocess
from typing import List, Optional

import pytest

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
from backend.services import advisory as ADV
from backend.services import alerts as A
from backend.services import grounding as G
from backend.services import llm as L
from backend.services import quality as Q
from backend.services import validation as V

REFS = pathlib.Path(__file__).resolve().parent.parent / "refs"
FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "index.html"

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 1, 2, 30, tzinfo=UTC)
FRESH_LOCAL = "2026-09-01T07:45"                # 15 min old against NOW -> inside the limit
PUNE_ALERT_ACTIVE_MID = dt.datetime(2026, 8, 28, 11, 30, tzinfo=UTC)   # inside the real window
PUNE_ALERT_AFTER_EXPIRY = dt.datetime(2026, 8, 28, 14, 0, tzinfo=UTC)  # after the real window

LOC = ResolvedLocation(
    name="Pune", latitude=18.51957, longitude=73.85535, country="India", country_code="IN",
    admin1="Maharashtra", admin2="Pune", timezone="Asia/Kolkata", utc_offset_seconds=19800,
)


# --------------------------------------------------------------------------- #
# builders (same shapes the phase suites use, kept self-contained)
# --------------------------------------------------------------------------- #
def bundle(*, temp: float = 25.8, precip: float = 0.0, wind: float = 12.4, code: int = 3,
           tomorrow_mm: Optional[float] = 6.0, tomorrow_code: int = 61) -> WeatherBundle:
    tomorrow = None
    if tomorrow_mm is not None:
        tomorrow = ForecastDay(
            date="2026-09-02", label="Tomorrow", is_forecast=True, temperature_max_c=29.0,
            temperature_min_c=22.0, precipitation_sum_mm=tomorrow_mm,
            precipitation_probability_max_pct=80.0, wind_speed_max_kmh=30.0,
            weather_code=tomorrow_code, condition="Light rain",
        )
    return WeatherBundle(
        provider="open-meteo", kind="live", requested_timeframe="now",
        retrieved_at_utc="2026-09-01T02:30:00Z", api_utc_offset_seconds=19800,
        current=CurrentWeather(
            time=FRESH_LOCAL, utc_offset_seconds=19800, temperature_c=temp,
            apparent_temperature_c=temp + 2, humidity_pct=88.0, precipitation_mm=precip,
            wind_speed_kmh=wind, weather_code=code,
            condition={95: "Thunderstorm", 99: "Thunderstorm with hail", 82: "Violent rain showers",
                       45: "Fog", 48: "Depositing fog"}.get(code, "Overcast"),
        ),
        today=ForecastDay(date="2026-09-01", label="Today", is_forecast=True,
                          temperature_max_c=30.0, temperature_min_c=23.0),
        tomorrow=tomorrow,
        request_url="https://api.open-meteo.com/v1/forecast?fake=1",
    )


def alert(*, severity: str = "Severe", alert_id: str = "A1", validity: str = "active",
          relevance: str = "relevant", instruction: Optional[str] = "Please follow SDMA guidelines.",
          urgency: str = "Expected", expires: Optional[str] = "2026-09-01T05:00:00Z") -> Alert:
    return Alert(
        alert_id=alert_id, source="NDMA SACHET", sender="Maharashtra-SDMA", event="Heavy Rain",
        headline="Heavy rain over Pune", description="Very heavy rain likely.",
        instruction=instruction, area_desc="Pune district of Maharashtra", severity=severity,
        urgency=urgency, certainty="Likely", effective_at="2026-09-01T02:00:00Z",
        expires_at=expires, validity=validity,
        validity_reason=("within the effective-to-expiry window" if validity == "active"
                         else "no expiry supplied by the source; not assumed active indefinitely"),
        relevance=AlertRelevance(status=relevance, level="L1_exact_locality",
                                 reason="areaDesc names this place (pune)", matched_terms=["pune"]),
    )


def alerts_block(*items: Alert, state: str = "checked", error: Optional[str] = None,
                 uncertain: int = 0, not_relevant: int = 0,
                 recent_expired: Optional[List[Alert]] = None) -> AlertsEvidence:
    return AlertsEvidence(
        state=state, mode="live", items=list(items), error=error,
        checked_at_utc="2026-09-01T02:29:00Z",
        feeds_considered=["https://sachet.ndma.gov.in/cap_public_website/rss/rss_maharashtra.xml"],
        rejected_uncertain=uncertain, rejected_not_relevant=not_relevant,
        recent_expired=list(recent_expired or []),
    )


def evidence(*, w: Optional[WeatherBundle] = None, alerts: Optional[AlertsEvidence] = None,
             timeframe: str = "now", intent: str = "forecast_current") -> Evidence:
    """A validated, scored, advised Evidence object — exactly what the answer layer receives."""
    ev = Evidence(
        status="grounded",
        request={"message": "test", "intent": intent, "timeframe": timeframe, "target_date": None},
        location=LOC,
        weather=w if w is not None else bundle(),
        alerts=alerts if alerts is not None else alerts_block(),
    )
    ev.sources.append(Source(name="Open-Meteo", type="forecast", timestamp=FRESH_LOCAL,
                             authority="research_repro", url="https://api.open-meteo.com/x"))
    if ev.alerts.state == "checked":
        ev.sources.append(Source(name="NDMA SACHET", type="official_alert",
                                 timestamp="2026-09-01T02:29:00Z", authority="official"))
    ev.validation = V.validate_evidence(ev, now=NOW)
    label, breakdown = Q.score_evidence(ev, ev.validation)
    ev.evidence_quality = label  # type: ignore[assignment]
    ev.quality_breakdown = {**ev.quality_breakdown, **breakdown}
    ev.advisory = ADV.advise(ev)
    ev.risk = ev.advisory.risk_level
    return ev


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    """Freeze the one wall clock validation touches so freshness is reproducible forever."""
    from backend.services import weather as W

    monkeypatch.setattr(W, "_utc_now", lambda: NOW)
    monkeypatch.setattr(L.config, "SIMULATE_LLM_FAILURE", False)
    monkeypatch.setattr(L.config, "SIMULATE_LLM_HALLUCINATION", False)


def reply(ev: Evidence, answer: str, **over):
    """What a well-behaved model would return (copied fields + one hand-written sentence)."""
    payload = {
        "answer": answer,
        "source": " + ".join(s.name for s in ev.sources) or "no usable source",
        "timestamp": ev.sources[0].timestamp if ev.sources else None,
        "risk": ev.advisory.risk_level if ev.advisory else "UNCERTAIN",
        "evidence_quality": ev.evidence_quality or "LOW",
    }
    payload.update(over)
    return {k: v for k, v in payload.items() if v not in (None, "")}


# =========================================================================== #
# 1. instruction preservation (parse -> normalize -> evidence), nothing invented
# =========================================================================== #
def test_01_cap_instruction_is_parsed_from_the_real_records():
    for name in ("cap_sample.xml", "cap_sample_marathi_pune.xml", "cap_files/1787913209058029.xml"):
        cap = A.parse_cap((REFS / name).read_text(encoding="utf-8"))
        assert cap["instruction"] == "Please follow SDMA guidelines.", name


def test_02_instruction_survives_normalization_verbatim():
    cap = A.parse_cap((REFS / "cap_files/1787913209058029.xml").read_text(encoding="utf-8"))
    alert_obj = A.normalize_alert(cap, now=PUNE_ALERT_ACTIVE_MID)
    assert alert_obj.instruction == "Please follow SDMA guidelines."
    assert alert_obj.validity == "active" and alert_obj.severity == "Moderate"


def test_03_instruction_is_never_invented_when_absent():
    """A CAP record with no <instruction> must surface None — the UI/answer may not patch a gap."""
    minimal = """<cap:alert xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
      <cap:identifier>IN-TEST-2</cap:identifier><cap:sent>2026-09-01T02:00:00+05:30</cap:sent>
      <cap:info><cap:headline>Only a headline</cap:headline>
        <cap:effective>2026-09-01T02:00:00+05:30</cap:effective>
        <cap:expires>2026-09-01T05:00:00+05:30</cap:expires></cap:info>
    </cap:alert>"""
    cap = A.parse_cap(minimal)
    assert cap["instruction"] is None
    alert_obj = A.normalize_alert(cap, now=NOW)
    assert alert_obj.instruction is None
    ev = evidence(alerts=alerts_block(alert(severity="Moderate", instruction=None)))
    payload = L.deterministic_payload(ev)
    assert "Official instruction" not in payload["answer"], "no invented instruction text"
    assert all("instruction" not in f for f in ev.advisory.factors), "no invented advisory factor"


# =========================================================================== #
# 2. official alert precedence (R1 intact) + instruction surfaced deterministically
# =========================================================================== #
def test_04_active_severe_alert_keeps_absolute_precedence_and_is_cited():
    ev = evidence(alerts=alerts_block(alert(severity="Severe")))
    assert ev.advisory.risk_level == "HIGH"
    assert "R1_active_severe_official_alert" in ev.advisory.rules_fired
    assert ev.advisory.alert_ids == ["A1"], "the advisory must cite the exact alert it rests on"
    ok, failures = V.advisory_references_ok(ev)
    assert ok and not failures
    # alert factor precedes any weather factor: the official alert owns the explanation
    assert ev.advisory.factors[0].startswith("official Severe Heavy Rain")


def test_05_immediate_urgency_alone_keeps_R1_high():
    ev = evidence(alerts=alerts_block(alert(severity="Moderate", urgency="Immediate")))
    assert ev.advisory.risk_level == "HIGH"
    assert "R1_active_severe_official_alert" in ev.advisory.rules_fired


def test_06_alert_priority_survives_bad_weather_data():
    """Stale weather must never bury a live official warning (the U1 safety order)."""
    stale = bundle()
    stale.current.time = "2026-09-01T01:00"          # 90 min old -> fails freshness
    ev = evidence(w=stale, alerts=alerts_block(alert(severity="Extreme")))
    assert ev.validation.fresh is False
    assert ev.advisory.risk_level == "HIGH"
    assert "active official" in ev.advisory.headline


def test_07_official_instruction_is_quoted_in_advisory_factors_verbatim():
    ev = evidence(alerts=alerts_block(alert(severity="Severe")))
    instr_factors = [f for f in ev.advisory.factors if f.startswith("official instruction")]
    assert len(instr_factors) == 1
    assert '"Please follow SDMA guidelines."' in instr_factors[0]
    assert "Maharashtra-SDMA" in instr_factors[0], "attribution must travel with the quote"


# =========================================================================== #
# 3. the deterministic fallback: alert first, instruction quoted, verify passes
# =========================================================================== #
def test_08_fallback_leads_with_the_alert_and_quotes_the_instruction():
    ev = evidence(alerts=alerts_block(alert(severity="Severe")))
    payload = L.deterministic_payload(ev)
    text = payload["answer"]
    assert text.startswith("An official Severe Heavy Rain alert is active"), text[:120]
    assert 'Official instruction from Maharashtra-SDMA: "Please follow SDMA guidelines."' in text
    rep = G.verify(ev, payload)
    assert rep.verified, rep.failures            # the fallback is held to the same verifier
    assert G.alert_mentioned(text, ev)


def test_09_fallback_instruction_quote_is_safe_against_quote_injection():
    """An official text containing double quotes must not break the verifier's quote scrubber."""
    a = alert(severity="Severe", instruction='Move to "higher ground" shelters now.')
    ev = evidence(alerts=alerts_block(a))
    payload = L.deterministic_payload(ev)
    assert "Move to 'higher ground' shelters now." in payload["answer"]
    rep = G.verify(ev, payload)
    assert rep.verified, rep.failures
    assert L._safe_quote('a "b"') == "a 'b'"


def test_10_unknown_validity_alert_is_not_relabelled_active():
    """validity == 'unknown' (no expiry published) must not be sold as an active alert."""
    ev = evidence(alerts=alerts_block(alert(severity="Moderate", validity="unknown")))
    assert ev.alerts.items[0].validity == "unknown"
    text = L.deterministic_payload(ev)["answer"]
    assert "is active for" not in text, text
    assert "does not prove it is active right now" in text
    rep = G.verify(ev, L.deterministic_payload(ev))
    assert rep.verified, rep.failures


# =========================================================================== #
# 4. boundaries: expired / uncertain / not-relevant are never presented as active
# =========================================================================== #
def test_11_expired_alert_is_never_presented_as_active_or_quoted():
    old = alert(severity="Extreme", validity="expired", alert_id="A-OLD",
                instruction="Do not go out.")
    ev = evidence(alerts=alerts_block(recent_expired=[old]))
    assert ev.alerts.items == []
    assert "R1_active_severe_official_alert" not in ev.advisory.rules_fired
    text = L.deterministic_payload(ev)["answer"]
    assert "Do not go out" not in text, "an expired instruction must not steer the answer"
    assert "No active official alert was verifiably tied" in text
    assert G.verify(ev, L.deterministic_payload(ev)).verified


def test_12_uncertain_relevance_alert_is_reported_not_attached():
    ev = evidence(alerts=alerts_block(uncertain=1))
    assert ev.advisory.risk_level == "UNCERTAIN"
    assert "R4_alert_relevance_uncertain" in ev.advisory.rules_fired
    text = L.deterministic_payload(ev)["answer"]
    assert "alert is active for" not in text
    assert G.verify(ev, L.deterministic_payload(ev)).verified


def test_13_not_relevant_alert_never_steers_the_answer():
    ev = evidence(alerts=alerts_block(not_relevant=2))
    assert ev.alerts.items == []
    assert ev.advisory.risk_level in {"LOW", "MEDIUM"}   # decided by weather only, no alert
    assert not any(r.startswith("R1_") or r.startswith("R2_") for r in ev.advisory.rules_fired)
    text = L.deterministic_payload(ev)["answer"]
    assert "alert is active for" not in text
    assert "No active official alert was verifiably tied" in text


# =========================================================================== #
# 5. disaster-oriented scenarios WITHOUT alerts run on the existing pipeline
# =========================================================================== #
@pytest.mark.parametrize("kwargs, level, rule, needle", [
    (dict(code=95), "MEDIUM", "R3_weather_hazard", "thunderstorm"),
    (dict(code=99), "HIGH", "R3_weather_hazard_strong", "severe thunderstorm with hail"),
    (dict(code=82), "HIGH", "R3_weather_hazard_strong", "violent rain showers"),
    (dict(precip=9.0), "MEDIUM", "R3_weather_hazard", "heavy rain right now"),
    (dict(wind=50.0), "MEDIUM", "R3_weather_hazard", "strong wind"),
    (dict(wind=90.0), "HIGH", "R3_weather_hazard_strong", "strong wind"),
    (dict(code=45), "MEDIUM", "R3_weather_hazard", "fog"),
    (dict(code=48), "HIGH", "R3_weather_hazard_strong", "depositing fog"),
])
def test_14_now_hazard_scenarios_without_alerts_still_work(kwargs, level, rule, needle):
    """Heavy rain, thunderstorms, lightning (hail codes), strong winds, fog — all from the
    EXISTING deterministic evidence pipeline. No new thresholds were added for U1."""
    ev = evidence(w=bundle(**kwargs), alerts=alerts_block())
    assert ev.advisory.risk_level == level, ev.advisory.factors
    assert rule in ev.advisory.rules_fired
    joined = " ".join(ev.advisory.factors)
    assert needle in joined
    assert all(not f.startswith("official") for f in ev.advisory.factors), "no phantom official alert"


@pytest.mark.parametrize("tomorrow_mm, level, rule", [
    (55.0, "MEDIUM", "R3_weather_hazard"),
    (140.0, "HIGH", "R3_weather_hazard_strong"),      # flood-primary level per THRESHOLDS
])
def test_15_flood_level_rain_day_scenario_without_alerts(tomorrow_mm, level, rule):
    ev = evidence(w=bundle(tomorrow_mm=tomorrow_mm), alerts=alerts_block(),
                  timeframe="tomorrow", intent="advisory_risk")
    assert ev.advisory.risk_level == level
    assert rule in ev.advisory.rules_fired
    assert any(f"{tomorrow_mm} mm total" in f for f in ev.advisory.factors)


def test_16_heat_scenario_reports_grounded_values_without_invented_thresholds():
    """Extreme heat: U1 adds NO heat threshold. The answer reports the measured temperature
    verbatim; risk comes only from rules that exist today (and any official heat alert)."""
    ev = evidence(w=bundle(temp=44.6), alerts=alerts_block())
    assert "44.6" in L.deterministic_payload(ev)["answer"]
    assert not any("heat" in f.lower() and "hazard" in f.lower() for f in ev.advisory.factors), \
        "no invented heat-hazard classification"
    # ...but an OFFICIAL heat-wave alert is still surfaced with full precedence:
    heat = alert(severity="Severe", alert_id="A-HEAT")
    heat.event, heat.headline = "Heat Wave", "Heat wave conditions over Pune"
    ev2 = evidence(alerts=alerts_block(heat))
    assert ev2.advisory.risk_level == "HIGH"
    assert "R1_active_severe_official_alert" in ev2.advisory.rules_fired


# =========================================================================== #
# 6. grounding invariants (U1 focus): the LLM cannot omit or contradict the alert
# =========================================================================== #
def test_17_llm_silence_about_an_active_alert_is_rejected():
    ev = evidence(alerts=alerts_block(alert(severity="Severe")))
    rep = G.verify(ev, reply(ev, "It is 25.8 °C right now, calm skies."))
    assert not rep.verified
    assert any("does not mention an alert" in f for f in rep.failures)


def test_18_llm_may_not_soften_severity_or_say_no_alert():
    ev = evidence(alerts=alerts_block(alert(severity="Severe")))
    soft = G.verify(ev, reply(ev, "A moderate Heavy Rain alert is active for Pune."))
    assert not soft.verified and any("severity" in f for f in soft.failures)
    denied = G.verify(ev, reply(ev, "There are no alerts for Pune right now."))
    assert not denied.verified


def test_19_llm_cannot_move_the_risk_the_alert_decided():
    ev = evidence(alerts=alerts_block(alert(severity="Extreme")))
    rep = G.verify(ev, reply(ev, "An Extreme Heavy Rain alert is active for Pune.", risk="LOW"))
    assert not rep.verified
    assert any("cannot move the risk level" in f for f in rep.failures)
    assert ev.advisory.risk_level == "HIGH"          # the decision itself is untouched


def test_20_instruction_free_invented_alert_ids_fail():
    ev = evidence(alerts=alerts_block(alert(severity="Severe", alert_id="IN-42")))
    rep = G.verify(ev, reply(ev, "Severe Heavy Rain alerts IN-42 and IN-999 are active."))
    assert not rep.verified and any("IN-999" in f for f in rep.failures)


def test_21_official_instruction_words_are_quotable_our_own_orders_are_not():
    """The authority's own words may be quoted; WE still may not issue orders or guarantees."""
    ev = evidence(alerts=alerts_block(alert(severity="Severe", instruction="Do not go out.")))
    quoted = L.deterministic_payload(ev)
    assert G.verify(ev, quoted).verified              # verbatim + attribution is admissible
    own_words = G.verify(ev, reply(ev, "Severe Heavy Rain alert active; it is safe to travel."))
    assert not own_words.verified                     # a safety guarantee remains forbidden


def test_22_fallback_payload_satisfies_every_u1_checklist_item():
    ev = evidence(alerts=alerts_block(alert(severity="Severe")))
    rep = G.verify(ev, L.deterministic_payload(ev))
    assert rep.verified, rep.failures
    for check in ("required_fields", "numbers(1)", "source_identity", "timestamp_is_as_of",
                  "alert_presence", "alert_ids_exist", "risk_matches_advisory",
                  "evidence_quality_matches", "current_vs_forecast", "insufficient_admitted",
                  "safety_wording", "numbers(2)", "numbers(3)"):
        if check.startswith("numbers("):
            continue                                # count is answer-dependent
        assert check in rep.checks_run, check


# =========================================================================== #
# 7. end-to-end pipeline (offline stubs): alert UX and hazard scenarios
# =========================================================================== #
def _patch_pipeline(monkeypatch, *, alerts, weather_bundle):
    from backend import main

    async def fake_resolve(text, context=None, **kw):
        return GeocodeResult(status="ok", query=text or "pune", location=LOC)

    class _P:
        async def fetch(self, lat, lon, **kw):
            return weather_bundle

    monkeypatch.setattr(main.geocoding, "resolve", fake_resolve)
    monkeypatch.setattr(main.weather, "get_provider", lambda: _P())
    monkeypatch.setattr(
        main.alerts_service, "check_alerts", lambda loc, **kw: _coro(alerts),
    )


async def _coro(x):
    return x


def test_23_pipeline_surfaces_active_alert_and_instruction_end_to_end(monkeypatch):
    from backend.main import run_pipeline

    _patch_pipeline(monkeypatch, alerts=alerts_block(alert(severity="Severe")),
                    weather_bundle=bundle())
    monkeypatch.setattr(L.config, "GROQ_API_KEY", "")   # deterministic fallback path
    ev, trace = asyncio.run(run_pipeline("Is there any weather alert for Pune today?"))
    # the pipeline shape phases 1-4 pinned is unchanged (stage list is a contract)
    assert [s["stage"] for s in trace["stages"]] == [
        "parse", "geocode", "retrieve_weather", "retrieve_alerts", "evidence",
        "validate", "quality", "advise", "llm", "grounding",
    ]
    assert ev.risk == "HIGH" and ev.alert_state == "checked"
    item = ev.alerts.items[0]
    assert item.instruction == "Please follow SDMA guidelines."
    # everything the U1 banner needs is present in the evidence (banner reads nothing else)
    assert item.validity == "active" and item.severity and item.area_desc and item.headline
    ans = trace["answer"]
    assert ans["alert_mentioned"] is True
    assert ans["text"].startswith("An official Severe Heavy Rain alert is active")
    assert 'Official instruction from Maharashtra-SDMA: "Please follow SDMA guidelines."' in ans["text"]
    assert ans["risk"] == ev.advisory.risk_level == "HIGH"
    assert "R1_active_severe_official_alert" in ev.advisory.rules_fired


def test_24_llm_silence_still_leaves_the_alert_front_and_center(monkeypatch):
    """Even if a model answer omits the official alert, the user-visible answer cannot:
    the reply is rejected and the deterministic sentence surfaces alert + instruction."""
    from backend import main
    from backend.services import http_client

    _patch_pipeline(monkeypatch, alerts=alerts_block(alert(severity="Severe")),
                    weather_bundle=bundle())
    monkeypatch.setattr(L.config, "GROQ_API_KEY", "gsk_test_key_never_used_offline")
    monkeypatch.setattr(L.config, "LLM_ENABLED", True)

    bad = {"answer": "It is 25.8 °C right now, pleasant.",
           "source": "Open-Meteo", "timestamp": FRESH_LOCAL,
           "risk": "HIGH", "evidence_quality": "HIGH"}

    async def fake_post_json(url, *, payload, **kw):
        return {"choices": [{"message": {"content": json.dumps(bad)}}], "model": payload.get("model")}

    monkeypatch.setattr(http_client, "post_json", fake_post_json)
    ev, trace = asyncio.run(main.run_pipeline("Is there any weather alert for Pune today?"))
    ans = trace["answer"]
    assert ans["origin"] == "deterministic_fallback", "the silent model answer must not be shown"
    assert any("does not mention an alert" in f for f in ans["grounding"]["failures"])
    assert "Severe Heavy Rain alert is active" in ans["text"]
    assert "Official instruction" in ans["text"]
    assert ans["risk"] == "HIGH"


def test_25_pipeline_thunderstorm_scenario_without_any_alert(monkeypatch):
    from backend.main import run_pipeline

    _patch_pipeline(monkeypatch, alerts=alerts_block(), weather_bundle=bundle(code=95))
    monkeypatch.setattr(L.config, "GROQ_API_KEY", "")
    ev, trace = asyncio.run(run_pipeline("Are thunderstorms expected in Pune right now?"))
    assert ev.alerts.items == [] and ev.alert_state == "checked"
    assert ev.risk == "MEDIUM" and "R3_weather_hazard" in ev.advisory.rules_fired
    assert any("thunderstorm" in f for f in ev.advisory.factors)
    ans = trace["answer"]
    assert ans["text"] and "alert is active for" not in ans["text"]
    assert ans["grounding"]["verified"] is True


def test_26_fixture_replay_instruction_flows_to_the_evidence(monkeypatch, tmp_path):
    """The real recorded Pune CAP (fixture replay, labelled) end-to-end through check_alerts:
    instruction preserved, relevance attached, alert active in its recorded window."""
    monkeypatch.setattr(A.config, "ALERT_FIXTURE_RSS", str(REFS / "rss_fixture_pune.xml"))
    monkeypatch.setattr(A.config, "ALERT_FIXTURE_CAP_DIR", str(REFS / "cap_files"))
    monkeypatch.setattr(A.config, "ALERT_INCLUDE_INDIA_FEED", False)

    class _NoNet:
        async def __call__(self, *a, **k):
            raise AssertionError("network must not be touched in fixture replay tests")

    monkeypatch.setattr(A, "get_text", _NoNet())
    res = asyncio.run(A.check_alerts(LOC, now=PUNE_ALERT_ACTIVE_MID))
    assert res.mode == "fixture_replay" and res.state == "checked"
    assert len(res.items) == 1
    item = res.items[0]
    assert item.instruction == "Please follow SDMA guidelines."
    assert item.validity == "active" and item.relevance.status == "relevant"
    assert item.headline and item.area_desc and item.severity and item.urgency
    # and judged after the window, the same record must leave the active set entirely
    res2 = asyncio.run(A.check_alerts(LOC, now=PUNE_ALERT_AFTER_EXPIRY))
    assert res2.items == [] and len(res2.recent_expired) == 1


# =========================================================================== #
# 8. frontend render contract (what the user actually sees)
# =========================================================================== #
def test_27_frontend_contains_the_u1_alert_presentation():
    """The page must carry the official-alert banner and instruction rendering, wired to
    evidence fields only. A change that silently drops the banner fails here."""
    html = FRONTEND.read_text(encoding="utf-8")
    for needle in (
        "renderOfficialBanner",
        "official NDMA / SACHET alert active",
        "Official instruction",
        "quoted verbatim from the CAP record",
        "What WeatherGPT recommends",
        'a.validity === "active"',                  # banner shows backend-decided validity only
    ):
        assert needle in html, needle
    # the banner is rendered BEFORE the status row (prominence is a code-order fact)
    assert html.index("renderOfficialBanner(activeItems)") < html.index("STATUS_PILL[ev.status]")
    # disaster scenario chips exist (they only fire queries at the existing pipeline)
    for scenario in ("heavy rain", "thunderstorm", "strong winds", "fog", "heat"):
        assert scenario in html.lower(), scenario


def test_28_node_render_check_script_passes():
    """The repo's offline render check (README §8) is part of the suite: it renders the real
    page script against synthetic evidence and asserts every U1 alert-UX invariant."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available for the render check")
    script = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "check_frontend_render.mjs"
    out = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr + out.stdout
    assert "RENDER CASES OK" in out.stdout


def test_29_frontend_script_is_syntactically_valid_javascript():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available for JS syntax check")
    import re

    script = re.search(r"<script>(.*?)</script>", FRONTEND.read_text(encoding="utf-8"), re.S)
    assert script, "inline script block missing"
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script.group(1))
        tmp = fh.name
    try:
        out = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
    finally:
        pathlib.Path(tmp).unlink(missing_ok=True)
    assert out.returncode == 0, out.stderr
