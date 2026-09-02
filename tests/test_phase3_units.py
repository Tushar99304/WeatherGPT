"""
test_phase3_units.py — validation, Evidence Quality and advisory. Fully deterministic, NO network.

Two kinds of tests live here:
  * pure-function tests over hand-built Evidence objects with fixed timestamps and a fixed `now`;
  * offline pipeline tests that monkeypatch geocoding/weather/alerts so `/api/query`'s real
    Phase-3 stages (validate -> quality -> advise) are exercised without internet.
Everything the demo laptop could lack (network) is stubbed; nothing here sleeps or retries.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

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
from backend.services import advisory as ADV
from backend.services import quality as Q
from backend.services import validation as V

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 1, 2, 30, tzinfo=UTC)          # fixed "now" for every test
FRESH_LOCAL = "2026-09-01T07:45"                            # 02:15Z -> 15 min old, inside the limit
STALE_LOCAL = "2026-09-01T01:00"                            # 195 min old -> far past the limit

LOC = ResolvedLocation(
    name="Pune", latitude=18.51957, longitude=73.85535, country="India", country_code="IN",
    admin1="Maharashtra", admin2="Pune", timezone="Asia/Kolkata", utc_offset_seconds=19800,
)


def bundle(*, time_str: Optional[str] = FRESH_LOCAL, temp=26.0, precip=0.2, wind=12.0, code=3,
           tomorrow_mm: Optional[float] = 6.0, kind="live") -> WeatherBundle:
    cur = None
    if time_str is not None:
        cur = CurrentWeather(
            time=time_str, utc_offset_seconds=19800, temperature_c=temp,
            apparent_temperature_c=None if temp is None else temp + 2, humidity_pct=88.0, precipitation_mm=precip,
            wind_speed_kmh=wind, weather_code=code, condition="Overcast" if code == 3 else "Rain",
        )
    tomorrow = None
    if tomorrow_mm is not None:
        tomorrow = ForecastDay(
            date="2026-09-02", label="Tomorrow", is_forecast=True, temperature_max_c=29.0,
            temperature_min_c=22.0, precipitation_sum_mm=tomorrow_mm,
            precipitation_probability_max_pct=80.0, wind_speed_max_kmh=30.0, weather_code=61,
            condition="Light rain",
        )
    return WeatherBundle(
        provider="open-meteo", kind=kind, requested_timeframe="now",
        retrieved_at_utc="2026-09-01T02:30:00Z", api_utc_offset_seconds=19800,
        current=cur, today=ForecastDay(date="2026-09-01", label="Today", is_forecast=True,
                                       temperature_max_c=30.0, temperature_min_c=23.0,
                                       precipitation_sum_mm=12.0, precipitation_probability_max_pct=60.0),
        tomorrow=tomorrow, request_url="https://api.open-meteo.com/v1/forecast?fake=1",
    )


def evidence(*, w: Optional[WeatherBundle] = None, alerts: Optional[AlertsEvidence] = None,
             loc: Optional[ResolvedLocation] = LOC, intent="forecast_current",
             timeframe="now", with_sources=True) -> Evidence:
    ev = Evidence(
        status="grounded",
        request={"message": "test", "intent": intent, "timeframe": timeframe, "target_date": None},
        location=loc,
        weather=bundle() if w is None and intent != "official_alert" else w,
        alerts=alerts if alerts is not None else AlertsEvidence(state="not_checked", mode="not_run"),
    )
    if with_sources:
        if ev.weather is not None:
            ev.sources.append(Source(name="Open-Meteo", type="forecast", timestamp=FRESH_LOCAL,
                                     authority="research_repro", url="https://api.open-meteo.com/x"))
        if ev.alerts.state == "checked":
            ev.sources.append(Source(name="NDMA SACHET", type="official_alert",
                                     timestamp="2026-09-01T02:00:00Z", authority="official"))
    return ev


def checked_alerts(*items, uncertain=0, not_rel=0, state="checked", error=None) -> AlertsEvidence:
    return AlertsEvidence(
        state=state, mode="live", items=list(items), error=error, checked_at_utc="2026-09-01T02:29:00Z",
        feeds_considered=["https://sachet.ndma.gov.in/cap_public_website/rss/rss_maharashtra.xml"],
        rejected_uncertain=uncertain, rejected_not_relevant=not_rel,
        notes=["1 feed item(s) in window; 1 CAP detail record(s) fetched"],
    )


def alert(*, severity="Severe", validity="active", relevance="relevant",
          level="L1_exact_locality", source="NDMA SACHET",
          expires="2026-09-01T05:00:00Z", effective="2026-09-01T02:00:00Z") -> Alert:
    return Alert(
        alert_id="A1", source=source,
        sender="Maharashtra-SDMA", event="Heavy Rain", headline="Heavy rain over Pune",
        area_desc="Pune district of Maharashtra", severity=severity, urgency="Expected",
        certainty="Likely", effective_at=effective, expires_at=expires, validity=validity,
        relevance=AlertRelevance(status=relevance, level=level,
                                 reason="areaDesc names this place (pune)", matched_terms=["pune"]),
    )


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    """Freeze the one wall clock validation touches so freshness is reproducible forever."""
    from backend.services import weather as W

    monkeypatch.setattr(W, "_utc_now", lambda: NOW)


# =============================== 1. VALIDATION =============================== #
def test_01_valid_current_weather_passes():
    v = V.validate_evidence(evidence(), now=NOW)
    assert v.ok and v.sufficient, v.failures
    assert v.location_resolved and v.fresh and v.values_plausible and v.complete
    assert v.labeling_consistent
    assert "location_sanity" in v.checks_run and "freshness" in v.checks_run


def test_02_stale_weather_fails_freshness():
    v = V.validate_evidence(evidence(w=bundle(time_str=STALE_LOCAL)), now=NOW)
    assert v.fresh is False
    assert not v.sufficient
    assert any("WEATHER_MAX_STALENESS_MIN" in f for f in v.failures), v.failures
    assert v.source_age_minutes and v.source_age_minutes > config.WEATHER_MAX_STALENESS_MIN


def test_03_missing_timestamp_fails():
    w = bundle(time_str=None)
    w.retrieved_at_utc = ""
    v = V.validate_evidence(evidence(w=w), now=NOW)
    assert any("retrieved_at_utc" in f for f in v.failures)
    assert v.timestamp_present is False


def test_04_impossible_numeric_value_fails():
    v = V.validate_evidence(evidence(w=bundle(temp=99.0)), now=NOW)
    assert v.values_plausible is False
    assert any("temperature_c=99.0" in f and "outside plausible range" in f for f in v.failures)


def test_05_unresolved_location_fails():
    v = V.validate_evidence(evidence(loc=None, w=None, intent="official_alert"), now=NOW)
    assert v.location_resolved is False and v.sufficient is False
    assert any("no resolved location" in f for f in v.failures)


def test_06_checked_with_no_alerts_is_a_valid_positive_result():
    v = V.validate_evidence(evidence(alerts=checked_alerts()), now=NOW)
    assert v.alerts_valid is True and v.alert_integrity is True
    assert not [f for f in v.failures if "alert" in f.lower()]


def test_07_unavailable_alerts_is_a_distinct_state_from_checked():
    v1 = V.validate_evidence(evidence(alerts=checked_alerts()), now=NOW)
    v2 = V.validate_evidence(
        evidence(alerts=checked_alerts(state="unavailable", error="ConnectTimeout")), now=NOW
    )
    assert v1.alerts_valid is True and v2.alerts_valid is False
    # the outage is RECORDED either way, and is never worded as "no alert exists"
    assert any("alert status is UNKNOWN" in w for w in v2.warnings), v2.warnings
    assert any("ConnectTimeout" in w for w in v2.warnings)
    # a weather question survives it (quality caps it instead); an alert question does not
    assert v2.sufficient is True and v2.ok is True
    v3 = V.validate_evidence(
        evidence(alerts=checked_alerts(state="unavailable", error="ConnectTimeout"),
                 intent="official_alert", timeframe="today"), now=NOW
    )
    assert v3.sufficient is False
    assert any("question was about official alerts" in f for f in v3.failures)
    # and the two states are still distinguishable from each other
    assert v1.alerts_valid is not v2.alerts_valid
    v4 = V.validate_evidence(evidence(alerts=AlertsEvidence(state="not_checked")), now=NOW)
    assert v4.alerts_valid is None and v4.alert_integrity is None


def test_08_invalid_relevant_alert_fails_integrity():
    # an alert that is NOT relevant to us, or one that is expired, must never sit in items[]
    # `authority` is Literal["official"] on the model, so a wrong label can only arrive by
    # bypassing construction (e.g. a hand-built payload) -> model_copy(update=...) skips validation
    # and lets us prove the VALIDATOR catches it, not the schema.
    bad_alerts = (
        alert(relevance="not_relevant"),
        alert(validity="expired"),
        alert().model_copy(update={"authority": "research_repro"}),
        alert(source="Some Blog"),
    )
    for bad in bad_alerts:
        v = V.validate_evidence(evidence(alerts=checked_alerts(bad)), now=NOW)
        assert v.alert_integrity is False and not v.ok, bad.alert_id
    # and one whose 'active' claim has no parseable expiry
    v = V.validate_evidence(evidence(alerts=checked_alerts(alert(expires=None))), now=NOW)
    assert any("no parseable expiry" in f for f in v.failures)


def test_09_referenced_nonexistent_alert_is_caught():
    ev = evidence(alerts=checked_alerts(alert()))
    ev.validation = V.validate_evidence(ev, now=NOW)
    ev.advisory = ADV.advise(ev)
    assert V.advisory_references_ok(ev)[0] is True          # honest reference passes
    ev.advisory.alert_ids = ["IN-MADE-UP-999"]              # now it cites something absent
    ok, failures = V.advisory_references_ok(ev)
    assert ok is False and any("IN-MADE-UP-999" in f for f in failures)
    assert V.alert_ids_present(ev.alerts, ["A1", "nope"]) == ["nope"]


def test_10_label_and_date_consistency_is_checked():
    # a "tomorrow" answer whose block is actually dated yesterday must fail, not warn
    w = bundle()
    w.tomorrow.date = "2026-08-30"
    ev = evidence(w=w, timeframe="tomorrow")
    ev.request["timeframe"] = "tomorrow"
    v = V.validate_evidence(ev, now=NOW)
    assert v.labeling_consistent is False
    assert any("not after the local date" in f for f in v.failures)
    # missing day block for a forecast question is also a failure
    w2 = bundle(tomorrow_mm=None)
    ev2 = evidence(w=w2)
    ev2.request["timeframe"] = "tomorrow"
    assert any("no matching day block" in f for f in V.validate_evidence(ev2, now=NOW).failures)


def test_11_phase2_alert_fields_survive_validation():
    """Validation must EXTEND the Validation object, not erase Phase 2's work."""
    ev = evidence(alerts=checked_alerts(state="unavailable", error="boom"))
    ev.validation.checks_run.append("alerts_unavailable")
    ev.validation.warnings.append("official alert source could not be consulted: boom")
    v = V.validate_evidence(ev, now=NOW)
    assert "alerts_unavailable" in v.checks_run
    assert "official alert source could not be consulted: boom" in v.warnings


