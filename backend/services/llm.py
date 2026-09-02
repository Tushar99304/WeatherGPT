"""
llm.py — Phase 4: the grounded explanation layer (Groq, OpenAI-compatible chat completions).

THE ONE RULE
    The model receives EXACTLY `evidence.model_dump()`. No tools, no history, no retrieval, no
    second data call. It turns a validated evidence object into a sentence; it cannot turn a
    sentence into evidence. Everything the model may assert is already decided upstream:
    `advisory.risk_level`, `evidence_quality`, `sources[]`, `alerts[]`, `validation.sufficient`.

WHY THE PROMPT LOOKS RESTRICTIVE
    It is a copy of docs/PLAN_48H.md §F extended with the four rules that Phase 4 has to enforce
    (risk preservation, no tools, alert-id discipline, unavailable-vs-no-alerts). The prompt is
    not the guard: `grounding.verify()` is. A prompt makes the model usually right; the verifier
    makes it provably never wrong about the four fields that matter.

FAILURE POLICY (the whole point of this phase)
    no key / disabled / timeout / HTTP error / unparseable JSON / grounding rejection after one
    regeneration  ->  `deterministic_answer()` builds the sentence from evidence values only.
    The user still gets a grounded answer with a visible origin badge; the weather product never
    becomes unavailable because the LLM is. `llm_status` on the trace records which path ran, so
    the demo can show it instead of asserting it.

GROQ NOTES (verified against their docs before writing this)
    base https://api.groq.com/openai/v1/chat/completions, OpenAI-compatible, model
    llama-3.3-70b-versatile supports `response_format={"type":"json_object"}`, which is why the
    answer arrives as JSON and needs no prose parsing.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from backend import config
from backend.models import Evidence, GroundedAnswer, GroundingReport
from backend.services import grounding, http_client
from backend.services import validation as validation_service

# --------------------------------------------------------------------------- #
# prompt
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """You are the explanation layer of WeatherGPT, a travel-risk assistant for Maharashtra.

You receive ONE JSON object: the verified evidence for a single user question. It is the whole
world for this turn. You have no tools and you must not ask for more data.

Produce ONLY a JSON object, no prose around it, with exactly these keys:
  "answer":            one or two sentences of plain Indian English answering the question asked.
  "source":            the provider name(s) to credit, copied from evidence.sources[].name.
  "timestamp":         the "as of" stamp, copied from evidence.sources[].timestamp or
                       evidence.weather.retrieved_at_utc.
  "risk":              the value of evidence.advisory.risk_level, copied exactly.
  "evidence_quality":  the value of evidence.evidence_quality, copied exactly.

Hard rules. Each one is checked programmatically after you reply, and a single violation discards
your entire answer, so do not test them:
1. Every number you write must appear in the evidence object, with the same unit, to one decimal
   place. Never compute, average, round up, convert units, or fill a gap with a plausible value.
   If a value is not in the evidence, do not mention it.
2. You have no tools. Do not request data, and do not mention data you were not given.
3. Do not change the risk level or the evidence quality. If the advisory says MEDIUM, the answer
   says the risk is MEDIUM. You may explain WHY (the rules in evidence.advisory.rules_fired and
   warnings), you may not decide otherwise.
4. If evidence.alerts.items is not empty, the answer MUST mention that official alert (its event
   and severity) before or while answering. Silence about a live warning is the most dangerous
   failure you can commit.
5. Only cite alert identifiers that literally appear in evidence.alerts.items[].alert_id. Never
   invent one.
6. If evidence.alerts.state is "unavailable" or "not_checked", say that the official alert service
   could not be verified at this time. That is NOT the same as "there are no alerts" — never imply
   the absence of alerts in that case.
7. When a value you need is missing from the evidence, say that it is not available. Do not use a
   neighbouring day's value, a regional average, or a number from your own memory of the climate.
8. A value that only exists in a forecast day block (today/tomorrow/target_day, is_forecast=true)
   must be attributed to that day and timeframe. Never describe it as "now" or "currently".
9. Quote the "as of" time you copied. Do not present a value as more current than it is.
10. If evidence.validation.sufficient is false, say plainly that reliable information could not be
    verified and that you will not guess. Do not soften it, and do not summarise the rejected
    numbers as if they were trustworthy.
11. Never guarantee personal safety, never order an evacuation, never claim official authority.
    State the weather-related risk and what the evidence is.
12. No greetings, no advice beyond the data, no bullet lists, no markdown, no disclaimers added
    to the ones above. Keep the answer under 60 words.
