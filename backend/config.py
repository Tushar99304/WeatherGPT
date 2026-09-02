"""
config.py — single place where every environment variable is read.

Why a hand-written loader instead of pydantic-settings?
  One less dependency for a 48h sprint. .env parsing here is deliberately dumb:
  KEY=VALUE, '#' comments, optional `export ` prefix. No shell evaluation.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # .../weathergpt-mvp
ENV_FILE = BASE_DIR / ".env"


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ (existing env vars win)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ENV_FILE)


def _s(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _i(name: str, default: int) -> int:
    try:
        return int(float(_s(name, str(default))))
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(_s(name, str(default)))
    except ValueError:
        return default


def _b(name: str, default: bool = False) -> bool:
    return _s(name, "true" if default else "false").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------- LLM (Phase 4)
GROQ_API_KEY = _s("GROQ_API_KEY")                       # empty => LLM disabled, safe fallback used
GROQ_MODEL = _s("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_BASE_URL = _s("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

# The API key itself is only ever read from the environment (or .env) and is never written into
# code, logs or /health. Everything here is optional: with no key the pipeline still answers, via
# the deterministic evidence-based fallback (see services/llm.py).
LLM_ENABLED = _b("LLM_ENABLED", True)          # false => skip the call entirely, use the fallback
LLM_CHAT_COMPLETIONS_PATH = _s("LLM_CHAT_COMPLETIONS_PATH", "/chat/completions")
LLM_TIMEOUT_S = _f("LLM_TIMEOUT_S", 30.0)      # LLM answers are slower than data lookups
LLM_MAX_TOKENS = _i("LLM_MAX_TOKENS", 1000)
LLM_TEMPERATURE = _f("LLM_TEMPERATURE", 0.0)   # 0: the explanation must be reproducible
LLM_JSON_MODE = _b("LLM_JSON_MODE", True)      # response_format={"type":"json_object"}
LLM_MAX_ATTEMPTS = _i("LLM_MAX_ATTEMPTS", 2)   # first answer + exactly ONE regeneration

# ---------------------------------------------------------------- Weather source (Phase 5A)
# Every weather source goes through the WeatherProvider interface and the registry in
# services/providers/. Only "open-meteo" is CURRENT/IMPLEMENTED. The keys "imd", "gfs" and
# "wrf" are registered as ARCHITECTURE-READY STUBS: they are discoverable via the registry and
# /health but raise the project's normal UpstreamError on fetch (-> abstain/fallback), so no live
# data is ever faked. See services/providers/stubs.py and README §6.
WEATHER_PROVIDER = _s("WEATHER_PROVIDER", "open-meteo")
OPEN_METEO_FORECAST_URL = _s("OPEN_METEO_FORECAST_URL", "https://api.open-meteo.com/v1/forecast")
OPEN_METEO_ARCHIVE_URL = _s("OPEN_METEO_ARCHIVE_URL", "https://archive-api.open-meteo.com/v1/archive")
# Optional Open-Meteo NWP model selection (forecast endpoint `models=` param).
#   "" (default) -> omit the param; Open-Meteo chooses "best_match".
# Phase 5A exposes a SINGLE configurable model (it is NOT multi-model ensemble retrieval, which
# is out of scope). e.g. OPEN_METEO_MODEL=gfs_seamless  / =ecmwf_ifs025 . Leave blank for default.
# The active model is reported on WeatherBundle.model and in /health. Archive calls ignore it
# (ERA5-style reanalysis carries its own model and does not accept a forecast `models=` param).
OPEN_METEO_MODEL = _s("OPEN_METEO_MODEL", "")

# ---------------------------------------------------------------- Geocoding
OPEN_METEO_GEOCODING_URL = _s(
    "OPEN_METEO_GEOCODING_URL", "https://geocoding-api.open-meteo.com/v1/search"
)
GEO_COUNTRY_BIAS = _s("GEO_COUNTRY_BIAS", "IN").upper()  # "" = no bias
GEO_MAX_RESULTS = _i("GEO_MAX_RESULTS", 10)               # candidates fetched (ambiguity detection)
# A same-named place only competes (=> "ambiguous, ask the user") if it is a real settlement.
# Verified live: GeoNames returns "Nagpur, Maharashtra (2.4M)" AND a hamlet "Nagpur" in UP.
# Without this, a trivial question would trigger a pointless clarification. See geocoding.py rule B.
AMBIGUITY_MIN_POP = _i("AMBIGUITY_MIN_POP", 100000)
# "none" disables; "nominatim" = OSM fallback for towns GeoNames misses (e.g. Lonavala -> 0 results).
GEO_FALLBACK = _s("GEO_FALLBACK", "nominatim").lower()
NOMINATIM_URL = _s("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
NOMINATIM_USER_AGENT = _s("NOMINATIM_USER_AGENT", "WeatherGPT-MVP/0.1 (SIH26068 student prototype)")

# ---------------------------------------------------------------- Alerts (Phase 2)
SACHET_ENABLED = _b("SACHET_ENABLED", True)
SACHET_RSS_BASE = _s("SACHET_RSS_BASE", "https://sachet.ndma.gov.in/cap_public_website/rss")
SACHET_CAP_URL = _s(
    "SACHET_CAP_URL", "https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier={id}"
)
SACHET_USER_AGENT = _s("SACHET_USER_AGENT", "WeatherGPT-MVP/0.1 (SIH26068 student prototype)")
ALERT_CACHE_TTL_S = _i("ALERT_CACHE_TTL_S", 300)   # polite polling: reuse feed for N seconds
ALERT_MAX_AGE_H = _i("ALERT_MAX_AGE_H", 24)        # ignore official alerts older than this
ALERT_DETAIL_LIMIT = _i("ALERT_DETAIL_LIMIT", 8)   # max CAP records fetched per query
ALERT_DETAIL_CONCURRENCY = _i("ALERT_DETAIL_CONCURRENCY", 4)
# Also pull the national feed even when a state feed exists: the state feed is a narrow
# subset (10 items) and the India feed (100 items) is the complete published set.
ALERT_INCLUDE_INDIA_FEED = _b("ALERT_INCLUDE_INDIA_FEED", True)
# Fixture replay (deterministic offline demo / tests). Never presented as live data:
# AlertsMeta.mode is set to "fixture_replay" so the UI and the judges can see it.
ALERT_FIXTURE_RSS = _s("ALERT_FIXTURE_RSS")            # path to an .rss/.xml file
ALERT_FIXTURE_CAP_DIR = _s("ALERT_FIXTURE_CAP_DIR")    # dir of <identifier>.xml CAP files
# Demo switch: force alerts_available=false path without touching the network.
SIMULATE_ALERT_FAILURE = _b("SIMULATE_ALERT_FAILURE")

# ---------------------------------------------------------------- Validation knobs
HTTP_TIMEOUT_S = _f("HTTP_TIMEOUT_S", 15.0)
WEATHER_MAX_STALENESS_MIN = _f("WEATHER_MAX_STALENESS_MIN", 90.0)

# ---------------------------------------------------------------- Demo switches
# Deterministic ways to trigger the abstention path when the internet is being flaky.
SIMULATE_WEATHER_FAILURE = _b("SIMULATE_WEATHER_FAILURE")
SIMULATE_STALE_DATA = _b("SIMULATE_STALE_DATA")
SIMULATE_LATENCY_MS = _i("SIMULATE_LATENCY_MS", 0)
# Phase 4 demo switches. These exist to PROVE THE GUARD FIRES, never to fake a successful LLM:
# SIMULATE_LLM_HALLUCINATION makes the "model" return an answer containing a number that is not in
# the evidence, so grounding must reject it and fall back. SIMULATE_LLM_FAILURE behaves like a dead
# or timed-out Groq, so the weather answer must survive on the deterministic path.
SIMULATE_LLM_FAILURE = _b("SIMULATE_LLM_FAILURE")
SIMULATE_LLM_HALLUCINATION = _b("SIMULATE_LLM_HALLUCINATION")

# Physical sanity ranges used by validation (documented so judges can see it is
# a plausibility filter, not a meteorological judgement).
RANGES = {
    "temperature_c": (-60.0, 60.0),
    "apparent_temperature_c": (-70.0, 70.0),
    "precipitation_mm": (0.0, 500.0),
    "precip_probability_pct": (0.0, 100.0),
    "wind_kmh": (0.0, 400.0),
    "humidity_pct": (0.0, 100.0),
}
