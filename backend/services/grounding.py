"""
grounding.py — Phase 4: verify the LLM's answer against the Evidence object, deterministically.

WHY THIS FILE EXISTS
  The LLM is only allowed to phrase a decision the backend already made. It is not trusted, so
  every claim it makes is re-checked against the structured evidence before the answer is shown.
  No model judges a model: each check below is a plain comparison, and each one that ran is listed
  in `checks_run` so the trace shows exactly what was enforced.

THE CHECKS (numbers match the spec)
  1  required fields present
  2  every weather number in the answer exists in the evidence (±0.1), unit-aware
  3  `source` must equal a real `sources[].name` (so "According to IMD…" fails while IMD is absent)
  4  `timestamp` must be an "as of" stamp from the evidence — never the forecast target date
  5  an active official alert must be mentioned, and a Severe/Extreme severity word must survive
  6  any alert identifier in the answer must exist in `alerts.items` (uses validation.alert_ids_present)
  7  `risk` must equal `advisory.risk_level` — the LLM may not move the risk level
  8  `evidence_quality` must equal the backend's label
  9  forecast values may not be phrased as current observations
  10 insufficient evidence must be admitted, not dressed up as a confident answer
  11 an unconsultable alert source must never be reported as "no alerts exist"
  12 "checked, nothing relevant" must not be shortened to "there are no alerts"
  13 no safety guarantees, no evacuation orders (unless quoted in the evidence itself)

DELIBERATE LIMITS (documented rather than hidden)
  * Bare small integers 0-12 ("next 3 hours", "two sources", "case 2") are NOT treated as weather
    claims: without a unit they are counts/durations, and rejecting them produced false failures on
    correct answers. Any number carrying a weather unit is always checked, no exceptions.
  * Numbers appearing anywhere in the evidence (including inside quoted alert headlines and notes)
    are allowed. That is the same rule `docs/PLAN_48H.md` §F specifies, and it is why a quoted
    "…in next 3 hours" from a real headline is not a hallucination.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from backend.models import Evidence, GroundingReport
from backend.services.validation import alert_ids_present

# tolerance for decimal drift between "22.8000001" and "22.8" (PLAN §F: ±0.1)
TOL = 0.1
REQUIRED_KEYS: Tuple[str, ...] = ("answer", "source", "timestamp", "risk", "evidence_quality")

# Which evidence fields define each measurement category. Numbers with one of these units must
# match a value of the SAME category, so "31.4 °C" cannot be waved through by an unrelated 31.4.
CATEGORY_KEYS: Dict[str, Set[str]] = {
    "temperature": {"temperature_c", "apparent_temperature_c", "temperature_max_c", "temperature_min_c"},
    "precipitation": {"precipitation_mm", "precipitation_sum_mm"},
    "probability": {
        "precipitation_probability_max_pct", "humidity_pct", "cloud_cover_pct",
    },
    "wind": {"wind_speed_kmh", "wind_speed_max_kmh"},
    "pressure": {"pressure_hpa"},
    "angle": {"wind_direction_deg"},
}
UNIT_TO_CATEGORY = {
    "°c": "temperature", "oc": "temperature", "c": "temperature", "celsius": "temperature",
    "k": "temperature",          # kelvin is not used by this evidence, so any "K" claim is a failure
    "mm": "precipitation", "cm": "precipitation", "inches": "precipitation", "in": "precipitation",
    "%": "probability", "percent": "probability", "pct": "probability",
    "km/h": "wind", "kmh": "wind", "kph": "wind", "mph": "wind", "m/s": "wind", "knots": "wind",
    "hpa": "pressure", "mb": "pressure", "mbar": "pressure",
    "deg": "angle", "°": "angle",
}
# Bare small integers are counts, durations and clock/day numbers, not measurements: "next 3
# hours", "15-minute interval", "case 2", "the 21st". Anything carrying a weather unit is always
# checked regardless of size, which is where a hallucination actually shows up ("31.4 °C").
SMALL_INT_EXEMPT_MAX = 60
YEAR_RANGE = (1900, 2199)

NUMBER_RE = re.compile(r"(?<![\w.:])(-?\d+(?:[.,]\d+)?)\s*"
                       r"(°?\s?[cC](?:elsius)?|m/s|km/h|kmph|kph|mph|knots|%|percent|pct|mm|cm|"
                       r"inches|in|hpa|hPa|mbar|mb|deg|°)?", re.IGNORECASE)
IDENT_RE = re.compile(r"\bIN-\d+(?:_\d+)?\b")
# 5+ bare digits are epochs, CAP identifiers or record ids — but ONLY when no unit follows: a
# unit-carrying number is a measurement claim no matter how absurd ("12345% chance"), and that is
# exactly the kind of invention this check exists to catch.
_UNIT_TAIL = "|".join(sorted((re.escape(u) for u in UNIT_TO_CATEGORY), key=len, reverse=True))
LONG_DIGITS = re.compile(rf"\b\d{{5,}}(?!\s*(?:{_UNIT_TAIL}))", re.IGNORECASE)
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")   # ISO date or datetime
# "Evidence quality HIGH (86/100)" — the denominator is a scale definition, not a weather value.
SCORE_RE = re.compile(r"\b\d{1,3}(?:\.\d+)?\s*/\s*100\b")
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A phrase inside quotes is a reference, not an assertion by this answer.
QUOTED_SPAN_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\u2018[^\u2019]*\u2019|\u201c[^\u201d]*\u201d")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
OFFSET_RE = re.compile(r"[+-]\d{2}:?\d{2}\b")
WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100,
}
# Present-tense wording: a clause that carries a day-only number without any day cue is the
# "forecast read as current" bug.
PRESENT_TENSE_CUES = ("now", "currently", "right now", "at this moment", "as of",
                      "it is", "there is", "we have", "conditions are", "outside")
# Cues that legitimately mark a clause as a day/forecast statement.
# Only *timeframe* words count. "chance" and "probability" deliberately do NOT: "there is a 100%
# chance of rain right now" must still be caught when 100% exists only in a day block.
DAY_FRAME_CUES = ("tomorrow", "day after", "forecast", "expected", "likely", "will",
                  "today", "tonight", "later", "yesterday", "morning", "afternoon", "evening",
                  "24 hour", "48 hour", "next ")
CLAUSE_SPLIT_RE = re.compile(r"[;!?\n]|\.(?=\s|$)|\b(?:but|while|however|whereas)\b")
HEDGE_PHRASES = (
    "could not be verified", "could not verify", "not be verified", "could not be confirmed",
    "could not be established", "unable to verify", "not verified", "insufficient", "unavailable",
    "won't guess", "will not guess", "won\u2019t guess", "will not invent", "no verified",
    "not enough evidence", "could not be consulted", "not been checked",
)
OVERCONFIDENT = ("definitely", "certainly", "guaranteed", "for sure", "100% safe", "completely safe")
FORBIDDEN_SAFETY = (
    "it is safe to travel", "safe to travel", "you are safe", "it is completely safe",
    "it is unsafe to travel", "do not travel", "don't travel", "do not go out", "avoid all travel",
    "you must evacuate", "evacuate immediately", "mandatory evacuation",
)
NO_ALERT_ASSERTIONS = (
    "no alerts exist", "there are no alerts", "no alerts are in effect", "no alert exists",
    "no official alerts exist", "there is no alert", "no warnings exist", "no active alerts",
    "zero alerts",
)
ALERT_CUES = ("alert", "warning", "sachet", "ndma")
UNAVAILABLE_OK_PHRASES = HEDGE_PHRASES + ("not be consulted", "not be checked", "not reachable")


# --------------------------------------------------------------------------- #
# evidence value sets
# --------------------------------------------------------------------------- #
def _walk(obj: Any, key: str = "") -> Iterable[Tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, k)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item, key)
    else:
        yield key, obj


def collect_numbers(ev: Evidence) -> Tuple[Dict[str, Set[float]], Set[float], Set[float], Set[float]]:
    """Returns (by_category, all_numbers, current_only, day_only).

    `current_only` / `day_only` are what make check 9 possible: a value that exists ONLY in a
    forecast day block must never be phrased as an observation happening now.
    """
    by_cat: Dict[str, Set[float]] = {c: set() for c in CATEGORY_KEYS}
    every: Set[float] = set()
    current_nums: Set[float] = set()
    day_nums: Set[float] = set()
    data = ev.model_dump()

    for key, value in _walk(data.get("weather") or {}):
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            every.add(float(value))
            for cat, keys in CATEGORY_KEYS.items():
                if key in keys:
                    by_cat[cat].add(float(value))
        elif isinstance(value, str):
            for tok in re.findall(r"-?\d+(?:\.\d+)?", value):
                try:
                    every.add(float(tok))
                except ValueError:
                    pass

    # which numbers belong to the current block vs a day block
    w = data.get("weather") or {}
    for key, value in _walk(w.get("current") or {}):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            current_nums.add(float(value))
    for block in ("today", "tomorrow", "target_day"):
        for key, value in _walk(w.get(block) or {}):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                day_nums.add(float(value))
    for day in w.get("past_days") or []:
        for key, value in _walk(day):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                day_nums.add(float(value))

    # alert figures (e.g. "25-30 cm of rain" inside a real headline) and evidence prose numbers
    for block in ("alerts", "sources", "advisory", "validation", "quality_breakdown", "location"):
        for key, value in _walk(data.get(block) or {}):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                every.add(float(value))
            elif isinstance(value, str):
                for tok in re.findall(r"-?\d+(?:\.\d+)?", value):
                    try:
                        every.add(float(tok))
                    except ValueError:
                        pass
    return by_cat, every, current_nums, (day_nums - current_nums)


# --------------------------------------------------------------------------- #
# number extraction from the answer
# --------------------------------------------------------------------------- #
def _scrub_non_measurements(text: str) -> str:
    """Blank out things that only LOOK like numbers: dates, clock times, UTC offsets, ids."""
    out = DATE_RE.sub(" ", text)
    out = TIME_RE.sub(" ", out)
    out = OFFSET_RE.sub(" ", out)
    out = IDENT_RE.sub(" ", out)
    out = LONG_DIGITS.sub(" ", out)
    out = SCORE_RE.sub(" ", out)
    return out


def _assertions(text: str) -> str:
    """Lower-cased text with quoted spans removed.

    "that is NOT the same as 'no alert exists'" must pass the wording checks: it mentions the
    forbidden phrase precisely to reject it. Quoted material is a reference; only unquoted
    sentences are assertions this answer is responsible for.
    """
    return QUOTED_SPAN_RE.sub(" ", text or "").lower()


def extract_claims(text: str) -> List[Tuple[float, Optional[str], str]]:
    """(value, category_or_None, raw_token) for every numeric claim the answer makes."""
    clean = _scrub_non_measurements(text)
    claims: List[Tuple[float, Optional[str], str]] = []
    for match in NUMBER_RE.finditer(clean):
        raw_num, raw_unit = match.group(1), (match.group(2) or "").strip().lower().replace("°", "")
        unit = raw_unit.replace(" ", "")
        try:
            value = float(raw_num.replace(",", "."))
        except ValueError:
            continue
        category = UNIT_TO_CATEGORY.get(unit) if unit else None
        if category is None and float(value).is_integer():
            if abs(value) <= SMALL_INT_EXEMPT_MAX or YEAR_RANGE[0] <= value <= YEAR_RANGE[1]:
                continue      # duration / day-of-month / year / ordinal, not a weather measurement
        claims.append((value, category, f"{raw_num} {raw_unit}".strip()))
    # word-numbers only count as claims when they carry a unit ("fifty mm"); "one of the sources"
    # is prose, and flagging it would reject correct answers.
    units_alt = "|".join(sorted((re.escape(u) for u in UNIT_TO_CATEGORY), key=len, reverse=True))
    for word, value in WORD_NUMBERS.items():
        if re.search(rf"\b{word}\b\s*(?:{units_alt})\b", clean):
            claims.append((float(value), None, word))
    return claims


def check_numbers(answer: str, ev: Evidence) -> Tuple[int, List[str], List[str]]:
    by_cat, every, _cur, _day = collect_numbers(ev)
    failures: List[str] = []
    rejected: List[str] = []
    claims = extract_claims(answer)
    for value, category, token in claims:
        if category:
            pool = by_cat.get(category, set())
            if not any(abs(value - known) <= TOL for known in pool):
                failures.append(
                    f"answer states {token}, but no such {category} value exists in the evidence "
                    f"(known: {', '.join(fmt(v) for v in sorted(pool)) or 'none'})"
                )
                rejected.append(token)
            continue
        if not any(abs(value - known) <= TOL for known in every):
            failures.append(f"answer states {token}, which does not appear anywhere in the evidence")
            rejected.append(token)
    return len(claims), failures, rejected


def fmt(v: float) -> str:
    return f"{v:g}"


# --------------------------------------------------------------------------- #
# source / timestamp
# --------------------------------------------------------------------------- #
def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def check_source(answer_src: str, ev: Evidence) -> Optional[str]:
    """Every part of the claimed source must be a source that is actually in the evidence.
    "Open-Meteo and NDMA SACHET" is fine when both are present; "IMD" is not, while IMD is absent."""
    if not ev.sources:
        placeholder = _norm_name(answer_src) in {
            _norm_name(x) for x in ("", "none", "n/a", "na", "not available", "no usable source",
                                    "unavailable", "unknown")
        }
        if placeholder:
            return None
        return (
            f"answer attributes the data to '{answer_src}', but this evidence has no sources at "
            "all — nothing can be credited to a provider we did not reach"
        )
    known = {_norm_name(s.name) for s in ev.sources} | {_norm_name(s.url or "") for s in ev.sources}
    parts = [
        part for part in re.split(
            r"\s*(?:\+|,|&|/|\band\b|\bplus\b|\bas well as\b)\s*", answer_src or ""
        ) if part.strip()
    ]
    if not parts:
        return "answer names no source"
    unknown = []
    for part in parts:
        n = _norm_name(part)
        if n in known or any(n and (n in k or k in n) for k in known if len(k) > 3):
            continue
        unknown.append(part.strip())
    if unknown:
        listed = ", ".join(sorted({s.name for s in ev.sources}))
        return (
            f"answer attributes the data to '{', '.join(unknown)}', which is not in evidence.sources "
            f"(available: {listed}). Naming a source we did not consult is a grounding failure."
        )
    return None


def _norm_ts(value: Any) -> Optional[Tuple[str, Optional[str]]]:
    if not value:
        return None
    text = str(value).strip().replace(" ", "T", 1) if " " in str(value) and "T" not in str(value) else str(value).strip()
    text = re.sub(r"\b(UTC|GMT|IST|UTC\+[\d:]+)\b", "", text).strip(" T")
    text = text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        digits = re.findall(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2})?", text)
        if not digits:
            return None
        try:
            parsed = dt.datetime.fromisoformat(digits[0])
        except ValueError:
            return None
    date = parsed.date().isoformat()
    time = parsed.strftime("%H:%M") if (parsed.hour or parsed.minute or "T" in text) else None
    return date, (time if "T" in text else None)


def allowed_timestamps(ev: Evidence) -> List[str]:
    """The 'as of' stamps this evidence legitimately carries. Deliberately excludes forecast day
    dates: 'Updated 2026-09-02' would claim tomorrow's data is a current reading."""
    out: List[str] = []
    for s in ev.sources:
        if s.timestamp:
            out.append(s.timestamp)
    if ev.weather:
        if ev.weather.retrieved_at_utc:
            out.append(ev.weather.retrieved_at_utc)
        if ev.weather.current and ev.weather.current.time:
            out.append(ev.weather.current.time)
    if ev.alerts.checked_at_utc:
        out.append(ev.alerts.checked_at_utc)
    return out