# ============================ 2. EVIDENCE QUALITY ============================ #
def test_12_official_active_alert_fresh_complete_is_HIGH():
    ev = evidence(alerts=checked_alerts(alert()), intent="official_alert", timeframe="today")
    ev.validation = V.validate_evidence(ev, now=NOW)
    label, b = Q.score_evidence(ev, ev.validation)
    assert label == "HIGH" and b["score"] == 100
    assert b["weights"] == {"authority": 40, "freshness": 30, "completeness": 20, "agreement": 10}
    assert b["breakdown"]["caps_applied"] == []
    assert "not a probability" in b["meaning"].lower().replace("is NOT a probability", "not a probability")


def test_13_alert_unavailable_caps_at_MEDIUM():
    ev = evidence(alerts=checked_alerts(state="unavailable", error="ConnectTimeout"))
    ev.validation = V.validate_evidence(ev, now=NOW)
    label, b = Q.score_evidence(ev, ev.validation)
    assert label in {"MEDIUM", "LOW"} and label != "HIGH"
    assert any("rule 1" in c for c in b["breakdown"]["caps_applied"]), b["breakdown"]


def test_14_uncertain_alert_caps_at_MEDIUM():
    ev = evidence(alerts=checked_alerts(uncertain=2))
    ev.validation = V.validate_evidence(ev, now=NOW)
    label, b = Q.score_evidence(ev, ev.validation)
    assert label != "HIGH"
    assert any("rule 2" in c for c in b["breakdown"]["caps_applied"])