"""

# The regeneration message: exact failures, one stricter attempt, same evidence object.
REGEN_HEADER = (
    "Your previous reply was rejected by the grounding verifier. It is the ONLY correction you "
    "will get, and if anything on this list is still wrong your reply will be discarded and a "
    "deterministic answer will be shown instead. Re-read the hard rules, then answer again from "
    "the same evidence object. Reject reasons:\n"
)


def evidence_json(ev: Evidence) -> str:
    """The single user message: the evidence dump, nothing else, stable key order."""
    return json.dumps(
        ev.model_dump(mode="json", exclude_none=False), ensure_ascii=False, sort_keys=False
    )


def build_messages(ev: Evidence, failures: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """system + ONE user message. No history, by construction: the list is always 2 items
    (3 on the regeneration, where the second user turn only contains the verifier's complaints)."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    user = evidence_json(ev)
    if failures:
        user += "\n\n" + REGEN_HEADER + "\n".join(f"- {f}" for f in failures)
    messages.append({"role": "user", "content": user})
    return messages


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
def _endpoint() -> str:
    return config.GROQ_BASE_URL.rstrip("/") + config.LLM_CHAT_COMPLETIONS_PATH


def llm_configured() -> bool:
    return bool(config.GROQ_API_KEY)


async def _chat(messages: List[Dict[str, str]]) -> str:
    """One Groq chat-completion request, returning the assistant message content as a string."""
    if config.SIMULATE_LLM_FAILURE:
        raise http_client.UpstreamError(
            "groq", "SIMULATE_LLM_FAILURE is set (acts like the endpoint being unreachable)"
        )
    payload: Dict[str, Any] = {
        "model": config.GROQ_MODEL,
        "messages": messages,
        "temperature": config.LLM_TEMPERATURE,
        "max_tokens": config.LLM_MAX_TOKENS,
        "stream": False,
        "include_reasoning": False,
        "reasoning_effort": "low",
    }
    if config.LLM_JSON_MODE:
        payload["response_format"] = {"type": "json_object"}
    body = await http_client.post_json(
        _endpoint(),
        payload=payload,
        service="groq",
        headers={
            "Authorization": f"Bearer {config.GROQ_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "WeatherGPT/1.0",
        },
        timeout=config.LLM_TIMEOUT_S,
        retries=0,
    )
    if not isinstance(body, dict) or not body.get("choices"):
        raise http_client.UpstreamError("groq", "response contained no choices[]")
    message = body["choices"][0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise http_client.UpstreamError("groq", "assistant message content was empty")
    return content


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """Parse the reply into a dict. Tolerates markdown fences and a leading sentence; anything
    that still is not a JSON object returns None so the caller can fall back deterministically."""
    if not raw:
        return None
    text = raw.strip()
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
    except ValueError:
        block = _JSON_BLOCK.search(text)
        if not block:
            return None
        try:
            obj = json.loads(block.group(0))
        except ValueError:
            return None
    return obj if isinstance(obj, dict) else None


# --------------------------------------------------------------------------- #
# deterministic fallback
# --------------------------------------------------------------------------- #
def _stamp(value: Any, *, utc: bool = True) -> str:
    """Minute-precision, unambiguous stamps in the SENTENCE.

    The machine-readable `timestamp` field keeps the evidence's exact string (the verifier compares
    that one); prose gets "2026-09-01T00:59Z" / "…T03:15 local" so a reader can tell which clock is
    meant — the provider answers current conditions in local wall time and everything else in UTC.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", text)
    if not match:
        return text
    date, hhmm = match.groups()
    if not utc:
        return f"{date}T{hhmm} local"
    return f"{date}T{hhmm}Z" if ("Z" in text or "+" in text or "-" in text[10:]) else f"{date}T{hhmm} UTC"


def _punct(text: str) -> str:
    text = (text or "").strip()
    return text + "." if text and not text.endswith((".", "!", "?")) else text


def _safe_quote(text: str) -> str:
    """An official text prepared for verbatim quoting inside our answer.

    The words are NOT altered (a paraphrased instruction is no longer the authority's), but
    whitespace is collapsed and inner double quotes become single ones, because the grounding
    verifier strips quoted spans before judging wording — an unbalanced quote would let the
    quote swallow prose this answer is responsible for.
    """
    return re.sub(r"\s+", " ", (text or "").strip()).replace('"', "'")


def _fmt(value: Any) -> str:
    """Numbers are rendered exactly as the evidence holds them (°C to one decimal, mm as-is)."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if abs(value - round(value)) < 1e-9:
            return str(int(round(value)))
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return str(value)


def _sources(ev: Evidence) -> str:
    names: List[str] = []
    for src in ev.sources:
        if src.name and src.name not in names:
            names.append(src.name)
    return " + ".join(names) if names else "no usable source"


def _as_of(ev: Evidence) -> Optional[str]:
    for src in ev.sources:
        if src.timestamp:
            return src.timestamp
    if ev.weather and ev.weather.retrieved_at_utc:
        return ev.weather.retrieved_at_utc
    if ev.alerts and ev.alerts.checked_at_utc:
        return ev.alerts.checked_at_utc
    return None


def _answered_block(ev: Evidence) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """(date, values) for the day the question was about, or (None, None) for a current answer.

    Reuses `validation.answered_day`, the same selector Phase 3 used to decide which day block the
    answer must come from — so the fallback cannot drift onto a different day than the validation
    and advisory already agreed on.
    """
    request = ev.request or {}
    timeframe = str(request.get("timeframe") or "now").lower()
    if timeframe in ("now", "current", "today_and_tomorrow", ""):
        return None, None
    day = validation_service.answered_day(ev.weather, timeframe, request.get("target_date"))
    if day is None:
        return None, None
    return day.date, day.model_dump(mode="json")


def _measurement_sentence(ev: Evidence) -> str:
    date, block = _answered_block(ev)
    if block:
        bits = []
        if block.get("temperature_max_c") is not None:
            bits.append(f"up to {_fmt(block['temperature_max_c'])} °C")
        if block.get("temperature_min_c") is not None:
            bits.append(f"down to {_fmt(block['temperature_min_c'])} °C")
        if block.get("precipitation_sum_mm") is not None:
            chance = block.get("precipitation_probability_max_pct")
            rain = f"{_fmt(block['precipitation_sum_mm'])} mm of rain"
            if chance is not None:
                rain += f" ({_fmt(chance)}% chance)"
            bits.append(rain)
        if block.get("wind_speed_max_kmh") is not None:
            bits.append(f"wind up to {_fmt(block['wind_speed_max_kmh'])} km/h")
        if block.get("condition"):
            bits.append(str(block["condition"]))
        label = f"For {date}" if date else "For the requested day"
        return f"{label}: " + (", ".join(bits) if bits else "no numeric values were returned") + "."
    cur = ev.weather.current if ev.weather else None
    if cur is None:
        return "The evidence carries no current weather block, so no conditions can be stated."
    bits = []
    if cur.temperature_c is not None:
        bits.append(f"{_fmt(cur.temperature_c)} °C")
    if cur.apparent_temperature_c is not None:
        bits.append(f"feels like {_fmt(cur.apparent_temperature_c)} °C")
    if cur.precipitation_mm is not None:
        bits.append(f"{_fmt(cur.precipitation_mm)} mm precipitation")
    if cur.humidity_pct is not None:
        bits.append(f"{_fmt(cur.humidity_pct)}% humidity")
    if cur.wind_speed_kmh is not None:
        bits.append(f"wind {_fmt(cur.wind_speed_kmh)} km/h")
    if cur.condition:
        bits.append(str(cur.condition))
    if not bits:
        return "The current block carried no usable values."
    when = f" as of {_stamp(cur.time, utc=False)}" if cur.time else ""
    return "Currently " + ", ".join(bits) + f"{when}."


def deterministic_payload(ev: Evidence) -> Dict[str, Any]:
    """A grounded answer assembled from evidence values only — no model, no invention.

    Deliberately reuses the wording the advisory/validation layer already produced (the advisory
    headline is already phrased as weather-related risk, not a safety guarantee), so the fallback
    cannot drift from the decision it is supposed to explain.
    """
    payload: Dict[str, Any] = {
        "answer": "",
        "source": _sources(ev),
        "timestamp": _as_of(ev),
        "risk": str(ev.advisory.risk_level if ev.advisory else (ev.risk or "UNCERTAIN")),
        "evidence_quality": str(ev.evidence_quality or "LOW"),
    }
    validation = ev.validation
    if validation is not None and not validation.sufficient:
        reason = validation.failures[0] if validation.failures else (ev.abstain_reason or "")
        # Credit what exists, and only what exists: no sources => no source line; an as-of stamp
        # from the alert check is still quoted so the abstention itself is dated.
        bits = []
        if ev.sources:
            bits.append(f"Source: {payload['source']}")
        if payload["timestamp"]:
            bits.append(("as of " if bits else "Checked at ") + _stamp(payload["timestamp"]))
        tail = (" " + ", ".join(bits) + ".") if bits else ""
        payload["answer"] = (
            "I could not verify reliable weather information for this place and time, so I will "
            "not guess."
            + (f" Reason: {reason}." if reason else "")
            + tail
        )
        if not ev.sources:
            payload["source"] = "no usable source"
        return payload

    parts: List[str] = []
    alerts = ev.alerts
    items = list(alerts.items) if alerts and alerts.items else []
    if items:
        lead = items[0]
        desc = " ".join(x for x in (lead.severity, lead.event) if x)
        if lead.validity == "active":
            line = f"An official {desc} alert is active for {lead.area_desc or 'this area'}"
            if lead.expires_at:
                line += f" until {_stamp(lead.expires_at)}"
        else:
            # U1 boundary: a relevant alert whose temporal window the source left unprovable
            # (validity == "unknown", e.g. no expiry published) must NOT be sold as "active".
            # Only alerts.py's classify_validity() may declare an alert active.
            line = (
                f"An official {desc} alert naming {lead.area_desc or 'this area'} was published, "
                f"but the source does not prove it is active right now "
                f"({_punct(lead.validity_reason or 'temporal window not published')[:-1].lower()})"
            )
        line += ". "
        if lead.headline:
            line += _punct(lead.headline)
        if lead.instruction:
            # U1: the issuing authority's instruction, quoted VERBATIM (never paraphrased, never
            # invented when absent). Attribution stays attached so it cannot read as our advice.
            line += (
                f" Official instruction from "
                f"{lead.sender or lead.author_name or 'the issuing authority'}: "
                f'"{_safe_quote(lead.instruction)}".'
            )
        if len(items) > 1:
            line += f" {len(items)} verified official alerts are attached to this location."
        parts.append(line)
    elif alerts is not None and alerts.state in ("unavailable", "not_checked"):
        parts.append(
            "The official alert service could not be verified at this time, so whether any alert "
            "is active for this location is unknown and no conclusion about alerts can be drawn "
            "from this answer."
        )
    elif alerts is not None and alerts.state == "checked":
        when = f" at {_stamp(alerts.checked_at_utc)}" if alerts.checked_at_utc \
            else " at the time of the check"
        parts.append(
            f"No active official alert was verifiably tied to this location when SACHET was checked{when};"
            " that is a checked result, not a promise that none exists."
        )

    parts.append(_measurement_sentence(ev))

    if ev.advisory is not None and ev.advisory.headline:
        score = (ev.quality_breakdown or {}).get("score")
        line = ev.advisory.headline
        if score is not None:
            line += f" Evidence quality {payload['evidence_quality']} ({_fmt(score)}/100)."
        parts.append(line)
    # validation.warnings are deliberately NOT inlined: they quote the wording they warn about
    # ("...NOT the same as 'no alert exists'"), and a sentence that quotes a forbidden phrase in
    # the fallback would trip the same check the LLM is held to. The evidence panel shows them.

    text = re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip()
    if not text.endswith((".", "!", "?")):
        text += "."
    trailer = f"Source: {payload['source']}"
    if payload["timestamp"]:
        trailer += f", as of {_stamp(payload['timestamp'])}."
    else:
        trailer += "."
    payload["answer"] = f"{text} {trailer}"
    return payload


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def _wrap(payload: Dict[str, Any], ev: Evidence, origin: str, report: GroundingReport) -> GroundedAnswer:
    text = re.sub(r"\s+", " ", str(payload.get("answer") or "")).strip()
    return GroundedAnswer(
        text=text,
        source=(str(payload.get("source")).strip() if payload.get("source") else None) or None,
        timestamp=(str(payload.get("timestamp")).strip() if payload.get("timestamp") else None) or None,
        risk=str(payload.get("risk") or (ev.advisory.risk_level if ev.advisory else "UNCERTAIN")),
        evidence_quality=str(payload.get("evidence_quality") or ev.evidence_quality or "LOW"),
        alert_mentioned=grounding.alert_mentioned(text, ev),
        origin=origin,
        grounding=report,
    )


def _fallback(
    ev: Evidence,
    status: str,
    note: str,
    started: float,
    *,
    rejected: Optional[GroundingReport] = None,
    attempts: int = 0,
    regenerated: bool = False,
) -> Tuple[GroundedAnswer, GroundingReport]:
    """Deterministic answer, put through the SAME verifier a model answer must pass.

    `verified` keeps one meaning throughout this module: the answer that will be displayed is
    faithful to the evidence object. Whatever the model got wrong is still recorded, prefixed with
    "[rejected model reply]", so a reviewer can see why the fallback ran without having to guess.
    """
    payload = deterministic_payload(ev)
    report = grounding.verify(ev, payload)
    report.model = None
    report.llm_status = status
    report.attempts = attempts
    report.regenerated = regenerated
    report.note = note + (f" | {report.note}" if report.note else "")
    report.latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
    if rejected is not None:
        report.failures = [f"[rejected model reply] {f}" for f in rejected.failures] + list(
            report.failures
        )
        report.numbers_rejected = list(rejected.numbers_rejected)
    return _wrap(payload, ev, "deterministic_fallback", report), report


async def explain(ev: Evidence) -> Tuple[GroundedAnswer, GroundingReport]:
    """Ask Groq to explain the evidence, verify it, allow exactly one regeneration, else fall back.

    Never raises and never returns an unverified model sentence: every exit is either a model
    answer that passed all grounding checks or `deterministic_payload(ev)`.
    """
    started = time.perf_counter()

    # The evidence gate comes FIRST: "we do not let a model dress up unverified data" is a safety
    # rule, while "no key" is only an availability one, and the trace should record the stronger
    # reason when both are true.
    if ev.validation is not None and not ev.validation.sufficient:
        return _fallback(
            ev, "skipped",
            "evidence failed validation — the deterministic abstention is the only permitted "
            "answer and the LLM was not consulted",
            started,
        )
    if not config.LLM_ENABLED:
        return _fallback(ev, "disabled", "LLM_ENABLED=false — the LLM was not called", started)
    if not llm_configured():
        return _fallback(
            ev, "no_key", "GROQ_API_KEY is not set — deterministic evidence-based answer used", started
        )

    messages = build_messages(ev)
    report = GroundingReport(model=config.GROQ_MODEL, llm_status="called")
    status = "no_answer"

    for attempt in range(1, max(1, config.LLM_MAX_ATTEMPTS) + 1):
        report.attempts = attempt
        try:
            raw = await _chat(messages)
        except http_client.UpstreamError as exc:
            # no transport retry and no second request: the timeout already cost LLM_TIMEOUT_S and
            # the fallback is instant, so a dead Groq must not make the endpoint slow as well.
            return _fallback(
                ev, "upstream_error",
                "Groq could not be reached — deterministic evidence-based answer used",
                started,
                rejected=GroundingReport(failures=[f"groq request failed: {exc}"]),
                attempts=attempt,
                regenerated=report.regenerated,
            )

        parsed = parse_json_object(raw)
        if parsed is None:
            status = "malformed_json"
            report = grounding.new_report(
                model=config.GROQ_MODEL, llm_status=status, attempts=attempt,
                failures=["the reply was not a JSON object with the required keys"],
            )
            messages = build_messages(ev, report.failures)   # one stricter attempt, same evidence
            continue

        if config.SIMULATE_LLM_HALLUCINATION:
            # Proves the guard fires. The injected sentence is never displayed: it must be rejected
            # below, and what the user sees is then the deterministic answer.
            parsed = dict(parsed)
            parsed["answer"] = (
                f"{parsed.get('answer', '')} It will be exactly 987.6 °C with 12345% chance of "
                "rain, per IMD."
            )

        report = grounding.verify(ev, parsed)
        report.model = config.GROQ_MODEL
        report.attempts = attempt
        if config.SIMULATE_LLM_HALLUCINATION:
            report.note = ((report.note + " | ") if report.note else "") + (
                "SIMULATE_LLM_HALLUCINATION injected numbers absent from the evidence"
            )
        if report.verified:
            report.llm_status = "ok"
            report.latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
            if attempt > 1:
                report.regenerated = True
                report.note = ((report.note + " | ") if report.note else "") + (
                    f"accepted on attempt {attempt} after one regeneration"
                )
            return _wrap(parsed, ev, "groq_llm", report), report

        status = "grounding_failed"
        messages = build_messages(ev, report.failures)       # THE one regeneration
        report.regenerated = True

    return _fallback(
        ev, status,
        "model reply rejected by grounding — deterministic evidence-based answer used",
        started,
        rejected=report,
        attempts=report.attempts,
        regenerated=True,
    )