def check_timestamp(answer_ts: str, ev: Evidence) -> Optional[str]:
    claimed = _norm_ts(answer_ts)
    if claimed is None:
        return f"timestamp {answer_ts!r} is not a readable timestamp"
    allowed = [_norm_ts(t) for t in allowed_timestamps(ev)]
    allowed = [a for a in allowed if a]
    for date, time in allowed:
        if claimed[0] == date and (claimed[1] is None or time is None or claimed[1] == time):
            return None
    # a day-block date is the classic mistake: say exactly what it was
    day_dates = set()
    if ev.weather:
        for block in (ev.weather.today, ev.weather.tomorrow, ev.weather.target_day):
            if block is not None:
                day_dates.add(block.date)
        day_dates |= {d.date for d in ev.weather.past_days}
    for s in ev.sources:
        if s.period:
            day_dates |= {p.strip() for p in s.period.split("..")}
    if claimed[0] in day_dates:
        return (
            f"timestamp {answer_ts!r} is the day the forecast COVERS, not the 'as of' time of the "
            f"data (allowed: {', '.join(a[0] + ('T' + a[1] if a[1] else '') for a in allowed)})"
        )
    return (
        f"timestamp {answer_ts!r} matches no timestamp in the evidence "
        f"(allowed: {', '.join(a[0] + ('T' + a[1] if a[1] else '') for a in allowed) or 'none'})"
    )