def test_15_stale_data_is_LOW():
    ev = evidence(w=bundle(time_str=STALE_LOCAL))
    ev.validation = V.validate_evidence(ev, now=NOW)
    label, b = Q.score_evidence(ev, ev.validation)
    assert label == "LOW"
    assert any("rule 4" in c for c in b["breakdown"]["caps_applied"])
    assert b["breakdown"]["freshness"] < 30


def test_16_missing_required_evidence_is_LOW():
    ev = evidence(w=bundle(temp=None, time_str=FRESH_LOCAL))
    ev.weather.current.temperature_c = None       # the field this question needs is gone
    ev.validation = V.validate_evidence(ev, now=NOW)
    label, b = Q.score_evidence(ev, ev.validation)
    assert label == "LOW" and b["breakdown"]["completeness"] < 20
    assert any("rule 3" in c for c in b["breakdown"]["caps_applied"])


def test_17_single_source_agreement_is_neutral_not_penalised():
    ev = evidence()
    ev.validation = V.validate_evidence(ev, now=NOW)
    _label, b = Q.score_evidence(ev, ev.validation)
    assert b["breakdown"]["agreement"] == 10
    assert any("only one comparable weather source" in n for n in b["notes"])
    assert b["disagreements"] == []


def test_18_conflicting_sources_reduce_agreement_and_are_surfaced():
    ev = evidence()
    # a second comparable source describing a different period + a different temperature value
    ev.sources.append(Source(name="IMD (future provider)", type="forecast", timestamp="2026-09-01T00:00",
                             period="2026-08-31", authority="official"))
    ev.weather.current.temperature_c = 26.0
    ev.validation = V.validate_evidence(ev, now=NOW)
    _label, b = Q.score_evidence(ev, ev.validation)
    assert b["breakdown"]["agreement"] == 0
    assert b["disagreements"], "disagreement must be surfaced, not averaged away"
    assert any("different periods" in d for d in b["disagreements"])


