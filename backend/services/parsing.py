"""
parsing.py — Phase 1 minimal, deterministic extraction.

This is intentionally the rule-based fallback from the build plan (mBERT+LoRA classifier
gets swapped in as `router.classify()` later; it must return the same ParsedQuery fields).
Explainable beats clever here: every decision carries a reason string we can show judges.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from backend.models import ParsedQuery, Timeframe

# --- timeframe patterns (order matters: more specific first) --------------- #
TIMEFRAME_PATTERNS: list[tuple[str, Timeframe, str]] = [
    (r"\bright now\b|\bcurrently\b|\bnow\b|\bat the moment\b|\bpresent\b", "now", "matched now-keywords"),
    (r"\btomorrow\b|\btmrw\b|\bnext morning\b", "tomorrow", "matched tomorrow-keywords"),
    (r"\btoday\b|\bthis morning\b|\bthis evening\b|\btonight\b", "today", "matched today-keywords"),
    (
        r"\byesterday\b|\bthe day before yesterday\b|\blast (?:night|evening|monday|tuesday|wednesday|thursday|friday|saturday|sunday|week)\b",
        "past",
        "matched past-keywords",
    ),
    (r"\bthis week\b|\bnext week\b|\bthis weekend\b|\bnext weekend\b", "specific_day", "matched week/weekend"),
]

DATE_IN_TEXT = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")

# --- location patterns ------------------------------------------------------ #
LOCATION_PATTERNS = [
    re.compile(r"\b(?:in|at|for|near|around|over|to)\s+([a-z\u0900-\u097F][a-z0-9\u0900-\u097F'.,\s-]{1,50}?)"
               r"\s+(?:right now|currently|tomorrow|today|tonight|this evening|this morning|now|[?.!])", re.I),
    re.compile(r"\bweather (?:in|at|for|of)\s+([a-z\u0900-\u097F][a-z0-9\u0900-\u097F'.,\s-]{1,50}?)"
               r"\s*(?:tomorrow|today|right now|currently|this week|\?|$)", re.I),
    re.compile(r"\ballert[s]? (?:in|for|at|issued for)\s+([a-z\u0900-\u097F][a-z0-9\u0900-\u097F'.,\s-]{1,50}?)"
               r"\s*(?:today|right now|now|\?|$)", re.I),
    re.compile(r"\b(?:rain|raining|snow|temperature|heat|humidity|wind) (?:in|at|for)\s+"
               r"([a-z\u0900-\u097F][a-z0-9\u0900-\u097F'.,\s-]{1,50}?)(?:\s|,|\?|$)", re.I),
]

FILLER = re.compile(
    r"\b(any|an?|the|weather|forecast|conditions?|please|tell me|what'?s|what is|how is|is there|do you know)\b",
    re.I,
)


def extract_timeframe(text: str, today: Optional[dt.date] = None) -> tuple[Timeframe, str, Optional[str]]:
    """`today` is accepted only for tests; production timeframes stay relative strings so the
    location's own timezone decides which calendar day "tomorrow" means."""
    t = text.lower()
    m = DATE_IN_TEXT.search(t)
    if m:
        # An explicit calendar date is the ONLY case where we pin target_date here: the
        # backend must not re-derive "today" from the SERVER clock (UTC) because the user's
        # city runs on its own clock. Live bug found in Phase 1 testing: "tomorrow" was
        # resolved to the UTC date and silently queried today.
        return "specific_day", "matched explicit YYYY-MM-DD date", m.group(1)
    for pattern, label, reason in TIMEFRAME_PATTERNS:
        if re.search(pattern, t):
            return label, reason, None
    return "unspecified", "no explicit timeframe word found", None


def extract_location(text: str) -> tuple[Optional[str], str]:
    """Return (place_phrase, reason). We never guess a city if the pattern misses."""
    # Dates are timeframe evidence, never part of a place name. Without this, "weather in
    # Ahmedabad on 2026-08-25" captured "ahmedabad on 2026" and failed geocoding (live bug,
    # found by running the pipeline).
    text = DATE_IN_TEXT.sub(" ", text)
    text = re.sub(r"\b(?:on|of|for)?\s*\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?(?:\s+\d{4})?", "", text, flags=re.I)
    for i, pattern in enumerate(LOCATION_PATTERNS):
        m = pattern.search(text)
        if m:
            raw = m.group(1).strip(" .,?'\"")
            cleaned = FILLER.sub(" ", raw)
            cleaned = re.sub(r"\b(tomorrow|today|right now|currently|now|tonight)\b", "", cleaned, flags=re.I)
            cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .,?'\"")
            if cleaned:
                return cleaned, f"pattern#{i + 1} preposition/keyword capture"
    return None, "no location pattern matched"


def classify_intent(
    text: str, timeframe: Timeframe, today: Optional[dt.date] = None, target_date: Optional[str] = None
) -> tuple[str, str]:
    """
    Phase 1 keyword router. Returns (intent, reason).
    Replace the body with a call to the mBERT+LoRA service when it is a callable function;
    keep this signature so nothing else changes.
    """
    t = text.lower()
    if re.search(r"\balert[s]?\b|\bwarning[s]?\b|\bwatch\b|\balarm\b|\bSACHET\b|\bIMD warning\b", t):
        return "official_alert", "matched alert/warning keywords"
    if re.search(
        r"\bsafe\b|\bshould i\b|\bcan i\b|\brisk\b|\btravel\b|\bcommut\w*\b|\bdrive\b|\bouting\b"
        r"\bflights?\b|\bplan(e|es)? a\b|\bcancel\w*\b|\bpickleball\b|\bmarathon\b|\boutdoor\b",
        t,
    ):
        return "advisory_risk", "matched travel/safety/activity keywords"
    # An explicit PAST date is a historical question even if the wording says "weather in".
    is_past_date = False
    if target_date:
        try:
            is_past_date = dt.date.fromisoformat(target_date) < (today or dt.date.today())
        except ValueError:
            is_past_date = False
    if timeframe == "past" or is_past_date or re.search(
        r"\bhistor\w+\b|\bclimate\b|\baverage\b|\bnormals?\b|\byesterday\b|\blast year\b|\bprevious\b", t
    ):
        return "historical_climate", "matched historical/climate keywords or past timeframe"
    if re.search(r"\?.*$", t) or re.search(
        r"\bweather\b|\bforecast\b|\btemperature\b|\brain\b|\bsnow\b|\bwind\b|\bhumid\w*\b", t
    ):
        return "forecast_current", "matched weather/forecast vocabulary"
    return "clarification_needed", "no weather signal found in the message"


def parse(text: str, today: Optional[dt.date] = None) -> ParsedQuery:
    text = (text or "").strip()
    timeframe, tf_reason, target_date = extract_timeframe(text, today=today)
    location, loc_reason = extract_location(text)
    intent, intent_reason = classify_intent(text, timeframe, today=today, target_date=target_date)
    notes = [f"location: {loc_reason}", f"timeframe: {tf_reason}"]
    if location is None:
        notes.append("no place name found in the message -> caller must ask for clarification")
        if intent != "advisory_risk":
            intent = "clarification_needed"
            intent_reason = "location missing"
    return ParsedQuery(
        message=text,
        intent=intent,  # type: ignore[arg-type]
        intent_reason=intent_reason,
        location_text=location,
        timeframe=timeframe,  # type: ignore[arg-type]
        timeframe_reason=tf_reason,
        target_date=target_date,
        notes=notes,
    )
