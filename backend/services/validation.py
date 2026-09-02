"""
validation.py — Phase 3: does this Evidence object actually hold up?

Design (deliberately small):
  * A handful of named, deterministic checks. No framework, no rule engine, no plugin registry —
    each check is a function that returns (verdict, failure_messages) so a judge can be shown the
    one-line reason for any decision.
  * Validation is a FILTER, not a forecast. It can say "this timestamp is 372 min old and our
    limit is 90" or "temperature 99 °C is outside any plausible range". It never claims that
    passing these checks makes the weather *meteorologically correct* — that is out of scope for
    a retrieval layer, and pretending otherwise would be the kind of overclaim this project is
    built to avoid.
  * It EXTENDS the Validation object that evidence.py already populated in Phase 2 (alert
    checks live there), so `checks_run` / `warnings` from Phase 2 survive untouched.

Ordering note: validation runs after retrieval, so every failure here is about evidence we
already have. Missing evidence is a failure of `complete`, not an exception.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from backend import config
from backend.models import AlertsEvidence, Evidence, ForecastDay, ResolvedLocation, Validation, WeatherBundle

UTC = dt.timezone.utc


def reference_now() -> dt.datetime:
    """The single wall clock for every time-based validation/quality judgement (aware UTC).

    All date/freshness decisions must read time through HERE rather than calling
    ``datetime.now()`` directly. It routes through ``services.weather._utc_now`` — the very
    clock ``validate_freshness`` already uses for the provider-timestamp age, and the one the
    test suites freeze (see the autouse ``_fixed_clock`` fixtures that monkeypatch
    ``weather._utc_now``). Before Phase 5A, ``validate_labeling`` and the alert-only /
    retrieval-age freshness paths in ``quality.py`` called ``datetime.now()`` themselves, so
    their verdicts drifted with the real calendar day (the two date-skew test failures).
    Production behaviour is unchanged: ``weather._utc_now()`` itself returns ``datetime.now(UTC)``.
    """
    from backend.services import weather as _weather

    return _weather._utc_now()


# WMO codes that mean "the weather itself is the hazard". Documented engineering heuristic for
# the MVP: a coarse mapping of the codes Open-Meteo reports to a hazard flag. NOT an official
# IMD classification, and not used to classify severity — severity comes from SACHET alerts.
SEVERE_WMO_CODES = {
    55, 57,                       # dense drizzle, freezing drizzle
    61, 63, 65,                   # slight / moderate / heavy rain
    66, 67,                       # freezing rain
    71, 73, 75, 77,               # snow
    80, 81, 82,                   # rain showers
    85, 86,                       # snow showers
    95, 96, 99,                   # thunderstorm (with hail)
}
# Codes that reduce visibility enough to matter for a travel question.
LOW_VIS_WMO_CODES = {45, 48}


def _utc(value: Any) -> Optional[dt.datetime]:
    """Tolerant aware-datetime parse (shared with the alert engine's semantics)."""
    from backend.services.alerts import parse_any_datetime

    return parse_any_datetime(value if isinstance(value, str) else None)


# --------------------------------------------------------------------------- #
# A. location
# --------------------------------------------------------------------------- #
def validate_location(loc: Optional[ResolvedLocation]) -> Tuple[bool, List[str]]:
    """Coordinates must exist and be real coordinates. Nothing else is judged here — whether the
    RIGHT place was picked is the geocoder's `resolution_note`, shown to the user."""
    if loc is None:
        return False, ["no resolved location (nothing can be grounded on coordinates)"]
    failures: List[str] = []
    if loc.latitude is None or loc.longitude is None:
        failures.append("location has no coordinates")
    else:
        if not -90.0 <= float(loc.latitude) <= 90.0:
            failures.append(f"latitude {loc.latitude} outside [-90, 90]")
        if not -180.0 <= float(loc.longitude) <= 180.0:
            failures.append(f"longitude {loc.longitude} outside [-180, 180]")
    if not (loc.name or "").strip():
        failures.append("resolved location carries no name to show the user")
    return not failures, failures


# --------------------------------------------------------------------------- #
# B. timestamps / freshness
# --------------------------------------------------------------------------- #
def validate_freshness(
    bundle: Optional[WeatherBundle], *, max_staleness_min: Optional[float] = None,
    now: Optional[dt.datetime] = None,
) -> Tuple[Optional[bool], Optional[float], List[str]]:
    """Two distinct ages, because they fail for different reasons:

    * `source_age_min`  — how old the provider's own timestamp is (`weather.current.time`,
      re-anchored to UTC through the API offset, exactly as weather.minutes_since_source does).
      This is the one that matters: an hour-old "current" value is not current.
    * `retrieved_at_utc` — when WE fetched it. Must exist and be timezone-aware/UTC.

    Historical evidence is exempt from the staleness rule on purpose: asking about 2026-06-14
    returns data that is *correctly* old. Marking that stale would be a category error.
    """
    limit = float(config.WEATHER_MAX_STALENESS_MIN if max_staleness_min is None else max_staleness_min)
    failures: List[str] = []
    if bundle is None:
        # Nothing to date. Whether that is a PROBLEM depends on the question (an alert-only query
        # needs no weather block), so it is completeness' call, not a freshness failure here.
        return None, None, []

    retrieved = _utc(bundle.retrieved_at_utc)
    if retrieved is None:
        failures.append(f"retrieved_at_utc missing or not parseable ({bundle.retrieved_at_utc!r})")
    elif retrieved.tzinfo is None or retrieved.utcoffset() is None:
        failures.append("retrieved_at_utc is naive — freshness cannot be compared across clocks")

    source_age: Optional[float] = None
    if bundle.kind == "historical":
        return True, None, failures          # by definition not "stale"; only the timestamp exists
    if bundle.current is not None and bundle.current.time:
        from backend.services import weather as weather_service

        # Reuse the Phase-1 helper so freshness means ONE thing in this codebase. It is
        # wall-clock based, so tests inject `now` through _stubbed clock or fixed timestamps.
        if now is not None:
            original = weather_service._utc_now
            weather_service._utc_now = lambda: now  # type: ignore[assignment]
            try:
                source_age = weather_service.minutes_since_source(bundle.current, bundle.api_utc_offset_seconds)
            finally:
                weather_service._utc_now = original  # type: ignore[assignment]
        else:
            source_age = weather_service.minutes_since_source(bundle.current, bundle.api_utc_offset_seconds)
    elif bundle.retrieved_at_utc:
        source_age = None  # forecast-only answer: age of the day block is checked in validate_labeling
    else:
        failures.append("no timestamp of any kind on the weather evidence")

    if source_age is not None and source_age > limit:
        failures.append(
            f"provider timestamp is {source_age:.0f} min old, over the "
            f"{limit:.0f} min limit (WEATHER_MAX_STALENESS_MIN)"
        )
    fresh = not failures
    return fresh, source_age, failures


# --------------------------------------------------------------------------- #
# C. plausible values
# --------------------------------------------------------------------------- #
def _field_ranges() -> List[Tuple[str, str, str]]:
    """(block, model field, config.RANGES key). Only fields that exist on the models — no
    invented variables. `units` are already normalised to °C / mm / km/h by weather.py."""
    return [
        ("current", "temperature_c", "temperature_c"),
        ("current", "apparent_temperature_c", "apparent_temperature_c"),
        ("current", "precipitation_mm", "precipitation_mm"),
        ("current", "wind_speed_kmh", "wind_kmh"),
        ("current", "humidity_pct", "humidity_pct"),
        ("day", "temperature_max_c", "temperature_c"),
        ("day", "temperature_min_c", "temperature_c"),
        ("day", "precipitation_sum_mm", "precipitation_mm"),
        ("day", "precipitation_probability_max_pct", "precip_probability_pct"),
        ("day", "wind_speed_max_kmh", "wind_kmh"),
    ]


def validate_timestamp_presence(
    bundle: Optional[WeatherBundle], *, timeframe: str = "now",
    alerts: Optional[AlertsEvidence] = None,
) -> Tuple[bool, List[str]]:
    """Every answer must be able to say WHEN it is from. Acceptable, in order of preference:
    our own retrieval time; a timestamp on the block we are actually answering from (the current
    block for "now", the selected day's date for a day question); or, for an alert-only question,
    the SACHET check time. A day block that is not the one being answered from does not count —
    'Today 2026-09-01' is not a timestamp for a 'tomorrow' answer."""
    if bundle is None:
        if alerts is not None and alerts.state == "checked" and _utc(alerts.checked_at_utc):
            return True, []
        return False, ["no timestamp on any evidence block"]
    if _utc(bundle.retrieved_at_utc) if bundle.retrieved_at_utc else None:
        return True, []
    if timeframe == "now" or not timeframe:
        if bundle.current is not None and bundle.current.time:
            return True, []
    else:
        day = answered_day(bundle, timeframe)
        if day is not None and day.date:
            return True, []
    if bundle.past_days and bundle.past_days[-1].date:
        return True, []
    return False, [
        "no retrieval timestamp, and the block this question is answered from carries no timestamp"
    ]


def _no_weather_is_fine(intent: str) -> bool:
    """An alert-only question does not need a weather block, so the weather-shaped checks return
    "not judgeable" (None, no failure) instead of crying wolf. The intents that DO need weather
    are covered by validate_completeness plus the explicit rule in validate_evidence."""
    return intent == "official_alert"


def validate_values(bundle: Optional[WeatherBundle], *, required: bool = True) -> Tuple[Optional[bool], List[str]]:
    """Sanity filter against config.RANGES. A value outside these ranges is a parsing or unit
    bug, and answering with it would be worse than abstaining. A value inside them is NOT
    certified correct — that is not what this checks, and the label says so."""
    if bundle is None:
        return None, (["no weather block to check"] if required else [])
    failures: List[str] = []
    blocks = [("current", bundle.current)]
    for name in ("today", "tomorrow", "target_day"):
        blocks.append((name, getattr(bundle, name)))
    blocks += [(f"past_days[{i}]", d) for i, d in enumerate(bundle.past_days or [])]
    for block_name, block in blocks:
        if block is None:
            continue
        for b, field, range_key in _field_ranges():
            if b != ("current" if block_name == "current" else "day"):
                continue
            value = getattr(block, field, None)
            if value is None:
                continue  # absent is a completeness question, not a plausibility one
            lo, hi = config.RANGES[range_key]
            if not (lo <= float(value) <= hi):
                failures.append(
                    f"{block_name}.{field}={value} outside plausible range [{lo}, {hi}] ({range_key})"
                )
    return (not failures), failures


# --------------------------------------------------------------------------- #
# D. current vs forecast labelling
# --------------------------------------------------------------------------- #
def _local_date(now: dt.datetime, offset_seconds: Optional[int]) -> dt.date:
    return (now + dt.timedelta(seconds=int(offset_seconds or 0))).date()


def validate_labeling(
    bundle: Optional[WeatherBundle],
    intent: str,
    timeframe: str,
    target_date: Optional[str] = None,
    *,
    now: Optional[dt.datetime] = None,
) -> Tuple[Optional[bool], List[str], List[str]]:
    # (bundle None + alert-only intent -> (None, [], []) handled below)
    """The mistake this prevents is subtle but user-visible: answering "tomorrow" from a block
    that is actually today, or answering "now" from a forecast. Phase 1 built the labels
    (`ForecastDay.label`, `is_forecast`, `WeatherBundle.kind`); here we verify we are still
    honouring them, and fail loudly when we are not."""
    failures: List[str] = []
    warnings: List[str] = []
    if bundle is None:
        return None, ([] if _no_weather_is_fine(intent) else ["no weather block to label"]), warnings
    # Route through the single reference clock (see reference_now): this check must agree with
    # the frozen clock the freshness check uses, or a "today" block judged current by freshness
    # can simultaneously be "in the past" by labelling — the Phase-3 date-skew bug.
    now = now or reference_now()
    local_today = _local_date(now, bundle.api_utc_offset_seconds)

    if timeframe == "now":
        if bundle.current is None:
            failures.append("asked for current conditions but the evidence has no current block")
        elif not bundle.current.time:
            failures.append("current block carries no timestamp, so it cannot be called 'current'")
        if bundle.kind == "historical":
            warnings.append("current question answered from an archive call — values are historical")

    if timeframe in {"today", "tomorrow", "specific_day"}:
        day: Optional[ForecastDay] = answered_day(bundle, timeframe, target_date)
        if day is None:
            failures.append(f"timeframe '{timeframe}' requested but the evidence has no matching day block")
        else:
            if target_date and day.date != target_date:
                failures.append(f"requested {target_date} but the day block is dated {day.date}")
            try:
                day_date = dt.date.fromisoformat(day.date)
            except ValueError:
                failures.append(f"day block date {day.date!r} is not ISO, cannot check the timeframe")
            else:
                if timeframe == "tomorrow" and day_date <= local_today:
                    failures.append(
                        f"'Tomorrow' block is dated {day.date}, which is not after the local date {local_today}"
                    )
                if timeframe == "today" and day_date < local_today:
                    warnings.append(f"'Today' block is dated {day.date}, earlier than the local date {local_today}")
                if day.is_forecast and day_date < local_today:
                    failures.append(f"{day.date} is in the past but is flagged is_forecast=True")
                if not day.is_forecast and day_date > local_today:
                    failures.append(f"{day.date} is in the future but is not flagged as a forecast")
    if timeframe == "past":
        used = answered_day(bundle, timeframe)
        if used is None:
            failures.append("past weather requested but the evidence contains no day block")
        elif bundle.kind != "historical":
            failures.append(
                f"past question answered with kind={bundle.kind!r}; historical evidence must be labelled"
            )
        elif used.is_forecast:
            failures.append(f"{used.date} is a past date but is flagged is_forecast=True")
    return (not failures), failures, warnings


def answered_day(
    bundle: Optional[WeatherBundle], timeframe: str, target_date: Optional[str] = None
) -> Optional[ForecastDay]:
    """The single day block this answer is about. Kept in validation (not advisory) because both
    completeness and labelling checks must agree on it — if they picked different blocks, one of
    them would silently validate the wrong day."""
    if bundle is None:
        return None
    if timeframe in {"past"} or bundle.kind == "historical":
        return bundle.target_day or (bundle.past_days[-1] if bundle.past_days else None)
    if timeframe == "today":
        return bundle.today
    # STRICT on purpose: if a "tomorrow" answer is missing its tomorrow block, falling back to
    # today would silently answer a different question. That is a failure, not a substitution.
    if timeframe == "tomorrow":
        return bundle.tomorrow
    if timeframe == "specific_day":
        return bundle.target_day if target_date else bundle.today
    return bundle.today


# --------------------------------------------------------------------------- #
# E. completeness (per intent — irrelevant fields are never penalised)
# --------------------------------------------------------------------------- #
def required_fields(intent: str, timeframe: str) -> List[str]:
    """What the ANSWER needs, not what the provider happens to send. Kept as a plain function
    returning names so the breakdown is readable and Phase 4 can reuse it for grounding."""
    if intent == "official_alert":
        return ["location", "alerts_state", "alert_source", "alert_authority"]
    if intent == "historical_climate" or timeframe == "past":
        return ["location", "retrieved_at_utc", "day_date", "day_precip"]
    if timeframe == "now":
        return ["location", "current_time", "current_temperature", "current_condition", "weather_source"]
    if intent == "advisory_risk":
        return ["location", "retrieved_at_utc", "alert_state", "day_or_current", "weather_source"]
    return ["location", "retrieved_at_utc", "day_date", "day_precip", "weather_source"]


def _present(ev: Evidence, field: str, timeframe: str = "now") -> bool:
    w, a = ev.weather, ev.alerts
    if field == "location":
        return ev.location is not None
    if field == "retrieved_at_utc":
        return bool(w and w.retrieved_at_utc)
    if field == "current_time":
        return bool(w and w.current and w.current.time)
    if field == "current_temperature":
        return bool(w and w.current and w.current.temperature_c is not None)
    if field == "current_condition":
        return bool(w and w.current and (w.current.condition or w.current.weather_code is not None))
    if field == "weather_source":
        # Provider-agnostic: ANY comparable, non-geocoding weather source counts. Geocoding
        # entries (type="geocoding") and official alerts are not weather evidence providers.
        from backend.services.quality import COMPARABLE_SOURCE_TYPES

        return any(
            s.type in COMPARABLE_SOURCE_TYPES and (s.timestamp or s.url) for s in ev.sources
        )
    if field == "day_date":
        day = answered_day(w, timeframe) if w else None
        return bool(day and day.date)
    if field == "day_precip":
        day = answered_day(w, timeframe) if w else None
        if day is None:
            return False
        return day.precipitation_sum_mm is not None or day.precipitation_probability_max_pct is not None
    if field == "day_or_current":
        return bool(w and (w.current or w.today or w.tomorrow or w.target_day))
    if field == "alerts_state":
        return a.state in {"checked", "unavailable", "not_checked"}
    if field == "alert_state":
        return a.state in {"checked", "unavailable", "not_checked"}
    if field == "alert_source":
        return any(s.name == "NDMA SACHET" for s in ev.sources) or a.state != "checked"
    if field == "alert_authority":
        return any(s.name == "NDMA SACHET" and s.authority == "official" for s in ev.sources) or a.state != "checked"
    return False


def missing_required_fields_for(ev: Evidence, intent: str, timeframe: str) -> List[str]:
    """Names (not prose) so Phase 3 can score a FRACTION of completeness and Phase 4 can check
    that the answer only used fields that exist."""
    return [f for f in required_fields(intent, timeframe) if not _present(ev, f, timeframe)]


def validate_completeness(ev: Evidence, intent: str, timeframe: str) -> Tuple[Optional[bool], List[str]]:
    missing = missing_required_fields_for(ev, intent, timeframe)
    if missing:
        return False, [f"required for this question but absent: {', '.join(missing)}"]
    return True, []


# --------------------------------------------------------------------------- #
# F + G. alert availability and alert integrity
# --------------------------------------------------------------------------- #
def validate_alerts(alerts: AlertsEvidence) -> Tuple[Optional[bool], List[str], List[str]]:
    """Three states, three different meanings (Phase 2's contract, enforced here):
      checked      -> we consulted SACHET; zero relevant items is a POSITIVE result
      unavailable  -> we could not look; must NEVER be read as "no alert exists"
      not_checked  -> we did not try (disabled / short-circuited) -> verdict unknown, not False
    """
    failures: List[str] = []
    warnings: List[str] = []
    if alerts.state == "unavailable":
        # Deliberately a WARNING, not a failure: "Pune weather right now?" is still answerable
        # while SACHET is down. The outage (a) caps Evidence Quality at MEDIUM in quality.py,
        # (b) makes the advisory UNCERTAIN, and (c) becomes a hard failure only when the question
        # itself was about alerts — that rule lives in validate_evidence.
        return False, [], [
            "official alert source could not be consulted, so the alert status is UNKNOWN "
            "(this is not evidence that no alert exists)"
            + (f": {alerts.error}" if alerts.error else "")
        ]
    if alerts.state == "not_checked":
        return None, [], ["alerts were not consulted for this request (not the same as 'no alert')"]

    # checked: every attached alert must hold up on its own terms.
    for a in alerts.items:
        if a.source != "NDMA SACHET":
            failures.append(f"alert {a.alert_id} carries source {a.source!r}, not NDMA SACHET")
        if a.authority != "official":
            failures.append(f"alert {a.alert_id} carries authority {a.authority!r}, not official")
        if a.relevance.status != "relevant":
            failures.append(
                f"alert {a.alert_id} is attached although relevance says {a.relevance.status!r} "
                "(only relevant alerts may be presented)"
            )
        if a.validity == "expired":
            failures.append(f"alert {a.alert_id} is EXPIRED but sits in the active list")
        if a.validity == "active":
            eff, exp = _utc(a.effective_at), _utc(a.expires_at)
            if exp is None:
                failures.append(f"alert {a.alert_id} claims 'active' with no parseable expiry")
            elif eff is not None and exp <= eff:
                failures.append(f"alert {a.alert_id} has expires <= effective ({a.effective_at} .. {a.expires_at})")
            if not (a.headline or a.event):
                warnings.append(f"alert {a.alert_id} has neither headline nor event text")
        if a.relevance.level == "L4_geometry" and not a.relevance.geometry_available:
            failures.append(f"alert {a.alert_id} claims geometry relevance with no geometry present")
    if alerts.rejected_uncertain:
        warnings.append(
            f"{alerts.rejected_uncertain} official alert(s) could not be tied to this location; "
            "they are neither attached nor dismissed"
        )
    return (not failures), failures, warnings


def alert_ids_present(alerts: AlertsEvidence, alert_ids: List[str]) -> List[str]:
    """Which referenced ids do NOT exist among the verified-relevant alerts.

    Phase 3 uses this to check the advisory's own references; Phase 4's grounding verifier uses
    the same function on the LLM's answer, so "the model invented an alert id" and "our engine
    referenced one it should not have" are caught by one rule, not two.
    """
    known = {a.alert_id for a in alerts.items if a.alert_id}
    return [i for i in alert_ids if i not in known]


def advisory_references_ok(ev: Evidence) -> Tuple[bool, List[str]]:
    """Post-advise integrity gate: an advisory may only cite alerts that are in the evidence."""
    if ev.advisory is None:
        return True, []
    bad = alert_ids_present(ev.alerts, ev.advisory.alert_ids)
    if bad:
        return False, [f"advisory references alert(s) absent from verified evidence: {', '.join(bad)}"]
    return True, []


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
def validate_evidence(ev: Evidence, *, now: Optional[dt.datetime] = None) -> Validation:
    """Runs every check, EXTENDING whatever evidence.py/_attach_alerts already recorded.

    `ok`         — no validation failure at all.
    `sufficient` — ok AND the fields this question needs are present. Abstention keys off
                   `sufficient`, so an alert question that could not consult SACHET abstains on
                   the alert part while a weather question is unaffected by alert outages.
    """
    base = ev.validation.model_copy(deep=True)
    checks: List[str] = list(base.checks_run)
    failures: List[str] = list(base.failures)
    warnings: List[str] = list(base.warnings)

    intent = str(ev.request.get("intent") or "forecast_current")
    timeframe = str(ev.request.get("timeframe") or "now")
    target_date = ev.request.get("target_date")

    ok_loc, f = validate_location(ev.location)
    base.location_resolved = ok_loc
    failures += f
    checks.append("location_sanity")

    fresh, age, f = validate_freshness(ev.weather, now=now)
    base.fresh = fresh
    base.source_age_minutes = None if age is None else round(age, 1)  # payload stays readable
    failures += f
    checks.append("freshness" if ev.weather else "freshness_skipped_no_weather")

    plausible, f = validate_values(ev.weather, required=not _no_weather_is_fine(intent))
    base.values_plausible = plausible
    failures += f
    checks.append("value_ranges")

    labelled, f, wl = validate_labeling(ev.weather, intent, timeframe, target_date, now=now)
    base.labeling_consistent = labelled
    failures += f
    warnings += wl
    checks.append("current_vs_forecast_labelling")

    complete, f = validate_completeness(ev, intent, timeframe)
    base.complete = complete
    failures += f
    checks.append("completeness_for_intent")

    alerts_valid, fa, wa = validate_alerts(ev.alerts)
    base.alerts_valid = alerts_valid
    # integrity is about alerts WE ATTACHED; with nothing consulted there is nothing to judge
    base.alert_integrity = (not fa) if ev.alerts.state == "checked" else None
    failures += fa
    warnings += wa
    if "alerts_consulted" not in checks and "alerts_unavailable" not in checks and "alerts_not_checked" not in checks:
        checks.append("alert_availability")

    stamped, f = validate_timestamp_presence(ev.weather, timeframe=timeframe, alerts=ev.alerts)
    base.timestamp_present = stamped
    failures += f
    checks.append("timestamp_present")

    # A question ABOUT official alerts cannot be answered from weather alone.
    if intent == "official_alert" and ev.alerts.state != "checked":
        # The one case where an alert outage IS fatal: the user asked about alerts.
        failures.append(
            "the question was about official alerts, so 'alerts not consulted' is a hard failure "
            "here, not a footnote"
        )
    # and a question about weather cannot be answered when the weather block is missing
    if ev.weather is None and intent != "official_alert":
        failures.append("no weather evidence retrieved")

    base.checks_run = checks
    base.failures = failures
    base.warnings = warnings
    base.ok = not failures
    base.sufficient = base.ok and bool(complete) and ok_loc
    if not base.sufficient:
        base.warnings.append(
            "insufficient evidence: the pipeline will abstain or downgrade its own confidence "
            "instead of presenting these numbers as trustworthy"
        )
    return base


def summary(v: Validation) -> Dict[str, Any]:
    """Compact trace view (kept separate so main.py's stage dict stays readable)."""
    return {
        "ok": v.ok,
        "sufficient": v.sufficient,
        "fresh": v.fresh,
        "complete": v.complete,
        "values_plausible": v.values_plausible,
        "labeling_consistent": v.labeling_consistent,
        "alerts_valid": v.alerts_valid,
        "source_age_minutes": None if v.source_age_minutes is None else round(v.source_age_minutes, 1),
        "checks_run": v.checks_run,
        "failures": v.failures,
        "warnings": v.warnings,
    }
