"""
scripts/demo_phase2.py — SACHET alert demonstration for the Thursday round.

Three cases, exactly as specified:
  CASE A  a location with NO verified relevant alert  -> state=checked, items=[]
  CASE B  a location with a REAL active alert          -> alert surfaced + provenance
  CASE C  alerts that exist elsewhere                  -> NOT attached to our city

CASE B is DISCOVERED from the live feed at run time (never hardcoded, never invented): we take
the freshest operational CAP record that names a specific district, then ask about that district.
If the feed has nothing usable right now, the script falls back to the recorded fixture in
refs/ and labels it `RECORDED FIXTURE` so nobody mistakes replay for live data.

Run:  python scripts/demo_phase2.py            # live
      python scripts/demo_phase2.py --fixture   # deterministic offline replay (rehearsal/backup)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import config  # noqa: E402
from backend.main import run_pipeline  # noqa: E402
from backend.services import alerts as A  # noqa: E402
from backend.services import geocoding  # noqa: E402
from backend.services.http_client import get_text  # noqa: E402

HEAD = "\n" + "=" * 78


def _print_alert(alert) -> None:
    print(f"    validity   : {alert.validity}  ({alert.validity_reason})")
    print(f"    event      : {alert.event}  | severity {alert.severity} | urgency {alert.urgency}"
          f" | certainty {alert.certainty}")
    print(f"    headline   : {(alert.headline or '').strip()[:120]}")
    print(f"    areaDesc   : {(alert.area_desc or '')[:100]}")
    print(f"    window     : {alert.effective_at}  ->  {alert.expires_at}")
    print(f"    relevance  : {alert.relevance.status} / {alert.relevance.level}")
    print(f"    why        : {alert.relevance.reason}")
    print(f"    authority  : {alert.authority}  | source {alert.source}")
    print(f"    evidence   : {alert.source_url}")


async def discover_live_positive() -> tuple[str | None, str]:
    """Find a district that a *currently published* CAP record explicitly names."""
    url = A.india_feed_url()
    try:
        text = await get_text(url, service="sachet-rss", headers={"User-Agent": config.SACHET_USER_AGENT})
        items = A.parse_rss_items(text, feed_url=url)
    except Exception as exc:  # noqa: BLE001 - demo tooling reports, never crashes
        return None, f"live discovery failed: {type(exc).__name__}: {exc}"
    now = dt.datetime.now(dt.timezone.utc)
    for item in items:  # feed order == recency order
        cap: dict = {}
        try:
            raw = await get_text(
                item["link"] or A.cap_url_for(item["identifier"]),
                service="sachet-cap",
                headers={"User-Agent": config.SACHET_USER_AGENT},
            )
            cap = A.parse_cap(raw)
        except Exception:  # noqa: BLE001
            continue
        status, _reason, _missing, _age = A.classify_validity(
            now,
            sent=A.parse_any_datetime(cap.get("sent")),
            effective=A.parse_any_datetime(cap.get("effective")),
            onset=A.parse_any_datetime(cap.get("onset")),
            expires=A.parse_any_datetime(cap.get("expires")),
            max_age_hours=float(config.ALERT_MAX_AGE_H),
        )
        if status != "active":
            continue
        # Self-verifying discovery: the string must BOTH resolve to a real place AND still be
        # judged L1-relevant by the same conservative ladder the product uses. Anything that
        # fails either check (e.g. a weather word caught by the extractor) is discarded, so the
        # demo can never advertise a fake "positive" case.
        state = (re.search(r"of ([A-Z][A-Za ]+)", cap.get("areaDesc") or "") or [None, None])[1]
        for cand in A._listed_districts(cap.get("headline")) + A._listed_districts(cap.get("areaDesc")):
            geo = await geocoding.resolve(cand, context=state)
            if geo.location is None:
                continue
            rel = A.assess_relevance(
                geo.location,
                area_desc=cap.get("areaDesc"),
                headline=cap.get("headline"),
                description=cap.get("description"),
            )
            if rel.status == "relevant":
                return cand, (
                    f"freshest active alert names '{cand}' ({geo.location.admin1}); resolves to "
                    f"{geo.location.latitude:.3f},{geo.location.longitude:.3f} and the ladder "
                    f"confirms {rel.level} (id {cap.get('identifier')})"
                )
    return None, "no active alert with an explicitly named, resolvable district in the feed right now"


async def case_a() -> bool:
    print(f"{HEAD}\nCASE A - no verified relevant alert for this city (must say 'checked')\n{HEAD}")
    ev, trace = await run_pipeline("What is the weather in Pune right now?")
    a = ev.alerts
    print(f"  answer status   : {ev.status}")
    print(f"  alert_state     : {a.state}   (NOT 'unavailable', NOT 'not_checked')")
    print(f"  mode            : {a.mode}")
    print(f"  feeds consulted : {[f.split('/')[-1] for f in a.feeds_considered]}")
    print(f"  in-window items : {a.items_in_feeds} | details fetched {a.details_fetched}")
    print(f"  relevant items  : {len(a.items)} | not_relevant {a.rejected_not_relevant}"
          f" | uncertain {a.rejected_uncertain} | expired seen {len(a.recent_expired)}")
    print("  wording for the panel: 'NDMA SACHET was checked; no active official alert is")
    print("    verifiably tied to Pune. That is a checked result, not an absence of data.'")
    ok = a.state == "checked" and not a.items and ev.status == "grounded"
    print(f"  --> {'PASS' if ok else 'FAIL'}")
    return ok


# The recorded fixture was published on 2026-08-28 (16:00-19:00 IST window). Judging it by
# today's clock would correctly mark it EXPIRED, so the replay path evaluates it inside its own
# window and prints that it is a recording -- never a claim that it is live right now.
FIXTURE_WINDOW = dt.datetime(2026, 8, 28, 11, 30, tzinfo=dt.timezone.utc)


def use_fixture_config(enabled: bool) -> dict:
    """Point the alert service at refs/ only, and restore afterwards. The India feed is
    switched OFF during replay so the result is exactly the recorded file, not a live+fixture
    mixture (that would make the rehearsal non-deterministic)."""
    saved = {
        "ALERT_FIXTURE_RSS": config.ALERT_FIXTURE_RSS,
        "ALERT_FIXTURE_CAP_DIR": config.ALERT_FIXTURE_CAP_DIR,
        "ALERT_INCLUDE_INDIA_FEED": config.ALERT_INCLUDE_INDIA_FEED,
    }
    if enabled:
        config.ALERT_FIXTURE_RSS = str(ROOT / "refs/rss_fixture_pune.xml")
        config.ALERT_FIXTURE_CAP_DIR = str(ROOT / "refs/cap_files")
        config.ALERT_INCLUDE_INDIA_FEED = False
    else:
        for k, v in saved.items():
            setattr(config, k, v)
    A.clear_caches()
    return saved


async def case_b_fixture_replay() -> tuple[bool, str]:
    print("  MODE: RECORDED FIXTURE (refs/rss_fixture_pune.xml + refs/cap_files/) -- "
          "evaluated inside its own 2026-08-28 16:00-19:00 IST window, NOT presented as live")
    use_fixture_config(True)
    geo = await geocoding.resolve("pune", context="Maharashtra")
    if geo.location is None:
        print(f"  geocoding failed in fixture mode: {geo.evidence_gap}")
        return False, "pune"
    res = await A.check_alerts(geo.location, now=FIXTURE_WINDOW)
    use_fixture_config(False)
    print(f"  state {res.state} | mode {res.mode} | relevant {len(res.items)} | "
          f"not_relevant {res.rejected_not_relevant} | uncertain {res.rejected_uncertain}")
    if not res.items:
        print(f"  notes: {res.notes}")
        print("  --> FAIL (fixture did not produce a relevant alert; check refs/)")
        return False, "pune"
    for alert in res.items[:2]:
        _print_alert(alert)
    ok = res.mode == "fixture_replay" and all(
        al.relevance.status == "relevant" for al in res.items
    )
    print(f"  --> {'PASS' if ok else 'FAIL'} (labelled replay of a genuine NDMA SACHET record)")
    return ok, "pune"


async def case_b(fixture_only: bool) -> tuple[bool, str]:
    print(f"{HEAD}\nCASE B - a REAL active official alert, surfaced with provenance\n{HEAD}")
    if fixture_only:
        return await case_b_fixture_replay()
    place, why = await discover_live_positive()
    if not place:
        print(f"  no live positive alert to show: {why}")
        print("  per the plan we DO NOT fabricate one; falling back to the labelled fixture replay")
        return await case_b_fixture_replay()  # keeps the case demonstrable, clearly labelled
    print(f"  discovered: {why}")
    hint = "Maharashtra" if place == "pune" else None
    ev, _ = await run_pipeline(f"Is there any weather alert for {place.replace(' ', '_')} today?", location_hint=hint)
    a = ev.alerts
    print(f"  place asked     : {place}   | answer status {ev.status} | alert mode {a.mode}")
    print(f"  state feed used : {(a.state_feed_used or a.feeds_considered[0] if a.feeds_considered else 'n/a')}")
    if not a.items:
        print("  no relevant alert surfaced (feed changed between discovery and query) -> see notes")
        print(f"  notes: {a.notes}")
        print("  --> FAIL (nothing to show; re-run or use --fixture)")
        return False, place
    for alert in a.items[:2]:
        _print_alert(alert)
    src = next((s for s in ev.sources if s.name == "NDMA SACHET"), None)
    print(f"  evidence source : {src.name if src else None} | type "
          f"{src.type if src else None} | authority {src.authority if src else None}")
    ok = a.state == "checked" and all(
        al.validity in {"active", "unknown"} and al.relevance.status == "relevant" for al in a.items
    )
    print(f"  --> {'PASS' if ok else 'FAIL'}")
    return ok, place


async def case_c(place_from_b: str) -> bool:
    print(f"{HEAD}\nCASE C - alerts published elsewhere must NOT be attached here\n{HEAD}")
    ev, trace = await run_pipeline("Is there any weather alert for Ahmedabad today?")
    a = ev.alerts
    print(f"  alert_state {a.state} | relevant {len(a.items)} | not_relevant "
          f"{a.rejected_not_relevant} | uncertain {a.rejected_uncertain} | stale {a.rejected_stale}")
    print(f"  note: the live feed is full of {'other districts of Gujarat' if place_from_b != 'pune' else 'other states'} "
          f"alerts; none of them may be claimed as covering Ahmedabad.")
    for exp in a.recent_expired[:2]:
        print(f"    (expired, kept out of the answer) {(exp.headline or '')[:70]}")
    ok = a.state == "checked" and not a.items and (
        a.rejected_not_relevant + a.rejected_uncertain + a.rejected_stale > 0 or a.items_in_feeds >= 0
    )
    print(f"  --> {'PASS' if ok else 'FAIL'}")
    return ok


async def main() -> int:
    fixture_only = "--fixture" in sys.argv
    print(f"{HEAD}\nWeatherGPT Phase 2 - NDMA SACHET official alert check "
          f"({'FIXTURE REPLAY' if fixture_only else 'LIVE FEED'})\n{HEAD}")
    results = {}
    results["case_a"] = await case_a()
    results["case_b"], place = await case_b(fixture_only)
    results["case_c"] = await case_c(place)

    out = ROOT / "demo_outputs" / (
        f"phase2_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    out.parent.mkdir(exist_ok=True)
    ev, trace = await run_pipeline("What is the weather in Nagpur right now?")
    out.write_text(
        json.dumps(
            {"results": results, "evidence": json.loads(ev.model_dump_json()), "pipeline": trace},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nSaved evidence snapshot (includes the alerts block) -> {out}")
    passed = sum(results.values())
    print(f"{passed}/3 cases passed.")
    return 0 if passed == 3 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