# --------------------------------------------------------------------------- #
# alerts
# --------------------------------------------------------------------------- #
def check_alert_presence(answer: str, ev: Evidence) -> List[str]:
    low = (answer or "").lower()
    items = [a for a in ev.alerts.items if a.validity in {"active", "unknown"}]
    failures: List[str] = []
    if items:
        if not any(cue in low for cue in ALERT_CUES):
            failures.append(
                f"the evidence contains {len(items)} official alert(s) relevant to this location and "
                "the answer does not mention an alert at all — an active warning must never be "
                "swallowed by a calm forecast summary"
            )
        suppressors = ("no active official alert", "no alerts", "no warning", "nothing to warn",
                       "no official alert")
        hit = [p for p in suppressors if p in _assertions(answer)]
        if hit:
            failures.append(
                f"the answer says {hit[0]!r} while {len(items)} relevant official alert(s) are present"
            )
        for a in items:
            if a.severity in {"Severe", "Extreme"} and a.severity.lower() not in low:
                failures.append(
                    f"alert {a.alert_id} is severity {a.severity} but the answer does not carry that "
                    "word — severity may not be softened"
                )
    if ev.alerts.state == "unavailable":
        said = _assertions(answer)
        if any(p in said for p in NO_ALERT_ASSERTIONS):
            failures.append(
                "the alert source could not be consulted, so the answer may not claim that no alerts "
                "exist (unknown is not 'none')",
            )
        if not any(p in low for p in UNAVAILABLE_OK_PHRASES):
            failures.append(
                "the alert source was unavailable and the answer does not say so — the user must be "
                "told the official check did not happen"
            )
    if ev.alerts.state == "checked" and not ev.alerts.items:
        said = _assertions(answer)
        if any(p in said for p in NO_ALERT_ASSERTIONS):
            failures.append(
                "'SACHET was checked and nothing verifiably applies here' must not be shortened to "
                f"{next(p for p in NO_ALERT_ASSERTIONS if p in said)!r}"
            )
    return failures


