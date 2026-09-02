"""
evidence.py — assembles the normalized Evidence object (the ONLY payload the LLM gets).

Phase 1 scope: fill location + weather + sources + timestamps + retrieval metadata,
and record pipeline stages. Validation/quality scoring is added on Tue night and just
sets the fields already reserved in models.py — that is why `sources` is built here now:
source + timestamp honesty is the whole point of the project.
"""

from __future__ import annotations

from typing import Optional

from backend import config
from backend.models import (
    AlertsEvidence,
    Evidence,
    GeocodeResult,
    ParsedQuery,
    Source,
    WeatherBundle,
)
from backend.services import providers

STALE_IF_NOT_RETRIEVED_S = 600  # our own retrieval age, separate from provider timestamp age


def _attach_alerts(ev: Evidence, alerts: AlertsEvidence) -> None:
    """Alerts become STRUCTURED evidence (never a raw text dump for the LLM):
      * ev.alerts.items       -> only alerts verified relevant to THIS location
      * ev.sources            -> one NDMA SACHET source entry, authority="official"
      * ev.validation         -> alert-specific notes, including "could not verify" states
    Counts of rejected/uncertain alerts stay inside ev.alerts so the abstention logic in
    Phase 3 can see WHY nothing was attached.
    """
    if alerts.state == "checked":
        ev.sources.append(
            Source(
                name="NDMA SACHET",
                type="official_alert",
                timestamp=alerts.checked_at_utc,
                url=alerts.feeds_considered[0] if alerts.feeds_considered else None,
                authority="official",
                note=(
                    f"{len(alerts.items)} alert(s) verified relevant; "
                    f"{alerts.rejected_not_relevant} explicitly unrelated, "
                    f"{alerts.rejected_uncertain} unconfirmable, "
                    f"{len(alerts.recent_expired)} expired; feeds={len(alerts.feeds_considered)}"
                ),
            )
        )
        ev.validation.alerts_valid = True
        ev.validation.checks_run.append("alerts_consulted")
        if not alerts.items:
            ev.validation.warnings.append(
                "SACHET was checked: no active official alert is verifiably tied to this "
                "location (that is NOT the same as 'no alert exists')"
            )
        else:
            ev.validation.checks_run.append("safety_critical_alert_present")
    elif alerts.state == "unavailable":
        ev.validation.alerts_valid = False
        ev.validation.checks_run.append("alerts_unavailable")
        ev.validation.warnings.append(
            "official alert source could not be consulted"
            + (f": {alerts.error}" if alerts.error else "")
        )
    else:  # not_checked / disabled
        ev.validation.checks_run.append("alerts_not_checked")
    if alerts.mode == "fixture_replay":
        ev.validation.warnings.append(
            "ALERT DATA IS A RECORDED FIXTURE (ALERT_FIXTURE_RSS), not a live SACHET pull"
        )


def build_evidence(
    parsed: ParsedQuery,
    geo: GeocodeResult,
    weather: Optional[WeatherBundle],
    alerts: Optional[AlertsEvidence] = None,
) -> Evidence:
    ev = Evidence(
        request={
            "message": parsed.message,
            "intent": parsed.intent,
            "intent_reason": parsed.intent_reason,
            "timeframe": parsed.timeframe,
            "timeframe_reason": parsed.timeframe_reason,
            "target_date": parsed.target_date,
            "location_text": parsed.location_text,
        },
        location=geo.location,
        weather=weather,
        alerts=alerts or AlertsEvidence(),  # not_checked unless the caller actually looked
    )
    if alerts is not None:
        _attach_alerts(ev, alerts)

    # ---- sources: geocoding first, then the weather evidence ------------- #
    ev.sources.append(
        Source(
            name="Open-Meteo Geocoding",
            type="geocoding",
            timestamp=weather.retrieved_at_utc if weather else None,
            authority="research_repro",
            note=geo.location.resolution_note if geo.location else None,
        )
    )
    if weather is not None:
        days = weather.past_days or []
        if weather.current and weather.current.time:
            primary_ts = weather.current.time
        elif weather.target_day is not None:
            primary_ts = weather.target_day.date
        elif days:
            primary_ts = days[-1].date
        else:
            primary_ts = weather.retrieved_at_utc
        period = (
            f"{days[0].date}..{days[-1].date}" if len(days) > 1 else (days[0].date if days else None)
        )
        # Provider-agnostic (Phase 5A): the weather Source's name/authority come from the
        # provider registry (services/providers/), never a hardcoded "Open-Meteo". The geocoding
        # source above is a distinct service and stays labelled Open-Meteo Geocoding regardless of
        # which weather provider is selected.
        provider_key = weather.provider or config.WEATHER_PROVIDER
        src_name = providers.source_label(provider_key)
        src_authority = providers.source_authority(provider_key)  # research_repro at best; never upgraded
        model_used = weather.model or providers.active_model(provider_key)
        model_clause = f" model={model_used}." if model_used else ""
        note = (
            f"Live weather from {src_name} (NWP model/reanalysis blend), provider key "
            f"{provider_key!r};{model_clause} An official national meteorological source (IMD) is "
            "the intended primary; its connector is architecture-ready, not live, and this "
            "blend is never relabelled official. 'current' is 15-min cadence model data."
        )
        ev.sources.append(
            Source(
                name=src_name,
                # A past-date lookup is historical evidence, never "current weather".
                type="historical" if weather.kind == "historical" else "forecast",
                timestamp=primary_ts,
                period=period,
                url=weather.request_url,
                authority=src_authority,  # type: ignore[arg-type]
                note=note,
            )
        )

    # ---- retrieval age check (cheap, keeps us honest before Phase 3) ------ #
    if weather is None:
        ev.status = "abstain"
        ev.abstain_reason = "Weather evidence could not be retrieved, so there is nothing to ground an answer on."
        ev.validation.failures.append("no_weather_evidence")
        return ev

    ev.status = "grounded"
    ev.validation.location_resolved = geo.location is not None
    ev.validation.timestamp_present = bool(weather.current and weather.current.time) or bool(
        weather.past_days
    )
    ev.validation.checks_run.append("phase1_presence_checks_only")
    # Phase 3 note: the *real* validation pass (validate_evidence) runs after this builder in
    # main.py, so evidence.py only records presence checks and leaves failures/quality to it.
    return ev


def provider_label() -> str:
    """Human Source name for the active weather provider (registry-backed, Phase 5A)."""
    return providers.source_label(config.WEATHER_PROVIDER)
