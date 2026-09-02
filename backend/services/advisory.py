"""
advisory.py — Phase 3: deterministic, evidence-only risk estimation.

WHAT THIS IS NOT
  * Not an ML model and not an LLM. There is no scoring function to tune and no prompt. The LLM
    (Phase 4) will only be allowed to EXPLAIN the object this returns; it must not produce it.
  * Not a safety guarantee. Output wording is always "weather-related travel risk is X based
    on …" — never "it is safe to travel", never "it is unsafe to travel". A person deciding
    whether to cross a flooded ghat needs more than a model blend, and pretending otherwise
    would be the most dangerous thing this project could do.

HOW A LEVEL IS CHOSEN (each rule has an id that ends up in `rules_fired`, so any number shown
on stage can be traced back to one sentence of code):

  R1  active + relevance-verified official alert with severity Severe/Extreme (or Immediate
      urgency)                     -> HIGH      (and nothing may downgrade it)
  R2  active + verified official alert, lower severity -> MEDIUM
  R3  weather hazard(s) from the validated evidence -> MEDIUM, or HIGH when a hazard crosses the
      "strong" line
  R4  alert relevance UNCERTAIN (an official alert exists for the wider area, we cannot tie it
      here) -> UNCERTAIN if the weather is calm, MEDIUM if a hazard is already present
  R5  official alerts could not be consulted (unavailable / not checked) and nothing else raises
      the risk -> UNCERTAIN. We refuse to call a trip low-risk while warnings are unverifiable.
  R6  evidence insufficient (validation failed / stale / Evidence Quality LOW) and no active
      alert -> UNCERTAIN
  R7  nothing above, evidence sufficient -> LOW

THRESHOLDS
  Every numeric threshold below is an ENGINEERING HEURISTIC for this MVP, listed in THRESHOLDS
  with its rationale. They are NOT IMD criteria and are not presented as one. Where the source
  itself classifies severity — SACHET alerts — that classification is used verbatim instead.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from backend.models import Advisory, Evidence, ForecastDay, WeatherBundle
from backend.services.validation import answered_day as validation_answered_day

# Documented heuristics (units match the normalised Evidence fields). Chosen to be conservative:
# they fire on weather a traveler in India would obviously care about, not on marginal drizzle.
THRESHOLDS: Dict[str, Tuple[float, str]] = {
    # Open-Meteo's `current.precipitation_mm` is the amount in the reported 15-minute step.
    "rain_now_mm_per_15min": (7.5, ">= 7.5 mm inside one 15-minute step is heavy rain for a road user"),
    "rain_day_mm": (50.0, "a 50 mm day is disruptive for travel in most of India"),
    "rain_day_mm_strong": (115.0, "115 mm/day is the level at which flooding becomes the primary hazard"),
    "wind_kmh": (40.0, "sustained 40 km/h noticeably affects two-wheelers and high-sided vehicles"),
    "wind_kmh_strong": (85.0, "85+ km/h is damage-level wind; a 'strong' hazard on its own"),
    "rain_prob_pct": (80.0, "high-confidence rain plus a meaningful amount, not probability alone"),
}

# WMO codes that by themselves qualify as a hazard for a travel question, and whether they are
# "strong". Codes 61/63/80/81 (light-moderate rain) are deliberately NOT here: the mm thresholds
# decide those, so we do not double-count the same event.
HAZARD_CODES: Dict[int, Tuple[str, bool]] = {
    95: ("thunderstorm", False),
    96: ("thunderstorm with hail", True),
    99: ("severe thunderstorm with hail", True),
    55: ("dense drizzle", False),
    57: ("freezing drizzle", True),
    66: ("freezing rain", True),
    67: ("heavy freezing rain", True),
    71: ("snow fall", False),
    73: ("moderate snow", True),
    75: ("heavy snow", True),
    77: ("snow grains", False),
    85: ("snow showers", False),
    86: ("heavy snow showers", True),
    45: ("fog", False),
    48: ("depositing fog", True),
    51: ("light shower", False),
    80: ("rain showers", False),
    81: ("moderate rain showers", False),
    82: ("violent rain showers", True),
    65: ("heavy rain", True),
}
SEVERITY_RAISING = {"Severe", "Extreme"}


def _day_for(bundle: WeatherBundle, timeframe: str, target_date: Optional[str]) -> Optional[ForecastDay]:
    # Same selector validation.answered_day() uses for the day_* completeness checks, so the
    # advisory and the validator are always looking at the same day block.
    return validation_answered_day(bundle, timeframe, target_date)


def weather_hazards(ev: Evidence) -> List[Tuple[str, bool, str]]:
    """(label, is_strong, evidence_quote). Reads ONLY validated numbers already on the Evidence
    object; it never forecasts, extrapolates or fills gaps."""
    out: List[Tuple[str, bool, str]] = []
    w = ev.weather
    if w is None:
        return out
    timeframe = str(ev.request.get("timeframe") or "now")
    now_limit, _ = THRESHOLDS["rain_now_mm_per_15min"]
    day_limit, _ = THRESHOLDS["rain_day_mm"]
    day_strong, _ = THRESHOLDS["rain_day_mm_strong"]
    wind_limit, _ = THRESHOLDS["wind_kmh"]
    wind_strong, _ = THRESHOLDS["wind_kmh_strong"]

    cur = w.current
    if cur is not None:
        if cur.precipitation_mm is not None and cur.precipitation_mm >= now_limit:
            out.append(("heavy rain right now", cur.precipitation_mm >= day_strong / 4,
                        f"current precipitation {cur.precipitation_mm} mm in the reported interval"))
        if cur.wind_speed_kmh is not None and cur.wind_speed_kmh >= wind_limit:
            out.append(("strong wind", cur.wind_speed_kmh >= wind_strong,
                        f"current wind {cur.wind_speed_kmh} km/h"))
        if cur.weather_code in HAZARD_CODES:
            label, strong = HAZARD_CODES[cur.weather_code]
            out.append((label, strong, f"WMO code {cur.weather_code} ({cur.condition or label})"))
    day = _day_for(w, timeframe, ev.request.get("target_date"))
    if day is not None:
        if day.precipitation_sum_mm is not None and day.precipitation_sum_mm >= day_limit:
            out.append((f"high rainfall for {day.label or day.date}",
                        day.precipitation_sum_mm >= day_strong,
                        f"{day.precipitation_sum_mm} mm total expected"))
        if day.wind_speed_max_kmh is not None and day.wind_speed_max_kmh >= wind_limit:
            out.append(("strong gusts expected", day.wind_speed_max_kmh >= wind_strong,
                        f"max wind {day.wind_speed_max_kmh} km/h"))
        if day.weather_code in HAZARD_CODES:
            label, strong = HAZARD_CODES[day.weather_code]
            out.append((f"{label} expected", strong, f"day WMO code {day.weather_code}"))
    return out


def _level_rank(level: str) -> int:
    return {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "UNCERTAIN": 1}[level]


def advise(ev: Evidence) -> Advisory:
    """Deterministic: same Evidence -> same Advisory. That is the whole point — the decision is
    auditable and testable, and the LLM cannot change it by phrasing."""
    factors: List[str] = []
    rules: List[str] = []
    alert_ids: List[str] = []
    v = ev.validation
    activity = "travel" if str(ev.request.get("intent")) == "advisory_risk" else "outdoor activity/travel"

    # Everything in ev.alerts.items was already tied to this location by the Phase-2 ladder, so
    # "verified" is the right word: we never carry an alert we could not attach.
    active_verified = [a for a in ev.alerts.items if a.validity == "active" and a.relevance.status == "relevant"]
    hazards = weather_hazards(ev)
    strong_hazard = next((h for h in hazards if h[1]), None)
    any_hazard = bool(hazards)

    def make(level: str, basis: str, reason: str) -> Advisory:
        # Fixed sentence shape, deliberately not interpolated with `activity`: the product rule is
        # that this line always reads "Weather-related travel risk is X based on ..." (never
        # "it is safe / unsafe"). `activity` still records what the estimate is about.
        head = (
            f"Weather-related travel risk is {level} based on {basis}."
            if level != "UNCERTAIN"
            else f"Weather-related travel risk is UNCERTAIN because {basis}."
        )
        return Advisory(
            risk_level=level,  # type: ignore[arg-type]
            activity=activity,
            headline=head,
            reason=reason,
            factors=factors,
            rules_fired=rules,
            alert_ids=alert_ids,
            evidence_quality=ev.evidence_quality,
        )

    # ---- R1: an active, location-verified Severe/Extreme official alert outranks everything ---- #
    if active_verified:
        top = max(active_verified, key=lambda a: (a.severity in SEVERITY_RAISING, a.urgency == "Immediate"))
        raise_high = top.severity in SEVERITY_RAISING or top.urgency == "Immediate"
        alert_ids = [a.alert_id for a in active_verified if a.alert_id]
        for a in active_verified:
            factors.append(
                f"official {a.severity or 'unclassified'} {a.event or 'alert'} from {a.sender or a.author_name or 'NDMA SACHET'}"
                f" (valid until {a.expires_at or 'not published'})"
            )
            if a.instruction:
                # U1: surface the authority's published instruction, quoted VERBATIM and
                # attributed. It is never paraphrased (that could warp an official order) and
                # never invented when the CAP record carries none.
                factors.append(
                    f'official instruction, quoted from {a.sender or a.author_name or "NDMA SACHET"}: '
                    f'"{a.instruction.strip()}"'
                )
        rules.append("R1_active_severe_official_alert" if raise_high else "R2_active_official_alert")
        if any_hazard:
            factors.extend(f"weather: {h[0]} — {h[2]}" for h in hazards)
        basis = f"the active official {top.severity or 'unclassified'} {top.event or 'alert'} for this area"
        reason = (
            f"NDMA SACHET publishes an active {top.severity or 'unclassified'} {top.event or 'alert'} that our "
            f"relevance check ties to this location ({top.relevance.reason}). "
            + ("Model weather also shows " + hazards[0][0] + ". " if any_hazard else "")
            + "An active official alert is surfaced ahead of the forecast summary and is never downplayed."
        )
        return make("HIGH" if raise_high else "MEDIUM", basis, reason)

    # ---- R3: hazard(s) visible in the validated weather evidence ------------------------------- #
    weather_level = "HIGH" if strong_hazard else ("MEDIUM" if any_hazard else "LOW")
    if any_hazard:
        factors.extend(f"weather: {h[0]} — {h[2]}" for h in hazards)
        rules.append("R3_weather_hazard_strong" if strong_hazard else "R3_weather_hazard")

    # ---- R4: alerts exist but relevance could not be established ------------------------------- #
    uncertain_alerts = ev.alerts.rejected_uncertain
    if uncertain_alerts and not active_verified:
        factors.append(
            f"{uncertain_alerts} official alert(s) exist in the consulted feeds whose coverage of this "
            f"specific area could not be confirmed ({ev.alerts.notes[-1] if ev.alerts.notes else 'see alerts.notes'})"
        )
        rules.append("R4_alert_relevance_uncertain")
        if not any_hazard:
            return make("UNCERTAIN",
                        "an official alert exists for the wider area but its relevance here is unconfirmed",
                        "An official alert exists in the broader state area, but its relevance to this "
                        "location could not be confirmed from the published area text, so we will not "
                        "call the risk low — and we will not claim it applies here either.")
        # a hazard is already enough to raise concern; the unconfirmed alert keeps it off HIGH
        return make("MEDIUM", "hazardous model weather, with an unconfirmed official alert in the wider area",
                    "Model weather shows a hazard for this timeframe AND an official alert exists whose "
                    "coverage of this exact area we could not confirm. Both facts are reported; the alert "
                    "is not upgraded and the risk is not downgraded.")

    # ---- R5: we could not check warnings at all ------------------------------------------------- #
    if ev.alerts.state != "checked":
        why = ("the official alert source could not be consulted"
               + (f" ({ev.alerts.error})" if ev.alerts.error else "")) if ev.alerts.state == "unavailable" \
            else "alerts were not consulted for this run"
        factors.append(why)
        if weather_level == "LOW":
            rules.append("R5_alerts_unverifiable")
            return make("UNCERTAIN", why,
                        f"Weather-related risk for {activity} cannot be called low while {why}: an active "
                        "official warning may exist that we simply could not see.")
        rules.append("R5_alerts_unverifiable_with_hazard")
        basis = "hazardous model weather, and " + why
        return make(weather_level, basis,
                    f"The weather evidence shows a hazard ({hazards[0][0]}). Note that {why}, so this "
                    "risk estimate may be incomplete in the other direction too.")

    # ---- R6: insufficient evidence -------------------------------------------------------------- #
    if not v.sufficient or ev.evidence_quality == "LOW":
        rules.append("R6_insufficient_evidence")
        detail = "; ".join(v.failures[:3]) if v.failures else "no failures recorded but the quality label is LOW"
        factors.append(f"evidence quality {ev.evidence_quality or 'LOW'}: {detail}")
        if weather_level == "HIGH":
            # We can still say the retrieved numbers show a hazard, but we flag the quality explicitly.
            return make("MEDIUM", "hazardous values in evidence that failed validation",
                        f"The retrieved values show {hazards[0][0]}, but validation did not pass ({detail}), "
                        "so this is reported as elevated risk with an explicit caveat rather than a firm HIGH.")
        return make("UNCERTAIN", f"validation did not pass ({detail})",
                    "Reliable evidence could not be verified for this request, so no confident risk "
                    "recommendation is made.")

    # ---- R7: quiet weather, alerts checked, evidence sufficient --------------------------------- #
    if weather_level != "LOW":
        basis = "validated weather evidence" + (f" showing {hazards[0][0]}" if hazards else "")
        return make(weather_level, basis,
                    "No active official alert applies to this location per NDMA SACHET, and the retrieved "
                    f"weather values {'show ' + hazards[0][0] if hazards else 'are unremarkable'} for the "
                    f"asked timeframe. Evidence Quality: {ev.evidence_quality or 'n/a'}.")
    rules.append("R7_quiet")
    factors.append("NDMA SACHET checked: no active official alert verifiably tied to this location")
    factors.extend(f"weather: {h[0]}" for h in hazards)
    return make("LOW", "validated model weather and an official-alert check that came back empty",
                "Current retrieved evidence shows no hazardous values for the asked timeframe, and SACHET "
                "was checked with no active official alert verifiably tied to this location. That is a checked "
                "result, not a promise that none exists — and not a statement about anyone's personal safety.")