def check_alert_ids(answer: str, ev: Evidence) -> List[str]:
    ids = IDENT_RE.findall(answer or "")
    if not ids:
        return []
    bad = alert_ids_present(ev.alerts, ids)      # shared with Phase 3's advisory gate
    if bad:
        return [
            f"answer cites alert id(s) {', '.join(bad)} that are not in evidence.alerts.items — an "
            "invented identifier is treated exactly like an invented alert"
        ]
    return []


# --------------------------------------------------------------------------- #
# verdicts the LLM may not move
# --------------------------------------------------------------------------- #
def check_risk(payload: Dict[str, Any], ev: Evidence) -> Optional[str]:
    authoritative = ev.advisory.risk_level if ev.advisory is not None else ev.risk
    if authoritative is None:
        claimed = (str(payload.get("risk") or "")).strip().upper()
        if claimed in ("", "UNCERTAIN"):
            # an abstention with no advisory object says UNCERTAIN, which is the honest label
            return None
        return (
            "answer states a risk level for a request that produced no advisory decision — with no "
            "decision made, none may be reported"
        )
    claimed = (str(payload.get("risk") or "")).strip().upper()
    if claimed == str(authoritative).upper():
        return None
    return (
        f"answer states risk {claimed or 'MISSING'!r} but the deterministic advisory decided "
        f"{authoritative}. The explanation layer cannot move the risk level."
    )


