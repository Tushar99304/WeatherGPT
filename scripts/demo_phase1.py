"""
scripts/demo_phase1.py — one command that runs every Phase 1 demo scenario and saves
the raw JSON next to it, so you can (a) verify before the pitch and (b) show judges
actual evidence payloads if the live UI hiccups.

Run from the weathergpt-mvp folder:
    python scripts/demo_phase1.py            # live Open-Meteo, 5 scenarios
    python scripts/demo_phase1.py --offline   # skip network scenarios (parse+geocode logic only)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from backend.main import run_pipeline  # noqa: E402

SCENARIOS: list[tuple[str, str, list[str]]] = [
    (
        "TEST 1 - live current weather",
        "What is the weather in Nagpur right now?",
        ["grounded"],
    ),
    (
        "TEST 2 - forecast, clearly labelled",
        "Will it rain in Pune tomorrow?",
        ["grounded"],
    ),
    (
        "TEST 3 - official alert lookup (alerts wired in Phase 2)",
        "Is there any weather alert for Mumbai today?",
        ["grounded", "abstain"],
    ),
    (
        "TEST 4 - ambiguous location, never guessed",
        "What is the weather in Springfield?",
        ["clarify"],
    ),
    (
        "TEST 5 - unresolvable location, graceful abstain",
        "What is the weather in Xylophoneistan?",
        ["abstain"],
    ),
    (
        "TEST 6 - small town that the primary gazetteer misses",
        "What is the weather in Lonavala right now?",
        ["grounded", "abstain"],
    ),
]


def summarize(ev) -> dict:
    w = ev.weather
    cur = w.current if w else None
    tmrw = w.tomorrow if w else None
    return {
        "status": ev.status,
        "intent": ev.request.get("intent"),
        "timeframe": ev.request.get("timeframe"),
        "location": (
            f"{ev.location.name}, {ev.location.admin1 or '-'}, {ev.location.country or '-'} "
            f"({ev.location.latitude}, {ev.location.longitude})"
            if ev.location
            else None
        ),
        "resolution_note": ev.location.resolution_note if ev.location else None,
        "current": (
            {
                "at": cur.time,
                "temperature_c": cur.temperature_c,
                "feels_like_c": cur.apparent_temperature_c,
                "precipitation_mm": cur.precipitation_mm,
                "wind_kmh": cur.wind_speed_kmh,
                "condition": cur.condition,
            }
            if cur
            else None
        ),
        "tomorrow": (
            {
                "label": tmrw.label,
                "date": tmrw.date,
                "precip_probability_pct": tmrw.precipitation_probability_max_pct,
                "precip_sum_mm": tmrw.precipitation_sum_mm,
                "condition": tmrw.condition,
            }
            if tmrw
            else None
        ),
        "sources": [
            {"name": s.name, "type": s.type, "timestamp": s.timestamp, "authority": s.authority}
            for s in ev.sources
        ],
        "evidence_quality": ev.evidence_quality,
        "alerts": {
            "state": ev.alerts.state,
            "mode": ev.alerts.mode,
            "relevant": len(ev.alerts.items),
            "not_relevant": ev.alerts.rejected_not_relevant,
            "uncertain": ev.alerts.rejected_uncertain,
            "expired_seen": len(ev.alerts.recent_expired),
            "error": ev.alerts.error,
        },
        "abstain_reason": ev.abstain_reason,
        "clarification": ev.clarification,
    }


async def main(offline: bool) -> int:
    out_dir = pathlib.Path(__file__).resolve().parent.parent / "demo_outputs"
    out_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results = []
    failures = 0

    for title, message, expected in SCENARIOS:
        if offline and "TEST" in title and title not in ("TEST 4 - ambiguous location, never guessed",):
            print(f"\n=== {title}: SKIPPED (offline mode) ===")
            continue
        print(f"\n=== {title} ===\nQ: {message}")
        ev, trace = await run_pipeline(message)
        payload = {
            "title": title,
            "question": message,
            "expected_status": expected,
            "summary": summarize(ev),
            "evidence": json.loads(ev.model_dump_json()),
            "pipeline": trace,
        }
        results.append(payload)
        ok = ev.status in expected
        failures += 0 if ok else 1
        print(json.dumps(payload["summary"], indent=2, ensure_ascii=False))
        print(f"--> {'PASS' if ok else 'FAIL'} (status={ev.status}, expected one of {expected})")

    path = out_dir / f"phase1_{stamp}.json"
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved full evidence to {path}")
    print(f"{len(results) - failures}/{len(results)} scenarios matched expectations.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--offline" in sys.argv)))
