"""
Offline unit tests for the Phase 1 decision logic (no network needed).

Run:  python -m pytest tests -q      (from the weathergpt-mvp folder)
"""

from __future__ import annotations

import datetime as dt

import pytest

from backend.services import geocoding, parsing, weather
from backend.models import CurrentWeather

TODAY = dt.date(2026, 9, 1)


# ------------------------------------------------------------------ parsing -- #
@pytest.mark.parametrize(
    "message,intent,location,timeframe",
    [
        ("What is the weather in Nagpur right now?", "forecast_current", "Nagpur", "now"),
        ("Will it rain in Pune tomorrow?", "forecast_current", "Pune", "tomorrow"),
        ("Is there any weather alert for Mumbai today?", "official_alert", "Mumbai", "today"),
        ("Should I travel to Lonavala tomorrow?", "advisory_risk", "Lonavala", "tomorrow"),
        ("What was the weather in Delhi yesterday?", "historical_climate", "Delhi", "past"),
    ],
)
def test_rule_router(message, intent, location, timeframe):
    p = parsing.parse(message, today=TODAY)
    assert p.intent == intent, p.intent_reason
    assert (p.location_text or "").lower().startswith(location.lower())
    assert p.timeframe == timeframe


def test_explicit_past_date_is_historical_intent():
    p = parsing.parse("What is the weather in Ahmedabad on 2026-08-25?", today=TODAY)
    assert p.intent == "historical_climate", p.intent_reason
    assert p.timeframe == "specific_day"


def test_relative_timeframes_stay_relative():
    """"tomorrow" must NOT be pinned to a server-clock date; the location's own timezone
    decides the calendar day (see weather.fetch). This test locks that decision in."""
    p = parsing.parse("Will it rain in Pune tomorrow?", today=TODAY)
    assert p.timeframe == "tomorrow"
    assert p.target_date is None


def test_missing_location_forces_clarification():
    p = parsing.parse("What is the weather right now?", today=TODAY)
    assert p.intent == "clarification_needed"
    assert p.location_text is None


# ----------------------------------------------------------------- geocoding -- #
def _geo(name, lat, lon, country="India", cc="IN", a1="Maharashtra", a2=None, pop=1000):
    return {
        "name": name,
        "latitude": lat,
        "longitude": lon,
        "country": country,
        "country_code": cc,
        "admin1": a1,
        "admin2": a2,
        "population": pop,
    }


def test_ambiguous_springfield_is_not_silently_resolved():
    results = [
        _geo("Springfield", 39.8, -89.6, "United States", "US", "Illinois"),
        _geo("Springfield", 42.1, -72.6, "United States", "US", "Massachusetts"),
    ]
    res = geocoding.disambiguate("Springfield", results, country_bias="")
    assert res.status == "ambiguous"
    assert res.location is None
    assert "Which location do you mean" in (res.clarification or "")


def test_country_bias_resolves_pune_in_india():
    results = [_geo("Pune", 18.52, 73.86, pop=3_100_000), _geo("Pune", -9.37, 31.9, "Tanzania", "TZ", "Iringa")]
    res = geocoding.disambiguate("Pune", results, country_bias="IN")
    assert res.status == "ok"
    assert res.location.country_code == "IN"


def test_context_narrows_same_name_places():
    results = [
        _geo("Pune", 18.52, 73.86, a1="Maharashtra"),
        _geo("Pune", 19.0, 73.0, a1="Goa"),
    ]
    res = geocoding.disambiguate("Pune", results, context="Maharashtra", country_bias="IN")
    assert res.status == "ok"
    assert res.location.admin1 == "Maharashtra"


def test_no_results_is_unresolved_not_guessed():
    res = geocoding.disambiguate("Zzqxville", [], country_bias="IN")
    assert res.status == "unresolved"
    assert res.location is None
    assert res.evidence_gap == "no_geocode_match"


# ------------------------------------------------------------------ weather -- #
def test_condition_lookup_is_a_mapping_not_a_model_output():
    assert weather.condition_name(63) == "Moderate rain"
    assert weather.condition_name(None) is None
    assert weather.condition_name(4242) == "Unmapped WMO code 4242"


def test_staleness_uses_provider_timestamp(monkeypatch):
    # 3 hours ago in a +05:30 local clock -> ~180 minutes old.
    cw = CurrentWeather(time="2026-01-01T05:15")
    # Provider reports LOCAL wall time 2026-01-01T05:15 (+05:30 => 23:45Z prev day).
    # "Now" is 3 real hours later => 2026-01-01T02:45Z.
    monkeypatch.setattr(
        weather,
        "_utc_now",
        lambda: dt.datetime(2026, 1, 1, 2, 45, tzinfo=dt.timezone.utc),
    )
    mins = weather.minutes_since_source(cw, 19800)
    assert mins is not None
    assert 175 < mins < 185


def test_day_label_distinguishes_today_from_tomorrow():
    now_local = dt.date(2026, 9, 1)
    monkey = now_local  # readability
    offset = 19800

    class _D(dt.date):
        pass

    import backend.services.weather as w

    orig = w._local_now
    w._local_now = lambda off: dt.datetime(2026, 9, 1, 12, 0) + dt.timedelta(seconds=offset)
    try:
        assert w._day_label("2026-09-01", offset) == "Today"
        assert w._day_label("2026-09-02", offset) == "Tomorrow"
        assert "Yesterday" in w._day_label("2026-08-31", offset)
    finally:
        w._local_now = orig
    assert monkey == dt.date(2026, 9, 1)