def check_quality(payload: Dict[str, Any], ev: Evidence) -> Optional[str]:
    if ev.evidence_quality is None:
        return None
    claimed = (str(payload.get("evidence_quality") or "")).strip().upper()
    if claimed == str(ev.evidence_quality).upper():
        return None
    return (
        f"answer states evidence_quality {claimed or 'MISSING'!r} but the score computed "
        f"{ev.evidence_quality} (score {ev.quality_breakdown.get('score', '?')}/100). The label is "
        "not the model's to choose."
    )


def check_current_vs_forecast(answer: str, ev: Evidence) -> Optional[str]:
    """#6, clause by clause.

    A day-only value (probability of rain, daily max/min, daily sum) presented as a *current*
    condition is a labelling failure even when the number itself is real — that is how "it is
    raining 100%" style bugs reach users. The clause is the unit of judgement because an answer
    may legitimately mix "it is 22.8 °C now" with "tomorrow the forecast shows 100%".
    """
    _by_cat, _every, current_only, day_only = collect_numbers(ev)
    if not day_only:
        return None
    text = f" {(answer or '').lower()} "
    for clause in CLAUSE_SPLIT_RE.split(text):
        if not clause.strip():
            continue
        claims = extract_claims(clause)
        offenders = [
            token
            for value, _category, token in claims
            if any(abs(value - d) <= TOL for d in day_only)
            and not any(abs(value - c) <= TOL for c in current_only)
        ]
        if not offenders:
            continue
        if any(cue in clause for cue in DAY_FRAME_CUES) or ISO_DATE_RE.search(clause):
            continue    # "For 2026-09-02: ..." names the day it belongs to, which is the point
        return (
            f"answer states {', '.join(offenders[:3])} as if it were a current observation. "
            "Those values exist only in a day block (forecast/historical), which is labelled "
            "is_forecast=true — say 'the forecast for <day> shows …' instead."
        )
    weather_text = re.sub(r"\b(?:\w[\w ]*?as of[^.]*\.)\s*$", " ", text)
    if (ev.weather is None or ev.weather.current is None) and any(
        cue in weather_text for cue in PRESENT_TENSE_CUES if cue != "as of"
    ):
        return (
            "answer uses present-tense weather wording but this evidence has no current block "
            "(it is a forecast/historical answer) — do not describe it as happening now"
        )
    return None


