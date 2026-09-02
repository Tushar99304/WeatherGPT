"""
registry.py — Phase 5A provider catalogue + factory.

The rest of the app asks for weather through ``weather.get_provider()`` (kept for backward
compatibility). That factory now delegates here, so provider SELECTION lives in one data-driven
place while the WeatherProvider Protocol and OpenMeteoProvider stay in services/weather.py.

A ProviderInfo is plain metadata: a stable key, the human/Source name, what kind of source it is
(which maps directly onto models.Source.authority), whether it is actually implemented, and which
NWP model it serves by default. Implemented providers have ``factory()``; stubs return the
honest-failure classes in stubs.py.

Nothing here performs I/O and nothing upgrades a research/NWP source to "official". The only
official evidence in the product is NDMA SACHET (alerts.py) — a weather provider's authority label
is research_repro at best until a real meteorological service is wired behind this same key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from backend import config
from backend.services.http_client import UpstreamError
from backend.services.providers.stubs import (
    GFSStubProvider,
    IMDStubProvider,
    WRFStubProvider,
)


@runtime_checkable
class WeatherProvider(Protocol):
    """Mirrors services/weather.py's WeatherProvider Protocol (kept there as canonical)."""

    name: str

    async def fetch(
        self,
        latitude: float,
        longitude: float,
        *,
        timeframe: str = "now",
        timezone: Optional[str] = None,
        target_date: Optional[str] = None,
        utc_offset_seconds: Optional[int] = None,
    ) -> Any:
        ...


@dataclass(frozen=True)
class ProviderInfo:
    key: str                       # WEATHER_PROVIDER value / WeatherBundle.provider
    source_label: str              # models.Source.name shown in evidence
    authority: str                 # models.Source.authority: official | research_repro | derived
    implemented: bool              # True = live retrieval; False = architecture-ready stub
    default_model: str = ""        # NWP model reported when none is explicitly configured
    note: str = ""                 # one-line honesty note for docs / /health
    factory: Optional[Callable[[], Any]] = None


def _open_meteo_factory() -> Any:
    # Imported lazily so the registry module never pulls HTTP at import and so weather.py's
    # singleton logic remains the single owner of provider construction details.
    from backend.services.weather import OpenMeteoProvider

    return OpenMeteoProvider()


REGISTRY: Dict[str, ProviderInfo] = {
    "open-meteo": ProviderInfo(
        key="open-meteo",
        source_label="Open-Meteo",
        authority="research_repro",
        implemented=True,
        default_model="best_match",
        note="CURRENT/live: Open-Meteo (blend of NWP models, model/reanalysis). Optional "
             "model selection via OPEN_METEO_MODEL. Not the official national source.",
        factory=_open_meteo_factory,
    ),
    "imd": ProviderInfo(
        key="imd",
        source_label="IMD",
        authority="official",
        implemented=False,
        default_model="",
        note="ARCHITECTURE-READY stub: India Meteorological Department, the intended authoritative "
             "source; API access pending approval. fetch() raises UpstreamError (no fabricated data).",
        factory=IMDStubProvider,
    ),
    "gfs": ProviderInfo(
        key="gfs",
        source_label="NOAA GFS",
        authority="research_repro",
        implemented=False,
        default_model="gfs",
        note="ARCHITECTURE-READY stub: direct NOAA GFS adapter not wired. GFS fields can be reached "
             "via OPEN_METEO_MODEL=gfs_seamless (Open-Meteo proxy). fetch() raises UpstreamError.",
        factory=GFSStubProvider,
    ),
    "wrf": ProviderInfo(
        key="wrf",
        source_label="WRF",
        authority="research_repro",
        implemented=False,
        default_model="wrf",
        note="ARCHITECTURE-READY stub: regional WRF mesoscale grid/endpoint not provided in this "
             "build. fetch() raises UpstreamError.",
        factory=WRFStubProvider,
    ),
}


def normalize_key(name: Optional[str]) -> str:
    return (name or "open-meteo").strip().lower()


def get_info(key: Optional[str]) -> Optional[ProviderInfo]:
    return REGISTRY.get(normalize_key(key))


def available_keys() -> List[str]:
    return list(REGISTRY.keys())


def implemented_keys() -> List[str]:
    return [k for k, info in REGISTRY.items() if info.implemented]


def source_label(key: Optional[str]) -> str:
    """models.Source.name for a provider. Unknown keys are passed through verbatim (they may be a
    custom test/future provider) rather than crash; selection itself still fails in create_provider."""
    info = get_info(key)
    return info.source_label if info else normalize_key(key)


def source_authority(key: Optional[str]) -> str:
    """models.Source.authority for a provider. Defaults to research_repro (never official)."""
    info = get_info(key)
    return info.authority if info else "research_repro"


def active_model(key: Optional[str] = None) -> str:
    """The NWP model to report on WeatherBundle.model / /health.

    For Open-Meteo an explicit OPEN_METEO_MODEL wins; otherwise the provider's default
    ("best_match"). Stubs report their model id (only metadata — they do not fetch)."""
    k = normalize_key(key if key is not None else config.WEATHER_PROVIDER)
    info = get_info(k)
    if k == "open-meteo" and getattr(config, "OPEN_METEO_MODEL", ""):
        return config.OPEN_METEO_MODEL.strip()
    return info.default_model if info else ""


def create_provider(key: Optional[str]):
    """Factory used by weather.get_provider(). Unknown keys raise the project's UpstreamError-like
    RuntimeError at selection time (kept identical to the previous behaviour for unrecognised
    WEATHER_PROVIDER values); registered-but-unimplemented keys return a stub that raises the
    standard UpstreamError on fetch (a graceful, honest failure path)."""
    k = normalize_key(key)
    info = get_info(k)
    if info is None:
        raise RuntimeError(
            f"WEATHER_PROVIDER={k!r} is not registered. Known providers: "
            f"{', '.join(available_keys())}. Only {', '.join(implemented_keys())} are implemented; "
            "the rest are architecture-ready stubs."
        )
    assert info.factory is not None  # every registered provider has a factory (real or stub)
    return info.factory()


def providers_report(active_key: Optional[str] = None) -> Dict[str, Any]:
    """Small, secret-free summary for /health: which providers exist and which is live."""
    k = normalize_key(active_key if active_key is not None else config.WEATHER_PROVIDER)
    return {
        "active": k,
        "active_model": active_model(k),
        "implemented": implemented_keys(),
        "stubs": [key for key, info in REGISTRY.items() if not info.implemented],
        "all": [
            {
                "key": info.key,
                "source_label": info.source_label,
                "authority": info.authority,
                "status": "live" if info.implemented else "stub_not_implemented",
                "default_model": info.default_model,
            }
            for info in REGISTRY.values()
        ],
    }


__all__ = [
    "WeatherProvider",
    "ProviderInfo",
    "REGISTRY",
    "normalize_key",
    "get_info",
    "available_keys",
    "implemented_keys",
    "source_label",
    "source_authority",
    "active_model",
    "create_provider",
    "providers_report",
]


# UpstreamError re-exported so callers that import the registry can reference the failure type
# without reaching for http_client (the stubs raise it internally; kept here for completeness).
__all__.append("UpstreamError")
