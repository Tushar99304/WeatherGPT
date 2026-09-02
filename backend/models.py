"""
models.py — the schemas that define the whole system.

Design rule for the sprint: the *Evidence* object is the ONLY thing the LLM ever sees,
so it must be serialisable, complete and self-describing (units + timestamps + source).
Everything the pipeline knows about a query lives here or in the small DTOs below.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, computed_field

# Intent labels supported by the MVP router (same names as the architecture doc).
Intent = Literal[
    "forecast_current",   # current conditions and/or a forecast question
    "official_alert",     # "is there an alert for ...?"
    "advisory_risk",      # "should I travel / is it safe to ...?"
    "historical_climate", # past weather / climate normals
    "clarification_needed",
]

Timeframe = Literal["now", "today", "tomorrow", "specific_day", "past", "unspecified"]
StageStatus = Literal["ok", "ambiguous", "unresolved", "error"]


# --------------------------------------------------------------------------- #
# 1. Location
# --------------------------------------------------------------------------- #
class ResolvedLocation(BaseModel):
    """A place that has been pinned down to coordinates, with enough metadata to
    show the user WHICH place we actually answered for (never answer silently)."""

    name: str
    latitude: float
    longitude: float
    country: Optional[str] = None
    country_code: Optional[str] = None
    admin1: Optional[str] = None      # state
    admin2: Optional[str] = None      # district
    timezone: Optional[str] = None
    utc_offset_seconds: Optional[int] = None
    population: Optional[int] = None
    feature_code: Optional[str] = None
    geonames_id: Optional[int] = None
    resolution_note: str = ""         # human-readable "why we picked this"


class GeoCandidate(BaseModel):
    name: str
    latitude: float
    longitude: float
    admin1: Optional[str] = None
    admin2: Optional[str] = None
    country: Optional[str] = None
    country_code: Optional[str] = None
    population: Optional[int] = None


class GeocodeResult(BaseModel):
    """Ambiguity and 'not found' are first-class results — abstention is a feature."""

    status: StageStatus
    query: str
    location: Optional[ResolvedLocation] = None
    candidates: List[GeoCandidate] = Field(default_factory=list)
    clarification: Optional[str] = None
    evidence_gap: Optional[str] = None


# --------------------------------------------------------------------------- #
# 2. Weather evidence (normalised, provider-independent)
# --------------------------------------------------------------------------- #
class CurrentWeather(BaseModel):
    time: str                       # local wall time at the location, as reported by source
    utc_offset_seconds: Optional[int] = None
    interval_seconds: Optional[int] = None
    temperature_c: Optional[float] = None
    apparent_temperature_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    precipitation_mm: Optional[float] = None
    wind_speed_kmh: Optional[float] = None
    wind_direction_deg: Optional[float] = None
    pressure_hpa: Optional[float] = None
    cloud_cover_pct: Optional[float] = None
    weather_code: Optional[int] = None
    condition: Optional[str] = None
    units: Dict[str, str] = Field(default_factory=dict)


class ForecastDay(BaseModel):
    """One daily forecast record. `label` distinguishes it from current weather in the UI."""

    date: str
    label: str = ""                 # "Today" / "Tomorrow" / "Sat 29 Aug 2026"
    is_forecast: bool = True
    temperature_max_c: Optional[float] = None
    temperature_min_c: Optional[float] = None
    precipitation_sum_mm: Optional[float] = None
    precipitation_probability_max_pct: Optional[float] = None
    wind_speed_max_kmh: Optional[float] = None
    weather_code: Optional[int] = None
    condition: Optional[str] = None
    units: Dict[str, str] = Field(default_factory=dict)


class WeatherBundle(BaseModel):
    """What the weather provider returned for this location/request."""

    provider: str = "open-meteo"
    # Phase 5A (additive): which NWP/model actually produced these numbers, e.g. Open-Meteo's
    # "best_match", an explicit OPEN_METEO_MODEL selection ("gfs_seamless", ...), or
    # "reanalysis_archive" for historical calls. Empty for stub providers / older payloads.
    model: str = ""
    kind: Literal["live", "historical"] = "live"
    requested_timeframe: Timeframe = "now"
    retrieved_at_utc: str = ""      # when OUR backend got it (freshness anchor)
    api_utc_offset_seconds: Optional[int] = None
    grid_latitude: Optional[float] = None
    grid_longitude: Optional[float] = None
    elevation_m: Optional[float] = None
    current: Optional[CurrentWeather] = None
    today: Optional[ForecastDay] = None
    tomorrow: Optional[ForecastDay] = None
    target_day: Optional[ForecastDay] = None   # explicit date asked for ("on 2026-08-25")
    past_days: List[ForecastDay] = Field(default_factory=list)
    requested_parameters: List[str] = Field(default_factory=list)
    request_url: str = ""           # exact URL called -> reproducible in front of judges


# --------------------------------------------------------------------------- #
# 3. Alerts (NDMA SACHET CAP — official safety-critical evidence)
# --------------------------------------------------------------------------- #
AlertValidity = Literal["active", "expired", "unknown"]
RelevanceStatus = Literal["relevant", "not_relevant", "uncertain"]
RelevanceLevel = Literal[
    "L1_exact_locality",       # our city/district is named in the alert area text
    "L2_explicit_state_wide",  # "all districts of <state>" / "Rest of <state>" wording
    "L3_state_scoped_subset",  # state matches but the district list excludes us
    "L4_geometry",             # circle/polygon actually supplied and parsed
    "none",
]


class AlertRelevance(BaseModel):
    """Outcome of the conservative relevance ladder. `uncertain` is a real answer: it means
    the official text does not let us tie this alert to the user's location — so we do not
    attach it. Level/reason exist to be shown to judges and to the LLM."""

    status: RelevanceStatus = "uncertain"
    level: RelevanceLevel = "none"
    reason: str = ""
    matched_terms: List[str] = Field(default_factory=list)
    area_text: Optional[str] = None
    geometry_available: bool = False


class Alert(BaseModel):
    """One normalised official alert (CAP 1.2 record). Only quoted fields from the source;
    nothing here is generated, paraphrased or inferred."""

    # CAP identifier, e.g. IN-1787913209058029_29 (one field, spec name; no alias)
    alert_id: Optional[str] = None
    source: str = "NDMA SACHET"
    authority: Literal["official"] = "official"
    sender: Optional[str] = None
    author_name: Optional[str] = None
    event: Optional[str] = None
    headline: Optional[str] = None
    description: Optional[str] = None
    instruction: Optional[str] = None
    severity: Optional[str] = None      # Minor / Moderate / Severe / Extreme
    urgency: Optional[str] = None       # Expected / Immediate / Future / Past
    certainty: Optional[str] = None     # Likely / Observed / Possible ...
    category: Optional[str] = None      # Met / Flood / Fire ...
    area_desc: Optional[str] = None
    cap_status: Optional[str] = None    # Actual / Test / Draft ...
    msg_type: Optional[str] = None      # Alert / Update / Cancel ...
    language: Optional[str] = None
    lgd_district_codes: List[str] = Field(default_factory=list)
    sent_at: Optional[str] = None
    effective_at: Optional[str] = None
    onset_at: Optional[str] = None
    expires_at: Optional[str] = None
    # `validity` IS the spec's alert status: active | expired | unknown. No second status
    # field, because two spellings of the same fact is how "expired" ends up shown as "active".
    validity: AlertValidity = "unknown"
    validity_reason: str = ""
    expiry_missing: bool = False
    age_minutes: Optional[float] = None
    source_url: Optional[str] = None
    raw_source_url: Optional[str] = None
    feed_url: Optional[str] = None
    match_reason: str = ""              # short human summary of relevance
    relevance: AlertRelevance = Field(default_factory=AlertRelevance)
    raw_fields: Dict[str, Any] = Field(default_factory=dict)  # untouched source values


class AlertsEvidence(BaseModel):
    """Alert subsystem state. `checked` with an empty `items` list is a POSITIVE result
    (we looked and found nothing that verifiably applies here) and must never be confused
    with `unavailable` (we could not look) or `not_checked` (we did not try)."""

    source: str = "NDMA SACHET"
    authority: Literal["official"] = "official"
    state: Literal["checked", "not_checked", "unavailable"] = "not_checked"
    mode: Literal["live", "fixture_replay", "disabled", "not_run"] = "not_run"
    error: Optional[str] = None
    checked_at_utc: Optional[str] = None
    feeds_considered: List[str] = Field(default_factory=list)
    state_feed_used: Optional[str] = None
    items_in_feeds: int = 0
    details_fetched: int = 0
    items: List[Alert] = Field(default_factory=list)          # verified-relevant only
    recent_expired: List[Alert] = Field(default_factory=list)  # labelled, never presented as active
    # same alert published to both the state and India feeds carries DIFFERENT CAP
    # identifiers, so identifier dedupe is not enough; counted here so the UI can say why
    # "8 details fetched" produced fewer than 8 distinct alerts.
    rejected_duplicate: int = 0
    rejected_not_relevant: int = 0
    rejected_uncertain: int = 0
    rejected_stale: int = 0
    duration_ms: Optional[int] = None
    notes: List[str] = Field(default_factory=list)



# --------------------------------------------------------------------------- #
# 4. Validation + quality
# --------------------------------------------------------------------------- #
class Validation(BaseModel):
    """Phase 1 seeded the presence flags, Phase 2 added `alerts_valid`, Phase 3 fills the rest.
    All Phase-3 additions are Optional with defaults, so every Phase-1/2 payload still validates.
    `None` means "not judgeable from this evidence" — deliberately distinct from False."""

    ok: bool = False                    # no validation failure at all
    sufficient: bool = False              # ok AND complete enough to answer THIS question
    fresh: Optional[bool] = None
    complete: Optional[bool] = None
    location_resolved: bool = False
    timestamp_present: bool = False
    values_plausible: Optional[bool] = None   # config.RANGES sanity filter, not a met. judgement
    alerts_valid: Optional[bool] = None      # False=unavailable, None=not consulted, True=checked
    # --- Phase 3 ---
    labeling_consistent: Optional[bool] = None   # current vs forecast vs requested timeframe
    alert_integrity: Optional[bool] = None       # attached alerts are official+relevant+well-formed
    source_age_minutes: Optional[float] = None   # age of the provider's own timestamp
    checks_run: List[str] = Field(default_factory=list)
    failures: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class Source(BaseModel):
    name: str
    type: Literal["forecast", "current", "historical", "official_alert", "geocoding"]
    timestamp: Optional[str] = None
    # Which calendar day(s) the returned block actually covers. Needed so a "tomorrow"
    # answer can show Updated: <retrieval ts> AND Period: 2026-09-02 - otherwise the UI
    # would imply the forecast day itself is "current".
    period: Optional[str] = None
    url: Optional[str] = None
    authority: Literal["official", "research_repro", "derived"] = "research_repro"
    note: Optional[str] = None


EvidenceQuality = Literal["HIGH", "MEDIUM", "LOW"]
RiskLevel = Literal["LOW", "MEDIUM", "HIGH", "UNCERTAIN"]


class Advisory(BaseModel):
    """Output of the deterministic risk engine (Phase 3). NOT a model, NOT an LLM: every field
    is traceable to a named rule over validated evidence. Phrased as weather-related risk for an
    activity — never a guarantee about a person's safety."""

    risk_level: RiskLevel = "UNCERTAIN"
    activity: str = "travel"
    headline: str = ""          # "Weather-related travel risk is HIGH based on ..."
    reason: str = ""            # the deciding evidence, in one or two sentences
    factors: List[str] = Field(default_factory=list)
    rules_fired: List[str] = Field(default_factory=list)
    # Alert ids this advisory cited. validation.advisory_references_ok() proves each one exists
    # in Evidence.alerts.items — a risk level must never rest on an alert we did not retrieve.
    alert_ids: List[str] = Field(default_factory=list)
    evidence_quality: Optional[EvidenceQuality] = None
    disclaimer: str = (
        "Weather-related risk estimate derived from validated evidence (official alerts + model "
        "weather). It is not an official order, an evacuation instruction, or a guarantee of "
        "personal safety."
    )


