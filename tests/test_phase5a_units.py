"""
test_phase5a_units.py — Phase 5A: NWP/provider registry + model metadata. Fully offline.

These tests pin the provider foundation without touching the network:

  * the registry catalogues open-meteo (live) + imd/gfs/wrf (architecture-ready stubs);
  * the stubs satisfy the WeatherProvider shape but FAIL GRACEFULLY (UpstreamError) — they never
    return fabric data, which is the whole safety rule for an unintegrated provider;
  * get_provider() delegates to the registry; an unknown WEATHER_PROVIDER is still a selection
    error, a registered stub is a runtime UpstreamError (the normal abstain/failure convention);
  * OpenMeteoProvider sends the optional `models=` param only when OPEN_METEO_MODEL is set and
    records the selected model on WeatherBundle.model (default = "best_match" for live);
  * the weather Source/Evidence is provider-agnostic (name/authority come from the registry).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any, Dict

import pytest

from backend import config
from backend.services import providers
from backend.services import weather as weather_service
from backend.services import evidence as evidence_service
from backend.services.http_client import UpstreamError

NOW = dt.datetime(2026, 9, 1, 2, 30, tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------- #
# 1. registry content
# --------------------------------------------------------------------------- #
def test_01_registry_lists_open_meteo_live_and_three_stubs():
    keys = providers.available_keys()
    assert "open-meteo" in keys
    for stub in ("imd", "gfs", "wrf"):
        assert stub in keys
    assert providers.implemented_keys() == ["open-meteo"], \
        "only open-meteo is a live integration; nothing else may be advertised as implemented"
    for stub in ("imd", "gfs", "wrf"):
        info = providers.get_info(stub)
        assert info is not None and info.implemented is False


def test_02_open_meteo_is_the_only_live_and_is_not_labelled_official():
    info = providers.get_info("open-meteo")
    assert info.implemented is True
    # weather NWP blends are research_repro; the only "official" authority in the product is
    # NDMA SACHET (alerts). IMD's stub carries authority=official metadata for when it goes live,
    # but Open-Meteo must never be upgraded.
    assert providers.source_authority("open-meteo") == "research_repro"
    assert providers.source_label("open-meteo") == "Open-Meteo"


def test_03_registry_report_is_secret_free_and_marks_statuses():
    rep = providers.providers_report("open-meteo")
    assert rep["active"] == "open-meteo"
    assert set(rep["stubs"]) == {"imd", "gfs", "wrf"}
    statuses = {row["key"]: row["status"] for row in rep["all"]}
    assert statuses["open-meteo"] == "live"
    assert all(statuses[k] == "stub_not_implemented" for k in ("imd", "gfs", "wrf"))
    import json
    # secret-free: no API key material ever appears in the health report
    assert "gsk_" not in json.dumps(rep) and "authorization" not in json.dumps(rep).lower()


# --------------------------------------------------------------------------- #
# 2. stubs fail gracefully (never fabricate)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", ["imd", "gfs", "wrf"])
def test_04_stub_fetch_raises_upstream_error(key):
    provider = providers.create_provider(key)
    # same async signature as the real provider
    with pytest.raises(UpstreamError):
        asyncio.run(provider.fetch(18.52, 73.85, timeframe="now"))


def test_05_get_provider_delegates_to_registry_for_stub(monkeypatch):
    monkeypatch.setattr(config, "WEATHER_PROVIDER", "imd")
    # weather.get_provider() caches a singleton; reset it so selection re-runs.
    monkeypatch.setattr(weather_service, "_PROVIDER", None)
    provider = weather_service.get_provider()
    assert provider.name == "imd"
    with pytest.raises(UpstreamError):
        asyncio.run(provider.fetch(18.52, 73.85))
    monkeypatch.setattr(weather_service, "_PROVIDER", None)


def test_06_unknown_provider_is_a_selection_error(monkeypatch):
    monkeypatch.setattr(config, "WEATHER_PROVIDER", "not-a-real-provider")
    monkeypatch.setattr(weather_service, "_PROVIDER", None)
    with pytest.raises(RuntimeError):
        weather_service.get_provider()
    monkeypatch.setattr(weather_service, "_PROVIDER", None)


# --------------------------------------------------------------------------- #
# 3. Open-Meteo model metadata
# --------------------------------------------------------------------------- #
def _fake_open_meteo_payload() -> Dict[str, Any]:
    return {
        "latitude": 18.52, "longitude": 73.85, "elevation": 560.0,
        "utc_offset_seconds": 19800,
        "current": {
            "time": "2026-09-01T07:45", "interval": 900,
            "temperature_2m": 25.8, "apparent_temperature": 28.0,
            "relative_humidity_2m": 88.0, "precipitation": 0.0,
            "weather_code": 3, "cloud_cover": 80.0,
            "wind_speed_10m": 12.4, "wind_direction_10m": 220.0, "pressure_msl": 1008.0,
        },
        "current_units": {"temperature_2m": "°C"},
        "daily": {
            "time": ["2026-09-01", "2026-09-02"],
            "weather_code": [3, 61],
            "temperature_2m_max": [30.0, 29.0], "temperature_2m_min": [23.0, 22.0],
            "precipitation_sum": [12.0, 6.0],
            "precipitation_probability_max": [60.0, 80.0],
            "wind_speed_10m_max": [20.0, 30.0],
        },
        "daily_units": {"temperature_2m_max": "°C"},
    }


def test_07_default_open_meteo_omits_models_param_and_reports_best_match(monkeypatch):
    captured: Dict[str, Any] = {}

    async def fake_get_json(url, *, params=None, service=None, **kw):
        captured["params"] = params
        return _fake_open_meteo_payload()

    monkeypatch.setattr(weather_service, "get_json", fake_get_json)
    monkeypatch.setattr(weather_service, "_utc_now", lambda: NOW)
    monkeypatch.setattr(config, "OPEN_METEO_MODEL", "")

    provider = weather_service.OpenMeteoProvider()
    bundle = asyncio.run(provider.fetch(18.52, 73.85, timeframe="now", timezone="Asia/Kolkata"))
    assert "models" not in captured["params"], "no model param => Open-Meteo chooses best_match"
    assert bundle.provider == "open-meteo"
    assert bundle.model == "best_match"


def test_08_explicit_open_meteo_model_is_sent_and_recorded(monkeypatch):
    captured: Dict[str, Any] = {}

    async def fake_get_json(url, *, params=None, service=None, **kw):
        captured["params"] = params
        return _fake_open_meteo_payload()

    monkeypatch.setattr(weather_service, "get_json", fake_get_json)
    monkeypatch.setattr(weather_service, "_utc_now", lambda: NOW)
    monkeypatch.setattr(config, "OPEN_METEO_MODEL", "gfs_seamless")

    provider = weather_service.OpenMeteoProvider()
    bundle = asyncio.run(provider.fetch(18.52, 73.85, timeframe="now", timezone="Asia/Kolkata"))
    assert captured["params"].get("models") == "gfs_seamless"
    assert bundle.model == "gfs_seamless"


def test_09_historical_bundle_marks_reanalysis_not_forecast_model(monkeypatch):
    async def fake_get_json(url, *, params=None, service=None, **kw):
        return _fake_open_meteo_payload()

    monkeypatch.setattr(weather_service, "get_json", fake_get_json)
    monkeypatch.setattr(weather_service, "_utc_now", lambda: NOW)
    monkeypatch.setattr(config, "OPEN_METEO_MODEL", "gfs_seamless")

    provider = weather_service.OpenMeteoProvider()
    # a past date routes to the archive call
    bundle = asyncio.run(provider.fetch(
        18.52, 73.85, timeframe="past", timezone="Asia/Kolkata", target_date="2026-08-20"
    ))
    assert bundle.kind == "historical"
    assert bundle.model == "reanalysis_archive", "archive rows must not claim a forecast NWP model"


# --------------------------------------------------------------------------- #
# 4. provider-agnostic evidence/Source naming
# --------------------------------------------------------------------------- #
def test_10_evidence_source_uses_registry_label_and_authority():
    from backend.models import (
        GeocodeResult,
        ParsedQuery,
        ResolvedLocation,
        WeatherBundle,
    )

    loc = ResolvedLocation(
        name="Pune", latitude=18.52, longitude=73.85, country="India", country_code="IN",
        admin1="Maharashtra", admin2="Pune", timezone="Asia/Kolkata",
    )
    geo = GeocodeResult(status="ok", query="pune", location=loc)
    parsed = ParsedQuery(message="weather", intent="forecast_current", timeframe="now")
    bundle = WeatherBundle(
        provider="open-meteo", model="best_match", kind="live",
        requested_timeframe="now", retrieved_at_utc="2026-09-01T02:30:00Z",
        api_utc_offset_seconds=19800, current=None,
        request_url="https://api.open-meteo.com/v1/forecast?x=1",
    )
    ev = evidence_service.build_evidence(parsed, geo, bundle, None)
    weather_sources = [s for s in ev.sources if s.type in ("forecast", "historical")]
    assert len(weather_sources) == 1
    assert weather_sources[0].name == "Open-Meteo"          # registry label, not a literal in evidence.py
    assert weather_sources[0].authority == "research_repro"
    geo_sources = [s for s in ev.sources if s.type == "geocoding"]
    assert geo_sources and geo_sources[0].name == "Open-Meteo Geocoding"


def test_11_provider_label_helper_is_registry_backed(monkeypatch):
    monkeypatch.setattr(config, "WEATHER_PROVIDER", "open-meteo")
    assert evidence_service.provider_label() == "Open-Meteo"
    monkeypatch.setattr(config, "WEATHER_PROVIDER", "imd")
    assert evidence_service.provider_label() == "IMD"


def test_12_completeness_weather_source_check_is_provider_agnostic():
    """validation's weather_source presence must accept ANY comparable provider, not only one
    literally named 'Open-Meteo' (regression guard for the de-hardening)."""
    from backend.models import Evidence, Source
    from backend.services.validation import _present

    ev = Evidence()
    ev.sources.append(Source(name="NOAA GFS", type="forecast", url="https://example/gfs",
                             authority="research_repro"))
    assert _present(ev, "weather_source", "now") is True

    ev2 = Evidence()
    ev2.sources.append(Source(name="Open-Meteo Geocoding", type="geocoding",
                              url="https://example/geo", authority="research_repro"))
    assert _present(ev2, "weather_source", "now") is False
