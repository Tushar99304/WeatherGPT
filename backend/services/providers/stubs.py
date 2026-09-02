"""
stubs.py — Phase 5A architecture-ready providers: IMD, GFS, WRF.

These are deliberately NOT integrations. Each class matches the WeatherProvider interface
(``name`` + ``fetch(...)`` with the same keyword signature as services/weather.py), so the
registry can hand them out and the rest of the pipeline treats them exactly like any provider —
but calling ``fetch`` raises the project's standard ``UpstreamError``. That is the SAME failure
convention a live upstream outage uses, which means:

  * main.py catches it and abstains with an honest "upstream weather source failed" message;
  * the LLM is never asked to invent the missing numbers;
  * Evidence Quality drops to LOW and the advisory goes UNCERTAIN — no fake HIGH from a stub.

Nothing here is marked as live: registry status is "stub" and /health reports them as not
implemented. When a real source exists (e.g. an approved IMD API key, a direct GFS THREDDS/OpenDAP
adapter, or a local WRF grid), a real provider class replaces the stub — the registry key and the
downstream pipeline stay untouched.
"""

from __future__ import annotations

from typing import Optional

from backend.services.http_client import UpstreamError


class _StubWeatherProvider:
    """Shared behaviour for providers that are interfaces only.

    Concrete subclasses set ``name`` and a human-readable ``_reason``; they deliberately do not
    perform any network I/O and do not return a WeatherBundle.
    """

    name: str = "stub"
    source_label: str = "Unimplemented provider"
    _reason: str = "provider not implemented in this build"

    async def fetch(
        self,
        latitude: float,
        longitude: float,
        *,
        timeframe: str = "now",
        timezone: Optional[str] = None,
        target_date: Optional[str] = None,
        utc_offset_seconds: Optional[int] = None,
    ):
        raise UpstreamError(
            self.name,
            f"{self.source_label} is ARCHITECTURE-READY only (no live data source is wired into "
            f"this build): {self._reason}. Abstaining rather than fabricating weather.",
        )


class IMDStubProvider(_StubWeatherProvider):
    """India Meteorological Department — the intended authoritative national source.

    Direct API access is pending approval. Until then this honours the interface and fails loudly
    via UpstreamError; the pipeline never relabels Open-Meteo (or any NWP blend) as IMD/official."""

    name = "imd"
    source_label = "IMD"
    _reason = "IMD API access is pending approval"


class GFSStubProvider(_StubWeatherProvider):
    """NOAA GFS — global NWP model.

    Note: GFS fields CAN already be reached indirectly through the Open-Meteo provider via
    ``OPEN_METEO_MODEL=gfs_seamless`` (Open-Meteo acts as a documented, key-free proxy). A direct
    GFS adapter (THREDDS/OpenDAP) is not wired in this phase, so this stub stands in for that
    slot and never claims live data on its own."""

    name = "gfs"
    source_label = "NOAA GFS"
    _reason = "no direct GFS adapter (THREDDS/OpenDAP) is wired in this build"


class WRFStubProvider(_StubWeatherProvider):
    """Weather Research and Forecasting model — regional mesoscale NWP.

    Requires running a local WRF grid or an operator-provided endpoint; neither exists in this
    build, so this is an interface-only slot."""

    name = "wrf"
    source_label = "WRF"
    _reason = "no regional WRF grid/endpoint is provided in this build"
