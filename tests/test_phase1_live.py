"""
Live smoke test of the Phase 1 pipeline (real Open-Meteo calls; no key required).

Run:  python -m pytest tests/test_phase1_live.py -q
Offline (skip network tests):  python -m pytest tests -q -m "not live"

Uses asyncio.run in plain sync tests so no pytest plugin needs installing.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.live


def _ask(message: str):
    from backend.main import run_pipeline

    return asyncio.run(run_pipeline(message))


def test_nagpur_now_returns_grounded_live_evidence():
    ev, trace = _ask("What is the weather in Nagpur right now?")
    assert ev.status == "grounded", ev.abstain_reason
    assert ev.location and abs(ev.location.latitude - 21.15) < 0.5
    assert ev.weather and ev.weather.current
    cur = ev.weather.current
    assert cur.temperature_c is not None and -10 < cur.temperature_c < 60
    assert cur.time, "timestamp must survive normalisation"
    weather_src = [s for s in ev.sources if s.name == "Open-Meteo"]
    assert weather_src and weather_src[0].timestamp
    assert weather_src[0].authority == "research_repro"  # honest: not official IMD
    # The stage LIST has grown by design (Phase 2 added retrieve_alerts, Phase 3 added
    # validate/quality/advise, Phase 4 added llm/grounding). Nothing here is loosened: the full
    # ordered list is still asserted exactly, so an accidental stage removal or reordering still
    # fails this test.
    assert [s["stage"] for s in trace["stages"]] == [
        "parse",
        "geocode",
        "retrieve_weather",
        "retrieve_alerts",
        "evidence",
        "validate",
        "quality",
        "advise",
        "llm",
        "grounding",
    ]
    # Phase 4 addition, not a replacement: the answer exists even where no LLM key is configured,
    # and it must agree with the fields it is only allowed to repeat.
    answer = trace.get("answer") or {}
    assert answer.get("text"), "an answer sentence must always be produced"
    assert answer.get("origin") in {"groq_llm", "deterministic_fallback"}
    assert answer.get("risk") == ev.advisory.risk_level
    assert answer.get("evidence_quality") == ev.evidence_quality
    assert ev.validation.ok and ev.validation.sufficient, ev.validation.failures
    assert ev.evidence_quality in {"HIGH", "MEDIUM", "LOW"}
    assert ev.advisory is not None and ev.risk == ev.advisory.risk_level


def test_pune_tomorrow_is_labelled_forecast_not_current():
    ev, _ = _ask("Will it rain in Pune tomorrow?")
    assert ev.status == "grounded", ev.abstain_reason
    assert ev.weather.tomorrow is not None and ev.weather.tomorrow.label == "Tomorrow"
    assert ev.weather.tomorrow.precipitation_probability_max_pct is not None
    src = next(s for s in ev.sources if s.name == "Open-Meteo")
    assert src.type == "forecast"


def test_unresolved_location_abstains_without_numbers():
    ev, _ = _ask("What is the weather in Xylophoneistan?")
    assert ev.status == "abstain"
    assert ev.weather is None  # no fabricated numbers, ever
    assert ev.evidence_quality == "LOW"
    assert "couldn" in (ev.abstain_reason or "")
