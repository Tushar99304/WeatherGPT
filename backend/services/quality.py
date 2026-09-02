"""
quality.py — Phase 3: the Evidence Quality badge.

WHAT THIS IS
  An ENGINEERING HEURISTIC that answers one question: "how much should we trust this evidence
  block we just assembled?" It is deliberately NOT a forecast probability. Nothing here estimates
  the chance of rain; the probability of rain, when the user asked for it, comes from the
  retrieved evidence (`precipitation_probability_max_pct`) and never from this score.

THE WEIGHTS (fixed, documented, no hidden tuning):

    source authority   40   how official is the evidence the answer rests on
    freshness          30   is it current for the timeframe that was asked about
    completeness       20   does it contain what THIS question needs
    agreement          10   do multiple comparable sources actually agree
                          ----
                           100

  40 for authority is the biggest single weight on purpose: an NDMA SACHET CAP record is the
  official source of truth for warnings, and a model/reanalysis blend is not. Open-Meteo keeps
  `authority="research_repro"` — it is never relabelled official, and IMD is not pretended to be
  connected (access pending), so the weather half of any answer caps out below full marks until a
  second real source exists.

  Label thresholds: score >= 80 → HIGH, >= 55 → MEDIUM, else LOW. The numbers are an MVP
  convention (documented here, tested), not a scientific standard — which is exactly why the UI
  shows `HIGH`, not `85`.

HARD CAPS (the rules that matter for safety, applied to the LABEL after scoring):
  1. alert source unavailable                → cannot be HIGH   (we could not check warnings)
  2. only-uncertain alert information         → cannot be HIGH   (an alert exists, coverage unknown)
  3. required evidence missing / insufficient → LOW
  4. stale data for the asked timeframe       → LOW
  5. location unresolved                      → LOW (never imply trustworthy weather evidence)
  6. attached alert failed integrity checks   → LOW
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from backend import config
from backend.models import Evidence, Validation
from backend.services import providers

WEIGHTS = {"authority": 40, "freshness": 30, "completeness": 20, "agreement": 10}
HIGH_MIN = 80
MEDIUM_MIN = 55

# Weather sources that could, in principle, be compared with each other. "geocoding" is excluded:
# it locates the place, it does not describe the atmosphere, so it can neither agree nor disagree.
COMPARABLE_SOURCE_TYPES = {"forecast", "current", "historical"}


def _label_for(score: int) -> str:
    if score >= HIGH_MIN:
        return "HIGH"
    if score >= MEDIUM_MIN:
        return "MEDIUM"
    return "LOW"


def _research_weather_source_name(ev: Evidence) -> str:
    """Display name of the research/repro weather source (provider-agnostic), for note prose.
    Reads the live sources[] rather than hardcoding one provider."""
    names = [
        s.name
        for s in ev.sources
        if s.type in COMPARABLE_SOURCE_TYPES and s.authority == "research_repro"
    ]
    if names:
        return names[0]
    return providers.source_label(config.WEATHER_PROVIDER)


def _authority(ev: Evidence, v: Validation, notes: List[str]) -> float:
    """40 = official SACHET evidence is available for the question.
    26 = the answer rests on a research/reanalysis blend (e.g. the Open-Meteo NWP models) —
         reproducible and honestly labelled, but not the national meteorological service.
    12 = only derived values.  0 = nothing authoritative at all.

    Provider-agnostic: the 26/12 bands are keyed on Source.authority, not on a provider name, so
    any future provider (IMD/GFS/WRF via the registry) is scored by its own authority label."""
    intent = str(ev.request.get("intent") or "")
    weather_part = 0.0
    if any(s.authority == "research_repro" and s.type in COMPARABLE_SOURCE_TYPES for s in ev.sources):
        weather_part = 26.0
    elif any(s.authority == "derived" for s in ev.sources):
        weather_part = 12.0

    official_available = ev.alerts.state == "checked"
    active_official = any(
        a.validity == "active" and a.relevance.status == "relevant" for a in ev.alerts.items
    )
    if intent == "official_alert":
        # The question IS the alert question: without SACHET there is no authority here at all.
        if official_available:
            notes.append("authority 40/40: the official warning source was consulted for an alert question")
            return 40.0
        notes.append("authority 0/40: an alert question could not be answered from the official source")
        return 0.0
    if active_official:
        notes.append("authority 40/40: an active, location-verified official alert outranks model weather")
        return 40.0
    if official_available and weather_part:
        notes.append(
            f"authority {weather_part:.0f}/40 from {_research_weather_source_name(ev)} "
            "(research_repro); SACHET checked and found nothing applicable, which does not add "
            "authority to the weather numbers"
        )
        return weather_part
    if weather_part:
        notes.append(
            f"authority {weather_part:.0f}/40: single research/reanalysis source "
            f"({_research_weather_source_name(ev)}). An official national meteorological provider "
            "(IMD) is the intended authoritative source and remains architecture-ready, not "
            "relabelled official; this NWP blend is NOT upgraded."
        )
    else:
        notes.append("authority 0/40: no source carrying an authority label is present")
    return weather_part


def _freshness(
    ev: Evidence, v: Validation, notes: List[str], now: Optional[dt.datetime] = None
) -> float:
    """30 = well inside WEATHER_MAX_STALENESS_MIN for a 'now' answer; scaled down from there.
    A forecast-day answer is judged on retrieval age instead, because its values are supposed to
    describe the future. Historical lookups are exempt by definition.

    `now` is the single reference clock (validation.reference_now; the frozen clock in tests).
    Every age here must read it instead of dt.datetime.now(), so quality scores the SAME moment
    validation judged — that mismatch was the alert-only freshness date-skew (a fixed SACHET check
    read as days-old under the real clock, dropping a HIGH alert answer to MEDIUM)."""
    from backend.services.validation import reference_now

    ref = now or reference_now()
    full = float(WEIGHTS["freshness"])
    w = ev.weather
    if w is None:
        # An alert-only question is answered from the alert check, so that is what gets dated:
        # "how long ago did we ask SACHET, and is the alert still inside its own window".
        if ev.alerts.state == "checked":
            checked = _parse(ev.alerts.checked_at_utc)
            if checked is None:
                notes.append("freshness 12/30: alert check has no usable checked_at timestamp")
                return full * 0.4
            mins = max(0.0, (ref - checked).total_seconds() / 60.0)
            score = full if mins <= 15 else (full * 0.6 if mins <= 60 else full * 0.2)
            notes.append(
                f"freshness {score:.0f}/30: no weather block is needed for this question; judged on "
                f"the SACHET check ({mins:.0f} min ago) and the alert's own validity window"
            )
            return score
        notes.append("freshness 0/30: no weather block and no completed alert check to date")
        return 0.0
    if w.kind == "historical":
        notes.append(
            "freshness 30/30: historical lookup — values are correctly old, staleness does not apply "
            "(retrieval timestamp present)" if w.retrieved_at_utc else "freshness 12/30: historical lookup without a retrieval timestamp"
        )
        return full if w.retrieved_at_utc else full * 0.4
    age = v.source_age_minutes
    limit = float(config.WEATHER_MAX_STALENESS_MIN)
    if age is None:
        # No current block to age (a pure forecast-day question): judge our own retrieval age.
        retrieved = _parse(w.retrieved_at_utc)
        if retrieved is None:
            notes.append("freshness 0/30: no parseable timestamp of any kind")
            return 0.0
        mins = max(0.0, (ref - retrieved).total_seconds() / 60.0)
        score = full if mins <= limit else (full * 0.6 if mins <= 2 * limit else full * 0.2)
        notes.append(
            f"freshness {score:.0f}/30: no current block for this timeframe, judged on retrieval age "
            f"({mins:.0f} min, limit {limit:.0f} min)"
        )
        return score
    if age <= 0.5 * limit:
        notes.append(f"freshness 30/30: provider timestamp {age:.0f} min old (limit {limit:.0f} min)")
        return full
    if age <= limit:
        notes.append(f"freshness 22/30: provider timestamp {age:.0f} min old, inside the limit but ageing")
        return full * (22 / 30)
    if age <= 2 * limit:
        notes.append(f"freshness 8/30: provider timestamp {age:.0f} min old, past the limit")
        return full * (8 / 30)
    notes.append(f"freshness 0/30: provider timestamp {age:.0f} min old, far past the {limit:.0f} min limit")
    return 0.0


def _completeness(ev: Evidence, v: Validation, notes: List[str]) -> float:
    """20 = every field this question needs is present. Scored on the FRACTION present, so a
    missing piece shows up as a visible deduction instead of a pass/fail cliff. Fields irrelevant
    to the question are not in the required list at all (validation.required_fields)."""
    from backend.services.validation import missing_required_fields_for, required_fields

    intent = str(ev.request.get("intent") or "forecast_current")
    timeframe = str(ev.request.get("timeframe") or "now")
    required = required_fields(intent, timeframe)
    missing = missing_required_fields_for(ev, intent, timeframe)
    total = len(required)
    if total == 0:
        return float(WEIGHTS["completeness"])
    score = float(WEIGHTS["completeness"]) * (total - len(missing)) / total
    if missing:
        notes.append(f"completeness {score:.0f}/20: absent for this question -> {', '.join(missing)}")
    else:
        notes.append(f"completeness 20/20: all {total} field(s) this question needs are present")
    return score


def _agreement(ev: Evidence, notes: List[str]) -> Tuple[float, List[str]]:
    """10 = nothing contradicts the answer.

    IMPORTANT honesty rule: agreement is only measured when multiple COMPARABLE sources actually
    exist in this evidence object. With one provider we report a neutral full mark and say why —
    we do not invent a second source, and we do not treat "unmeasurable" as "disagreement".
    When two comparable sources do exist, we compare the period/timestamp they describe and, if
    they carry the same measurement, whether it matches. We never average incompatible values:
    a disagreement lowers the score and is surfaced, not smoothed away.
    """
    comparable = [s for s in ev.sources if s.type in COMPARABLE_SOURCE_TYPES]
    if len(comparable) < 2:
        notes.append(
            "agreement 10/10 (neutral): only one comparable weather source exists in this build. "
            "Cross-source agreement becomes measurable when the IMD provider is added — it is not "
            "penalised for not existing."
        )
        return float(WEIGHTS["agreement"]), []
    periods = {s.period or (s.timestamp or "").split("T")[0] or "unknown" for s in comparable}
    disagreements: List[str] = []
    if len(periods) > 1:
        disagreements.append(
            "sources describe different periods: " + ", ".join(sorted(p for p in periods))
        )
    # Numeric comparison hook: only possible once a second provider fills the same fields.
    values: Dict[str, List[Tuple[str, float]]] = {}
    for s in comparable:
        if s.type == "forecast" and ev.weather and ev.weather.current and ev.weather.current.temperature_c is not None:
            values.setdefault("temperature_c", []).append((s.name, float(ev.weather.current.temperature_c)))
    for field, pairs in values.items():
        seen = {round(v, 1) for _, v in pairs}
        if len(seen) > 1:
            disagreements.append(
                f"{field} differs across sources: " + ", ".join(f"{n}={v}" for n, v in pairs)
                + " (reported, NOT averaged)"
            )
    if disagreements:
        notes.append(
            f"agreement 0/{WEIGHTS['agreement']}: " + "; ".join(disagreements)
            + " — disagreement is surfaced for the user to see, not silently resolved"
        )
        return 0.0, disagreements
    notes.append(
        f"agreement {WEIGHTS['agreement']}/{WEIGHTS['agreement']}: "
        f"{len(comparable)} comparable sources describe the same period with consistent values"
    )
    return float(WEIGHTS["agreement"]), []


def _parse(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        d = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def score_evidence(
    ev: Evidence, v: Validation, now: Optional[dt.datetime] = None
) -> Tuple[str, Dict[str, Any]]:
    """Returns (label, quality_breakdown). Deterministic given the evidence + validation object:
    the same Evidence always produces the same score, which is what makes it testable.

    `now` defaults to the single reference clock (validation.reference_now); callers/tests may
    inject a fixed instant, exactly as they do for validate_evidence()."""
    from backend.services.validation import reference_now

    ref = now or reference_now()
    notes: List[str] = []
    caps: List[str] = []

    parts = {
        "authority": _authority(ev, v, notes),
        "freshness": _freshness(ev, v, notes, ref),
        "completeness": _completeness(ev, v, notes),
        "agreement": 0.0,  # filled below (needs the disagreement list)
    }
    agree, disagreements = _agreement(ev, notes)
    parts["agreement"] = agree
    score = int(round(sum(parts.values())))

    # ---- hard caps on the LABEL (the score is kept, so the deduction stays visible) ---------- #
    forced_low = False
    if ev.location is None or not v.location_resolved:
        caps.append("rule 5: location unresolved -> no quality label may imply trustworthy weather evidence")
        forced_low = True
    if v.fresh is False:
        caps.append(
            "rule 4: data is stale for the requested timeframe "
            f"(provider timestamp age {v.source_age_minutes if v.source_age_minutes is not None else 'unknown'})"
        )
        forced_low = True
    if not v.sufficient:
        caps.append("rule 3: required evidence missing or failed validation -> LOW")
        forced_low = True
    if ev.alerts.state == "unavailable":
        caps.append("rule 1: official alert source could not be consulted -> capped at MEDIUM")
    if ev.alerts.state == "checked" and not ev.alerts.items and ev.alerts.rejected_uncertain:
        caps.append(
            f"rule 2: {ev.alerts.rejected_uncertain} official alert(s) exist whose relevance to this "
            "location could not be confirmed -> capped at MEDIUM"
        )
    if v.alert_integrity is False:
        caps.append("rule 6: an attached alert failed integrity checks -> LOW")
        forced_low = True

    label = _label_for(score)
    if forced_low:
        label = "LOW"
    elif caps and label == "HIGH":
        label = "MEDIUM"

    breakdown: Dict[str, Any] = {
        "score": score,
        "label": label,
        "weights": dict(WEIGHTS),
        "thresholds": {"HIGH_MIN": HIGH_MIN, "MEDIUM_MIN": MEDIUM_MIN},
        "breakdown": {
            "authority": round(parts["authority"], 1),
            "freshness": round(parts["freshness"], 1),
            "completeness": round(parts["completeness"], 1),
            "agreement": round(parts["agreement"], 1),
            "caps_applied": caps,
        },
        "notes": notes,
        "disagreements": disagreements,
        "meaning": (
            "Evidence Quality measures how trustworthy this retrieved evidence is (authority, "
            "freshness, completeness, agreement). It is NOT a probability that the weather "
            "forecast turns out right."
        ),
    }
    return label, breakdown