class Evidence(BaseModel):
    """THE single source of truth handed to the LLM. No other context is provided."""

    schema_version: str = "weathergpt-evidence/0.1"
    status: Literal["grounded", "abstain", "clarify", "error"] = "grounded"
    request: Dict[str, Any] = Field(default_factory=dict)     # original text + intent + timeframe
    location: Optional[ResolvedLocation] = None
    weather: Optional[WeatherBundle] = None
    # Phase 2: one container owns alert state, so `checked-with-nothing-relevant` can never be
    # confused with `unavailable`. `alert_state` below is a computed view kept for compatibility
    # with the Phase 1 trace/UI contract (single source of truth: AlertsEvidence.state).
    alerts: AlertsEvidence = Field(default_factory=AlertsEvidence)
    sources: List[Source] = Field(default_factory=list)
    validation: Validation = Field(default_factory=Validation)
    evidence_quality: Optional[EvidenceQuality] = None
    quality_breakdown: Dict[str, Any] = Field(default_factory=dict)
    # Phase 3: `risk` stays the simple scalar the UI/badge already reads; `advisory` carries the
    # why (rules fired, factors, cited alert ids). Both come from the same Advisory object, so
    # they cannot disagree.
    risk: Optional[RiskLevel] = None
    advisory: Optional[Advisory] = None
    abstain_reason: Optional[str] = None
    clarification: Optional[str] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def alert_state(self) -> str:
        """Derived, never stored twice: checked | not_checked | unavailable."""
        return self.alerts.state


