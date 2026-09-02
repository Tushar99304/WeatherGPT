"""
providers/ — Phase 5A weather-provider registry.

This package does NOT replace the existing provider architecture in ``services/weather.py``;
it extends it:

  * The ``WeatherProvider`` Protocol and the fully-working ``OpenMeteoProvider`` stay in
    ``services/weather.py`` (nothing about the current/default provider moves).
  * ``registry.py`` adds a small, data-driven catalogue of every provider the system *knows
    about*, whether it is actually implemented or only an interface, plus the factory that the
    existing ``weather.get_provider()`` delegates to.
  * ``stubs.py`` holds the architecture-ready providers (IMD / GFS / WRF). They satisfy the same
    ``WeatherProvider`` shape but raise the project's normal ``UpstreamError`` on ``fetch`` — the
    same convention a live outage uses — so selecting one degrades gracefully to abstain/fallback
    and never fabricates a number. Nothing here is presented as live data.

Provider status, honestly:

  CURRENT (implemented, live):   open-meteo
  ARCHITECTURE-READY (stub):     imd   (national meteorological service; API access pending)
  ARCHITECTURE-READY (stub):     gfs   (NOAA GFS; Open-Meteo *can* proxy it via the `models=`
                                       param, but a direct GFS adapter is not wired here)
  ARCHITECTURE-READY (stub):     wrf   (regional mesoscale model; no public endpoint in this build)
"""

from __future__ import annotations

from backend.services.providers.registry import (
    REGISTRY,
    ProviderInfo,
    active_model,
    available_keys,
    create_provider,
    get_info,
    implemented_keys,
    providers_report,
    source_authority,
    source_label,
)
from backend.services.providers.stubs import (
    GFSStubProvider,
    IMDStubProvider,
    WRFStubProvider,
)

__all__ = [
    "REGISTRY",
    "ProviderInfo",
    "active_model",
    "available_keys",
    "create_provider",
    "get_info",
    "implemented_keys",
    "providers_report",
    "source_authority",
    "source_label",
    "GFSStubProvider",
    "IMDStubProvider",
    "WRFStubProvider",
]