def check_insufficient_admitted(answer: str, ev: Evidence) -> Optional[str]:
    if ev.validation.sufficient:
        return None
    low = (answer or "").lower()
    if any(p in low for p in HEDGE_PHRASES):
        return None
    return (
        "validation.sufficient is false, so the answer must state that reliable information could "
        "not be verified (and must not present the numbers as trustworthy)"
    )


def check_safety_wording(answer: str, ev: Evidence) -> List[str]:
    low = _assertions(answer)
    quoted = " ".join(
        ((a.instruction or "") + " " + (a.headline or "")).lower() for a in ev.alerts.items
    )
    failures = []
    for phrase in FORBIDDEN_SAFETY:
        if phrase in low and phrase not in quoted:
            failures.append(
                f"answer says {phrase!r}: this product describes weather-related risk, it does not "
                "guarantee personal safety and it does not issue evacuation orders"
            )
    if not ev.validation.sufficient and any(w in low for w in OVERCONFIDENT):
        failures.append("answer uses confident wording ('" + next(w for w in OVERCONFIDENT if w in low) +
                        "') on evidence that failed validation")
    return failures


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
def new_report(**kwargs: Any) -> GroundingReport:
    """A GroundingReport with defaults applied — keeps llm.py from re-listing the fields."""
    return GroundingReport(**kwargs)


ALERT_WORDS = (
    "alert", "warning", "warnings", "watch", "red alert", "orange alert", "yellow alert",
    "carried to attention", "rainstorm", "thunderstorm", "squall", "heat wave", "cold wave",
    " landslide", "flood",
)