# --------------------------------------------------------------------------- #
# 5. API I/O
# --------------------------------------------------------------------------- #
class GroundingReport(BaseModel):
    """Deterministic verification of the LLM's answer against the Evidence object.

    Nothing here is judged by a model: each check is a plain comparison over fields the backend
    already produced, and every check that ran is listed so a judge can see the guard working.
    """

    verified: bool = False
    checks_run: List[str] = Field(default_factory=list)
    failures: List[str] = Field(default_factory=list)
    numbers_checked: int = 0
    numbers_rejected: List[str] = Field(default_factory=list)
    attempts: int = 0                    # 1 = accepted first try, 2 = accepted after regeneration
    regenerated: bool = False
    llm_status: str = "not_attempted"    # ok|no_key|disabled|upstream_error|malformed_json|rejected
    model: Optional[str] = None
    latency_ms: Optional[float] = None
    note: str = ""


class GroundedAnswer(BaseModel):
    """What the user is shown. Either the LLM's phrasing (only after it passed verification) or the
    deterministic evidence-based fallback — never an unverified LLM string."""

    text: str = ""
    source: str = ""
    timestamp: Optional[str] = None
    risk: Optional[RiskLevel] = None
    evidence_quality: Optional[EvidenceQuality] = None
    alert_mentioned: bool = False
    # provenance of the sentence itself, kept separate so the UI can be honest about it
    origin: Literal["groq_llm", "deterministic_fallback"] = "deterministic_fallback"
    grounding: GroundingReport = Field(default_factory=GroundingReport)


class QueryRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=400)
    session_id: Optional[str] = None
    location_hint: Optional[str] = None    # e.g. user tapped "Pune, Maharashtra" in the UI
    include_pipeline: bool = True          # stage-by-stage trace: gold for judging/demo


class ParsedQuery(BaseModel):
    message: str
    intent: Intent = "forecast_current"
    intent_reason: str = "phase1_default"
    location_text: Optional[str] = None
    timeframe: Timeframe = "now"
    timeframe_reason: str = "phase1_default"
    target_date: Optional[str] = None      # YYYY-MM-DD for historical/specific day
    notes: List[str] = Field(default_factory=list)


class QueryResponse(BaseModel):
    status: Literal["grounded", "abstain", "clarify", "error"]
    user_message: str
    evidence: Evidence
    pipeline: Dict[str, Any] = Field(default_factory=dict)
    # Phase 4: the phrased answer. Optional on purpose — when the LLM is unconfigured or the guard
    # rejects it, `answer` carries the deterministic fallback, and the Evidence object below stays
    # the authoritative record either way.
    answer: Optional[GroundedAnswer] = None