def test_19_scoring_is_deterministic_and_labelled_not_a_probability():
    ev = evidence()
    ev.validation = V.validate_evidence(ev, now=NOW)
    first = Q.score_evidence(ev, ev.validation)
    second = Q.score_evidence(ev, ev.validation)
    assert first[0] == second[0] and first[1]["score"] == second[1]["score"]
    # the word "confidence" must not appear in the public vocabulary of this feature
    joined = " ".join([*first[1]["notes"], first[1]["meaning"]]).lower()
    assert "confidence" not in joined
    assert "evidence quality" in first[1]["meaning"].lower()


# ================================ 3. ADVISORY ================================ #
def _advise(alerts=None, w=None, quality_label=None, intent="forecast_current", timeframe="now"):
    ev = evidence(alerts=alerts, w=w, intent=intent, timeframe=timeframe)
    ev.validation = V.validate_evidence(ev, now=NOW)
    if quality_label is None:
        quality_label, _ = Q.score_evidence(ev, ev.validation)
    ev.evidence_quality = quality_label
    return ADV.advise(ev), ev


def test_20_active_severe_official_alert_forces_HIGH():
    adv, _ = _advise(alerts=checked_alerts(alert(severity="Severe")))
    assert adv.risk_level == "HIGH"
    assert "R1_active_severe_official_alert" in adv.rules_fired
    assert adv.alert_ids == ["A1"]
    assert adv.headline.startswith("Weather-related travel risk is HIGH based on")