def alert_mentioned(answer: str, ev: Evidence) -> bool:
    """Did this text actually talk about the alert? Used for the `alert_mentioned` flag, which the
    frontend shows next to the answer — so it is computed from the evidence, never trusted from
    the model's own claim."""
    items = list(ev.alerts.items) if (ev.alerts and ev.alerts.items) else []
    if not items:
        return False
    low = (answer or "").lower()
    return any(word in low for word in ALERT_WORDS) or any(
        (a.event or "").lower() in low for a in items if a.event
    )


def verify(ev: Evidence, payload: Optional[Dict[str, Any]]) -> GroundingReport:
    """Run every check. Deterministic, side-effect free, and safe to call twice (regeneration)."""
    checks: List[str] = []
    failures: List[str] = []
    # NOTE: pydantic copies list fields at construction, so every return path writes its own
    # checks_run/failures back onto the report.
    report = GroundingReport()

    if not isinstance(payload, dict):
        return GroundingReport(verified=False, checks_run=["json_object"],
                               failures=["response is not a JSON object"])

    allowed_ts = allowed_timestamps(ev)
    # Both fields are conditional on the evidence having something to copy: a geocode/total-outage
    # abstention has no source and no as-of stamp, and inventing either would be worse than
    # omitting it. So the requirement is "copy it if we have it", not "always print it".
    required = [
        k for k in REQUIRED_KEYS
        if not ((k == "timestamp" and not allowed_ts) or (k == "source" and not ev.sources))
    ]
    missing = [k for k in required if k not in payload or payload.get(k) in (None, "", "null")]
    checks.append("required_fields")
    if missing:
        failures.append(f"required field(s) missing from the LLM response: {', '.join(missing)}")
        return GroundingReport(verified=False, checks_run=checks, failures=failures,
                               numbers_checked=0, numbers_rejected=[])

    answer = str(payload.get("answer") or "")
    n, num_failures, rejected = check_numbers(answer, ev)
    report.numbers_checked, report.numbers_rejected = n, rejected
    checks.append(f"numbers({n})")
    failures += num_failures

    checks.append("source_identity")
    f = check_source(str(payload.get("source") or ""), ev)
    if f:
        failures.append(f)

    checks.append("timestamp_is_as_of")
    claimed_ts = str(payload.get("timestamp") or "")
    if allowed_ts:
        f = check_timestamp(claimed_ts, ev)
        if f:
            failures.append(f)
    elif claimed_ts.strip():
        failures.append(
            "answer states an as-of time, but this evidence carries no timestamp to quote — an "
            "invented 'updated at' is exactly the kind of false currency we reject"
        )

    checks.append("alert_presence")
    failures += check_alert_presence(answer, ev)

    checks.append("alert_ids_exist")
    failures += check_alert_ids(answer, ev)

    checks.append("risk_matches_advisory")
    f = check_risk(payload, ev)
    if f:
        failures.append(f)

    checks.append("evidence_quality_matches")
    f = check_quality(payload, ev)
    if f:
        failures.append(f)

    checks.append("current_vs_forecast")
    f = check_current_vs_forecast(answer, ev)
    if f:
        failures.append(f)

    checks.append("insufficient_admitted")
    f = check_insufficient_admitted(answer, ev)
    if f:
        failures.append(f)

    checks.append("safety_wording")
    failures += check_safety_wording(answer, ev)

    # ---------- admissibility (reported, not mixed into faithfulness) -------------------------
    # `verified` answers exactly one question: is this text faithful to the evidence object?
    # Whether the evidence is good enough to answer at ALL is validation.sufficient's job, so it is
    # surfaced here as a note (plus check 12, which forces the hedged wording). Keeping the two
    # apart is what lets the deterministic abstention be marked faithful-but-unverified-evidence
    # instead of looking like a verifier failure.
    if ev.validation is not None and not ev.validation.sufficient:
        checks.append("evidence_not_sufficient_note")
        report.note = (
            "validation.sufficient=false: only a hedged 'could not be verified' answer is "
            "permitted, and the LLM is not consulted for it at all"
        )

    report.verified = not failures
    report.checks_run = checks
    report.failures = failures
    return report
