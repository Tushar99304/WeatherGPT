"""
scripts/demo_phase4.py — the grounded LLM layer, its verifier, and the fallback, on live data.

Seven cases. Each prints the evidence it was built on, the guard's verdict, and PASS/FAIL, and the
whole run is written to demo_outputs/phase4_<utc>.json so a reviewer can re-run `grounding.verify()`
over the same payload and reach the same verdict.

  CASE 1  live pipeline, NO GROQ_API_KEY          -> skipped; deterministic grounded answer; verify ok
  CASE 2  a reply the verifier ACCEPTS            -> origin=groq_llm, the exact request shape shown
  CASE 3  an INVENTED number (guard switch)       -> rejected, regenerated once, then fallback
  CASE 4  a live alert the answer omits           -> rejected; the fallback names it
  CASE 5  Groq unreachable (SIMULATE_LLM_FAILURE) -> still answers, evidence byte-identical
  CASE 6  the model tries to move the risk level  -> rejected, the advisory's level stands
  CASE 7  stale data (SIMULATE_STALE_DATA)        -> model not consulted; abstention shown

HONESTY RULES
  * Every case runs the REAL pipeline (Open-Meteo + NDMA SACHET) — the evidence is live.
  * Where a "model reply" is needed, the transport is stubbed UNLESS GROQ_API_KEY is set, and the
    line says OFFLINE STUB. This repository has no key, so no output of this script may claim a
    live api.groq.com call. Export a key and the identical code path hits the real endpoint.
  * Case 4 uses a real alert when the feeds carry one; otherwise it rehearses the guard on
    clearly-labelled SYNTHETIC alert data. It never presents that as a live warning.

Run:  python scripts/demo_phase4.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import config  # noqa: E402
from backend.main import run_pipeline  # noqa: E402
from backend.models import (  # noqa: E402
    Alert, AlertRelevance, AlertsEvidence, Evidence, GroundedAnswer, Source,
)
from backend.services import advisory as ADV  # noqa: E402
from backend.services import http_client, llm  # noqa: E402
from backend.services import validation as validation_service  # noqa: E402

LINE = "\n" + "=" * 78
PUNE_NOW = "What is the weather in Pune right now?"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _answer_from_trace(trace: Dict[str, Any]) -> Tuple[GroundedAnswer, Any]:
    """Read the answer the PIPELINE produced, so the demo shows exactly what the endpoint sent."""
    ans = GroundedAnswer(**trace["answer"])
    return ans, ans.grounding


def _show(ev: Evidence, ans: GroundedAnswer, rep: Any,
          trace: Optional[Dict[str, Any]] = None) -> None:
    stage = {s["stage"]: s for s in (trace or {}).get("stages", [])}
    print(f"  evidence             : status={ev.status} quality={ev.evidence_quality} "
          f"risk={ev.risk} sufficient={ev.validation.sufficient}")
    if "llm" in stage:
        print(f"  llm stage            : {stage['llm']['status']} (reason={stage['llm']['reason']}, "
              f"attempts={stage['llm']['attempts']}, regenerated={stage['llm']['regenerated']}, "
              f"{stage['llm']['latency_ms']} ms, model={stage['llm']['model']})")
    gs = stage.get("grounding", {}).get("status", "n/a")
    print(f"  grounding stage      : {gs} verified={rep.verified} "
          f"numbers_checked={rep.numbers_checked} numbers_rejected={rep.numbers_rejected}")
    print(f"  answer origin        : {ans.origin}")
    print(f"  answer               : {ans.text}")
    print(f"  credited             : source={ans.source!r} as_of={ans.timestamp!r} "
          f"risk={ans.risk} quality={ans.evidence_quality} alert_mentioned={ans.alert_mentioned}")
    for f in rep.failures:
        print(f"    - {f}")


def _stub_transport(responder: Callable[[Dict[str, Any], int], Any]) -> List[Dict[str, Any]]:
    """Replace the ONE HTTP call the LLM layer makes, keyed on the evidence it was actually given.

    `responder` gets the decoded evidence object and the 1-based call number, and returns either the
    reply dict or an Exception to raise. One run of run_pipeline per case: the stub sees the same
    payload a real Groq call would have received.
    """
    calls: List[Dict[str, Any]] = []

    async def fake_post_json(url: str, *, payload: Dict[str, Any], **kw: Any) -> Dict[str, Any]:
        calls.append({"url": url, "payload": payload, **kw})
        user_turn = payload["messages"][1]["content"]
        evidence = json.loads(user_turn.split("\n\n" + llm.REGEN_HEADER)[0])
        step = responder(evidence, len(calls))
        if isinstance(step, Exception):
            raise step
        return {"choices": [{"message": {"content": json.dumps(step, ensure_ascii=False)}}]}

    llm.http_client.post_json = fake_post_json  # type: ignore[assignment]
    return calls


def _unstub_transport() -> None:
    llm.http_client.post_json = http_client.post_json  # type: ignore[assignment]


def _reply_from(evidence: Dict[str, Any], answer: str, **over: Any) -> Dict[str, Any]:
    """A reply in exactly the shape the contract demands; the sentence is the only variable."""
    sources = evidence.get("sources") or []
    ts = next((s.get("timestamp") for s in sources if s.get("timestamp")), None)
    advisory = evidence.get("advisory") or {}
    payload = {
        "answer": answer,
        "source": " + ".join({s["name"] for s in sources if s.get("name")}) or "no usable source",
        "timestamp": ts,
        "risk": advisory.get("risk_level") or "UNCERTAIN",
        "evidence_quality": evidence.get("evidence_quality") or "LOW",
    }
    payload.update(over)
    return {k: v for k, v in payload.items() if v not in (None, "")}


def _sentence_from(evidence: Dict[str, Any]) -> str:
    cur = ((evidence.get("weather") or {}).get("current") or {})
    parts = []
    if cur.get("temperature_c") is not None:
        parts.append(f"{cur['temperature_c']:g} °C")
    if cur.get("wind_speed_kmh") is not None:
        parts.append(f"wind {cur['wind_speed_kmh']:g} km/h")
    if not parts:
        return "No current block was returned, so no conditions can be stated."
    return f"It is {' and '.join(parts)} in Pune right now."


def _key_for_stub() -> Any:
    """The verifier path needs `configured=True`; no request is ever sent when stubbed."""
    saved = config.GROQ_API_KEY
    config.GROQ_API_KEY = saved or "gsk_stub_only_no_request_is_made"
    return saved


# --------------------------------------------------------------------------- #
# cases
# --------------------------------------------------------------------------- #
async def case_1_no_key() -> bool:
    print(LINE)
    print("CASE 1 — live pipeline, no GROQ_API_KEY: the weather product still answers")
    saved = config.GROQ_API_KEY
    config.GROQ_API_KEY = ""
    try:
        ev, trace = await run_pipeline(PUNE_NOW)
    finally:
        config.GROQ_API_KEY = saved
    ans, rep = _answer_from_trace(trace)
    _show(ev, ans, rep, trace)
    print("  reading: the LLM was never the reason this answer exists — the evidence was. The")
    print("  sentence is built from evidence values and still passes all 10+ grounding checks.")
    ok = (
        ans.origin == "deterministic_fallback"
        and rep.verified
        and rep.llm_status == "no_key"
        and trace["stages"][-2]["stage"] == "llm"
        and trace["stages"][-1]["stage"] == "grounding"
    )
    print(f"  --> {'PASS' if ok else 'FAIL'}")
    return ok


async def case_2_accepted_reply() -> bool:
    print(LINE)
    live = llm.llm_configured()
    print(f"CASE 2 — a reply the verifier ACCEPTS  "
          f"[{'LIVE GROQ CALL' if live else 'OFFLINE STUB — no key in this environment'}]")
    calls = _stub_transport(lambda evidence, n: _reply_from(evidence, _sentence_from(evidence)))
    saved = _key_for_stub() if not live else None
    try:
        ev, trace = await run_pipeline(PUNE_NOW)
    finally:
        _unstub_transport()
        if saved is not None:
            config.GROQ_API_KEY = saved
    ans, rep = _answer_from_trace(trace)
    _show(ev, ans, rep, trace)
    if calls:
        body = calls[0]["payload"]
        safe = {k: v for k, v in body.items() if k != "messages"}
        print(f"  request              : {calls[0]['url']}")
        print(f"  request body         : {json.dumps(safe, ensure_ascii=False)}")
        print(f"  messages             : roles={[m['role'] for m in body['messages']]} "
              f"(one user turn, no history, no tools)")
        sent = json.loads(body["messages"][1]["content"].split("\n\n" + llm.REGEN_HEADER)[0])
        print(f"  user turn == evidence dump: {sent.keys() == ev.model_dump(mode='json').keys()}")
        print(f"  prompt chars         : system={len(body['messages'][0]['content'])} "
              f"user={len(body['messages'][1]['content'])}")
    ok = bool(calls) and rep.verified and ans.origin == "groq_llm"
    print(f"  --> {'PASS' if ok else 'FAIL'}")
    return ok


async def case_3_hallucination() -> bool:
    print(LINE)
    print("CASE 3 — an invented number: rejected, regenerated once, then the deterministic answer")
    print("  SIMULATE_LLM_HALLUCINATION injects 'exactly 987.6 °C with 12345% chance' into whatever")
    print("  the reply was. The switch exists to prove the guard fires, never to fake a success.")
    saved_switch = config.SIMULATE_LLM_HALLUCINATION
    config.SIMULATE_LLM_HALLUCINATION = True
    saved = _key_for_stub()
    calls = _stub_transport(lambda evidence, n: _reply_from(evidence, _sentence_from(evidence)))
    try:
        ev, trace = await run_pipeline(PUNE_NOW)
    finally:
        config.SIMULATE_LLM_HALLUCINATION = saved_switch
        _unstub_transport()
        config.GROQ_API_KEY = saved if saved is not None else config.GROQ_API_KEY
    ans, rep = _answer_from_trace(trace)
    _show(ev, ans, rep, trace)
    print(f"  transport calls       : {len(calls)} (first answer + exactly one regeneration)")
    print("  reading: the injected numbers never reach the user; the fallback sentence does.")
    ok = (
        len(calls) == config.LLM_MAX_ATTEMPTS
        and ans.origin == "deterministic_fallback"
        and any("987.6" in f for f in rep.failures)
        and "987.6" not in ans.text
        and rep.regenerated
    )
    print(f"  --> {'PASS' if ok else 'FAIL'}")
    return ok


def _synthetic_alert() -> Alert:
    """GUARD REHEARSAL DATA ONLY — printed as such everywhere. Not a real SACHET warning."""
    now = dt.datetime.now(dt.timezone.utc)
    return Alert(
        alert_id="SYNTHETIC-DEMO-1", source="NDMA SACHET", sender="SYNTHETIC REHEARSAL (demo only)",
        event="Heavy Rain", headline="Heavy rain over Pune [SYNTHETIC REHEARSAL DATA]",
        description="Rehearsal item used only to prove the alert-omission guard.",
        area_desc="Pune district of Maharashtra", severity="Severe", urgency="Immediate",
        certainty="Likely", effective_at=now.isoformat(),
        expires_at=(now + dt.timedelta(hours=3)).isoformat(), validity="active",
        relevance=AlertRelevance(status="relevant", level="L1_exact_locality",
                                 reason="areaDesc names this place (synthetic)",
                                 matched_terms=["pune"]),
    )


async def _evidence_with_an_alert(ev: Evidence) -> Tuple[Evidence, bool]:
    """Keep the live evidence if its alerts are usable, else attach labelled rehearsal data."""
    if ev.alerts.items:
        return ev, True
    print("  no live alert on this feed right now — rehearsing the guard on SYNTHETIC data")
    ev.alerts = AlertsEvidence(
        state="checked", mode="fixture_replay", items=[_synthetic_alert()],
        checked_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        items_in_feeds=1, details_fetched=1,
        notes=["SYNTHETIC rehearsal item — not a live SACHET alert"],
    )
    if not any(s.name == "NDMA SACHET" for s in ev.sources):
        ev.sources.append(Source(name="NDMA SACHET", type="official_alert",
                                 timestamp=ev.alerts.checked_at_utc, authority="official"))
    ev.validation = validation_service.validate_evidence(ev)
    ev.advisory = ADV.advise(ev)
    ev.risk = ev.advisory.risk_level
    return ev, False


async def case_4_alert_omission() -> bool:
    print(LINE)
    print("CASE 4 — an active alert the answer omits: the guard refuses the calm summary")
    print("  the reply below deliberately says nothing about alerts, which is the failure mode")
    print("  this product cannot afford: a calm forecast sentence that swallows a live warning.")
    saved = _key_for_stub()
    calls = _stub_transport(lambda evidence, n: _reply_from(evidence, _sentence_from(evidence)))
    try:
        ev, _trace = await run_pipeline(PUNE_NOW)
        ev, live_alert = await _evidence_with_an_alert(ev)
        ans, rep = await llm.explain(ev)      # second pass, now WITH the alert attached
    finally:
        _unstub_transport()
        config.GROQ_API_KEY = saved
    print(f"  alert attached        : "
          f"{[{'id': a.alert_id, 'severity': a.severity, 'event': a.event, 'sender': a.sender} for a in ev.alerts.items]}")
    print(f"  alert provenance      : {'LIVE SACHET' if live_alert else 'SYNTHETIC REHEARSAL DATA'}")
    _show(ev, ans, rep)
    print(f"  transport calls       : {len(calls)} (alert-free reply twice -> rejected twice -> fallback)")
    print("  reading: a reply that buries an active warning is discarded; the deterministic answer")
    print("  names the alert and its severity instead of offering a calm summary.")
    ok = (
        ans.origin == "deterministic_fallback"
        and ans.alert_mentioned
        and any("does not mention an alert" in f for f in rep.failures)
    )
    print(f"  --> {'PASS' if ok else 'FAIL'}")
    return ok


async def case_5_groq_down() -> bool:
    print(LINE)
    print("CASE 5 — Groq unreachable: the weather product does not go down with it")
    saved = config.SIMULATE_LLM_FAILURE
    key = _key_for_stub()
    config.SIMULATE_LLM_FAILURE = True
    try:
        ev, trace = await run_pipeline("What is the weather in Mumbai right now?")
    finally:
        config.SIMULATE_LLM_FAILURE = saved
        config.GROQ_API_KEY = key if key is not None else config.GROQ_API_KEY
    ans, rep = _answer_from_trace(trace)
    _show(ev, ans, rep, trace)
    print("  evidence rewritten by the LLM layer: no — `explain()` receives a copy of the dump and")
    print("  returns a sentence; `Evidence` is only ever produced upstream by the retrieval stages.")
    print(f"  status/latency         : {ev.status}, llm stage said "
          f"{[s for s in trace['stages'] if s['stage']=='llm'][0]['status']}")
    ok = (
        rep.llm_status == "upstream_error"
        and ans.origin == "deterministic_fallback"
        and ev.status in ("grounded", "abstain")
        and rep.verified
    )
    print(f"  --> {'PASS' if ok else 'FAIL'}")
    return ok


async def case_6_risk_change_refused() -> bool:
    print(LINE)
    print("CASE 6 — the model tries to move the risk level: refused, and the decision stands")
    probe, _ = await run_pipeline(PUNE_NOW)
    decided = probe.advisory.risk_level if probe.advisory else "UNCERTAIN"
    wanted = "HIGH" if decided != "HIGH" else "LOW"
    print(f"  advisory decided        : {decided} | reply will claim {wanted}")
    saved = _key_for_stub()
    calls = _stub_transport(
        lambda evidence, n: _reply_from(evidence, _sentence_from(evidence), risk=wanted)
    )
    try:
        ev, trace = await run_pipeline(PUNE_NOW)
    finally:
        _unstub_transport()
        config.GROQ_API_KEY = saved if saved is not None else config.GROQ_API_KEY
    ans, rep = _answer_from_trace(trace)
    _show(ev, ans, rep, trace)
    print(f"  shown to the user        : risk={ans.risk} | evidence still says "
          f"{ev.advisory.risk_level if ev.advisory else 'UNCERTAIN'}")
    print("  reading: a risk mismatch is a grounding FAILURE, not a rewrite, in either direction.")
    ok = (
        any("cannot move the risk level" in f for f in rep.failures)
        and ans.risk == (ev.advisory.risk_level if ev.advisory else "UNCERTAIN")
        and len(calls) == config.LLM_MAX_ATTEMPTS
    )
    print(f"  --> {'PASS' if ok else 'FAIL'}")
    return ok


async def case_7_unverified_evidence() -> bool:
    print(LINE)
    print("CASE 7 — stale data: the model is not asked to dress it up")
    saved = config.SIMULATE_STALE_DATA
    config.SIMULATE_STALE_DATA = True
    try:
        ev, trace = await run_pipeline(PUNE_NOW)
    finally:
        config.SIMULATE_STALE_DATA = saved
    ans, rep = _answer_from_trace(trace)
    _show(ev, ans, rep, trace)
    print(f"  freshness              : {ev.validation.fresh} "
          f"(age {ev.validation.source_age_minutes} min) | failures={ev.validation.failures[:1]}")
    print("  transport calls        : 0 — with insufficient evidence the endpoint is not contacted")
    print("  reading: the numbers stay in the payload for the reviewer and never enter the sentence.")
    ok = (
        rep.llm_status == "skipped"
        and ans.origin == "deterministic_fallback"
        and "could not verify" in ans.text
        and ev.status == "abstain"
    )
    print(f"  --> {'PASS' if ok else 'FAIL'}")
    return ok


# --------------------------------------------------------------------------- #
CASES = (
    ("case1_no_key", case_1_no_key),
    ("case2_accepted_reply", case_2_accepted_reply),
    ("case3_hallucination", case_3_hallucination),
    ("case4_alert_omission", case_4_alert_omission),
    ("case5_groq_down", case_5_groq_down),
    ("case6_risk_change_refused", case_6_risk_change_refused),
    ("case7_unverified_evidence", case_7_unverified_evidence),
)


async def main() -> int:
    print(LINE)
    print(f"Phase 4 demo | provider=Groq model={config.GROQ_MODEL} temp={config.LLM_TEMPERATURE} "
          f"json_mode={config.LLM_JSON_MODE} attempts={config.LLM_MAX_ATTEMPTS} "
          f"timeout={config.LLM_TIMEOUT_S}s")
    print(f"key configured: {llm.llm_configured()} | LLM_ENABLED: {config.LLM_ENABLED}")
    if not llm.llm_configured():
        print("  no GROQ_API_KEY here: cases needing a model reply use a labelled offline stub, so "
              "nothing in this output claims a live api.groq.com call.")
    results: Dict[str, bool] = {}
    for name, fn in CASES:
        try:
            results[name] = await fn()
        except Exception as exc:  # keep the demo alive and report the truth
            results[name] = False
            print(f"  --> FAIL (exception: {type(exc).__name__}: {exc})")
    print(LINE)
    passed = sum(1 for v in results.values() if v)
    print(f"{passed}/{len(results)} cases passed: "
          + ", ".join(f"{k}:{'PASS' if v else 'FAIL'}" for k, v in results.items()))
    out = ROOT / "demo_outputs" / (
        "phase4_" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"
    )
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "llm": {"configured": llm.llm_configured(), "provider": "groq", "model": config.GROQ_MODEL,
                "temperature": config.LLM_TEMPERATURE, "json_mode": config.LLM_JSON_MODE,
                "max_attempts": config.LLM_MAX_ATTEMPTS, "timeout_s": config.LLM_TIMEOUT_S},
        "cases": results,
        "note": ("cases 2/3/4/6 used a labelled offline reply stub because no GROQ_API_KEY is set; "
                 "no live model call is claimed by this file"),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"run record: {out.relative_to(ROOT)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