def test_21_active_extreme_official_alert_forces_HIGH():
    adv, _ = _advise(alerts=checked_alerts(alert(severity="Extreme")))
    assert adv.risk_level == "HIGH" and "R1_active_severe_official_alert" in adv.rules_fired


def test_22_alert_priority_survives_low_evidence_quality():
    """The safety rule: bad weather data must never bury a live official warning."""
    adv, ev = _advise(alerts=checked_alerts(alert(severity="Extreme")), w=bundle(time_str=STALE_LOCAL))
    assert ev.validation.fresh is False
    assert adv.risk_level == "HIGH"
    assert any("active official" in adv.headline for _ in [0]) or "active official" in adv.headline


def test_23_uncertain_alert_gives_uncertain_not_false_confidence():
    adv, _ = _advise(alerts=checked_alerts(uncertain=1))
    assert adv.risk_level == "UNCERTAIN"
    assert "could not be confirmed" in adv.reason
    assert "R4_alert_relevance_uncertain" in adv.rules_fired


def test_24_uncertain_alert_with_a_real_hazard_is_MEDIUM():
    adv, _ = _advise(alerts=checked_alerts(uncertain=1), w=bundle(precip=12.0, code=95))
    assert adv.risk_level == "MEDIUM"
    assert {"R3_weather_hazard_strong", "R4_alert_relevance_uncertain"} & set(adv.rules_fired)


def test_25_benign_forecast_with_checked_alerts_is_LOW():
    adv, ev = _advise(alerts=checked_alerts(), w=bundle(precip=0.0, code=1, tomorrow_mm=2.0))
    assert adv.risk_level == "LOW" and "R7_quiet" in adv.rules_fired
    assert "checked result" in adv.reason  # never says "no alert exists anywhere"


def test_26_heavy_rain_elevates_risk_on_its_own():
    w = bundle(tomorrow_mm=140.0, precip=9.0, code=82)
    adv, _ = _advise(alerts=checked_alerts(), w=w, intent="advisory_risk", timeframe="tomorrow")
    assert adv.risk_level == "HIGH" and "R3_weather_hazard_strong" in adv.rules_fired
    assert any("140.0 mm total" in f for f in adv.factors)
    assert adv.activity == "travel"


def test_27_unverifiable_alerts_never_yield_a_low_risk_all_clear():
    adv, _ = _advise(alerts=checked_alerts(state="unavailable", error="feed down"),
                     w=bundle(precip=0.0, code=1, tomorrow_mm=1.0))
    assert adv.risk_level == "UNCERTAIN"
    assert "R5_alerts_unverifiable" in adv.rules_fired
    assert "could not be consulted" in " ".join(adv.factors)


def test_28_no_reliable_evidence_is_uncertain():
    adv, ev = _advise(alerts=checked_alerts(), w=bundle(time_str=STALE_LOCAL))
    assert adv.risk_level == "UNCERTAIN" and "R6_insufficient_evidence" in adv.rules_fired


def test_29_wording_never_promises_or_denies_safety():
    for kwargs in ({}, {"alerts": checked_alerts(alert())}, {"alerts": checked_alerts(state="unavailable")},
                   {"alerts": checked_alerts(uncertain=3)}):
        adv, _ = _advise(**kwargs)
        text = (adv.headline + " " + adv.reason).lower()
        for forbidden in ("it is safe", "safe to travel", "it is unsafe", "do not travel",
                          "guarantee", "all clear"):
            assert forbidden not in text, (forbidden, adv.headline)
        assert "weather-related" in adv.headline.lower()
        assert "personal safety" in adv.disclaimer or "not an official order" in adv.disclaimer


def test_30_advisory_only_reads_evidence_and_is_deterministic():
    adv1, ev = _advise(alerts=checked_alerts(alert()))
    adv2, _ = _advise(alerts=checked_alerts(alert()))
    assert adv1.model_dump() == adv2.model_dump()
    # and its cited alerts exist in the evidence it was derived from
    assert set(adv1.alert_ids) <= {a.alert_id for a in ev.alerts.items}


# ============================== 4. INTEGRATION ============================== #
# The pipeline is exercised with the three network services replaced by deterministic stubs, so
# these tests cover main.py's real wiring (stages, abstention on insufficient evidence, alert
# priority) without needing internet.
def _patch_pipeline(monkeypatch, *, geo_status="ok", alerts=None, weather_bundle=None, geo_location=LOC):
    from backend import main

    async def fake_resolve(text, context=None, **kw):
        if geo_status == "ok":
            return GeocodeResult(status="ok", query=text or "pune", location=geo_location)
        if geo_status == "ambiguous":
            return GeocodeResult(status="ambiguous", query=text or "springfield",
                                 clarification="I found multiple places matching \u201cspringfield\u201d. Which location do you mean?")
        return GeocodeResult(status="unresolved", query=text or "?", evidence_gap="no_geocode_match")

    class _P:
        async def fetch(self, lat, lon, **kw):
            return weather_bundle if weather_bundle is not None else bundle()

    monkeypatch.setattr(main.geocoding, "resolve", fake_resolve)
    monkeypatch.setattr(main.weather, "get_provider", lambda: _P())
    monkeypatch.setattr(
        main.alerts_service, "check_alerts",
        lambda loc, **kw: _done(alerts if alerts is not None else checked_alerts()),
    )


async def _done(x):
    return x


@pytest.mark.parametrize("stage_name", ["validate", "quality", "advise"])
def test_31_to_33_query_pipeline_contains_the_three_new_stages(monkeypatch, stage_name):
    import asyncio

    from backend.main import run_pipeline

    _patch_pipeline(monkeypatch, alerts=checked_alerts(alert()))
    _ev, trace = asyncio.run(run_pipeline("What is the weather in Pune right now?"))
    stages = [s["stage"] for s in trace["stages"]]
    # Extended for Phase 4 (llm/grounding appended after advise) — still an exact ordered list, so
    # removing or reordering any stage fails here.
    assert stages == ["parse", "geocode", "retrieve_weather", "retrieve_alerts", "evidence",
                      "validate", "quality", "advise", "llm", "grounding"]
    assert stage_name in stages


def test_34_stage_order_is_fixed_and_evidence_stage_still_first_of_the_old_five(monkeypatch):
    import asyncio

    from backend.main import run_pipeline

    _patch_pipeline(monkeypatch)
    _ev, trace = asyncio.run(run_pipeline("What is the weather in Pune right now?"))
    stages = [s["stage"] for s in trace["stages"]]
    assert stages.index("retrieve_alerts") < stages.index("evidence") < stages.index("validate")
    assert stages.index("validate") < stages.index("quality") < stages.index("advise")


def test_35_alert_evidence_survives_phase3_untouched(monkeypatch):
    import asyncio

    from backend.main import run_pipeline

    items = [alert(severity="Severe")]
    _patch_pipeline(monkeypatch, alerts=checked_alerts(*items),
                    weather_bundle=bundle())
    ev, trace = asyncio.run(run_pipeline("Is there any weather alert for Pune today?"))
    assert [a.alert_id for a in ev.alerts.items] == ["A1"]
    assert ev.alert_state == "checked"
    assert any(s.name == "NDMA SACHET" and s.authority == "official" for s in ev.sources)
    assert next(s for s in trace["stages"] if s["stage"] == "retrieve_alerts")["status"] == "checked"
    assert ev.risk == "HIGH"                      # active Severe alert -> priority rule
    assert ev.evidence_quality == "HIGH"


def test_36_stale_data_now_abstains_instead_of_answering(monkeypatch):
    import asyncio

    from backend.main import run_pipeline

    _patch_pipeline(monkeypatch, weather_bundle=bundle(time_str=STALE_LOCAL))
    ev, trace = asyncio.run(run_pipeline("What is the weather in Pune right now?"))
    assert ev.status == "abstain"
    assert "could not verify this evidence" in ev.abstain_reason
    assert ev.evidence_quality == "LOW"
    assert ev.risk == "UNCERTAIN"
    assert any(s["stage"] == "abstain_or_clarify" for s in trace["stages"])


def test_37_weather_failure_path_is_unchanged_and_still_scored(monkeypatch):
    import asyncio

    from backend import main
    from backend.services.http_client import UpstreamError

    _patch_pipeline(monkeypatch)

    class _Boom:
        async def fetch(self, *a, **k):
            raise UpstreamError("open-meteo", "ConnectTimeout: simulated")

    monkeypatch.setattr(main.weather, "get_provider", lambda: _Boom())
    ev, trace = asyncio.run(main.run_pipeline("What is the weather in Pune right now?"))
    assert ev.status == "abstain" and ev.evidence_quality == "LOW"
    assert "upstream weather source failed" in ev.abstain_reason
    assert next(s for s in trace["stages"] if s["stage"] == "retrieve_alerts")["status"] == "skipped"
    assert ev.risk == "UNCERTAIN"                 # no evidence -> no confident recommendation


def test_38_ambiguous_location_behaviour_is_unchanged(monkeypatch):
    import asyncio

    from backend.main import run_pipeline

    _patch_pipeline(monkeypatch, geo_status="ambiguous")
    ev, trace = asyncio.run(run_pipeline("What is the weather in Springfield?"))
    assert ev.status == "clarify" and ev.evidence_quality == "LOW"
    assert "multiple places" in ev.clarification
    assert ev.weather is None and ev.alert_state == "not_checked"
    # Phase 1 short-circuits BEFORE the new stages, exactly as it did before Phase 3
    assert [s["stage"] for s in trace["stages"]] == ["parse", "geocode", "abstain_or_clarify"]


def test_39_unresolvable_location_behaviour_is_unchanged(monkeypatch):
    import asyncio

    from backend.main import run_pipeline

    _patch_pipeline(monkeypatch, geo_status="unresolved")
    ev, trace = asyncio.run(run_pipeline("What is the weather in Xylophoneistan?"))
    assert ev.status == "abstain" and ev.weather is None and ev.evidence_quality == "LOW"
    assert "couldn\u2019t verify a real location" in ev.abstain_reason
    assert [s["stage"] for s in trace["stages"]] == ["parse", "geocode", "abstain_or_clarify"]


def test_40_quality_breakdown_keeps_the_phase2_alert_blocker(monkeypatch):
    import asyncio

    from backend.main import run_pipeline

    _patch_pipeline(monkeypatch, alerts=checked_alerts(state="unavailable", error="feed down"))
    ev, _ = asyncio.run(run_pipeline("Is there any weather alert for Pune today?"))
    assert ev.quality_breakdown["alert_intent_blocker"] == "feed down"   # Phase 2 contract kept
    assert ev.quality_breakdown["breakdown"]["caps_applied"], "cap must be recorded"
    assert ev.evidence_quality in {"MEDIUM", "LOW"} and ev.evidence_quality != "HIGH"
    assert ev.status == "abstain"   # an alert question without an alert source cannot be answered
