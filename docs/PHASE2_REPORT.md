# Phase 2 report — NDMA SACHET official alert retrieval

WeatherGPT · SIH26068 · written 2026-08-31 20:44 UTC · branch state: Phases 1–2 complete, 53 tests passing.

---

## 1. Files changed

| File | Change | Why |
| --- | --- | --- |
| `backend/services/alerts.py` | **new**, 1071 lines, 36 public/private functions | the whole Phase-2 engine: RSS parse → candidate selection → CAP detail fetch → recency → relevance ladder → normalized alerts, with an in-memory TTL cache |
| `backend/models.py` | modified | `AlertValidity` / `RelevanceStatus` / `RelevanceLevel` enums, `AlertRelevance`, `AlertsEvidence`, extended `Alert` (alert_id, authority, CAP fields, validity, relevance, provenance, `raw_fields`); **`Evidence.alerts` is now an `AlertsEvidence` object** and `Evidence.alert_state` became a `@computed_field` derived from it |
| `backend/services/evidence.py` | modified | `build_evidence(parsed, geo, weather, alerts=None)`; `_attach_alerts()` adds the `official_alert` source (authority `official`), `validation.alerts_valid`, `checks_run` entries and warnings |
| `backend/main.py` | modified | weather + alerts run concurrently (`asyncio.gather`); new `retrieve_alerts` trace stage; `alert_intent_blocker` in `quality_breakdown`; new component endpoint `GET /api/alerts?place=&context=`; `/health` reports the alert config; version → `0.2.0-phase2` |
| `backend/config.py` | modified | `SACHET_ENABLED`, `SACHET_RSS_BASE`, `SACHET_CAP_URL`, `SACHET_USER_AGENT`, `ALERT_CACHE_TTL_S=300`, `ALERT_MAX_AGE_H=24`, `ALERT_DETAIL_LIMIT=8`, `ALERT_DETAIL_CONCURRENCY=4`, `ALERT_INCLUDE_INDIA_FEED`, `ALERT_FIXTURE_RSS`, `ALERT_FIXTURE_CAP_DIR`, `SIMULATE_ALERT_FAILURE` |
| `tests/test_phase2_alerts.py` | **new**, 32 tests | deterministic offline coverage from `refs/` + synthetic CAP docs, injected `now`, zero network |
| `tests/test_phase2_live.py` | **new**, 3 tests | real SACHET calls, invariant-based (never "expect exactly N alerts") |
| `tests/test_phase1_live.py` | **1 line** | the pipeline-stage assertion now expects `retrieve_alerts` in the list (order + presence still asserted) |
| `scripts/demo_phase2.py` | **new**, 253 lines | Cases A/B/C; discovers a live positive at run time; `--fixture` for offline rehearsal |
| `scripts/demo_phase1.py` | modified | demo output now also records the alert block per scenario |
| `refs/*` | **new** recorded sources | `sachet_rss_sample.xml` (99 real items), `cap_sample.xml`, `cap_sample_marathi_pune.xml` (en-IN + Marathi), `rss_fixture_pune.xml`, `cap_files/*.xml`, `polygon_sample.txt` (the 403) |
| `.env.example`, `README.md` (§1–§9), `docs/PLAN_48H.md` (§E, §G, §H) | modified | new switches, the implemented ladder, demo script for the alerts case |

Unchanged on purpose: `http_client.py` (reused, no second client), `geocoding.py`, `parsing.py`, `weather.py`, all Phase-1 logic, and the weather half of `Evidence`.

## 2. What was implemented

* **Retrieval, not guessing.** State feed `rss_<slug>.xml` (slug = lowercase first word of `admin1`, 4 verified special cases, probe + fallback) **plus** `rss_india.xml`, deduped by CAP identifier, both read per query; no single hardcoded alert and no dependence on one state.
* **CAP parsing with stdlib only** (`xml.etree`, no `feedparser`): every required field, all `info` language blocks merged (English preferred for display text, Marathi/Hindi preserved verbatim in `raw_fields`), `geocode` LGD codes, `parameter` Polygon URL, `areaDesc` de-duplicated. Missing optional fields, empty `description`, HTML-error-body-instead-of-XML and malformed documents are all handled — the service degrades to a counted skip or `unavailable`, never a crash and never a silent "no alerts".
* **Explicit timezone-aware recency** (`classify_validity`): IST-offset CAP times and GMT `pubDate` strings all become aware UTC datetimes; `now ≥ expires` → **expired**; `expires` missing → **unknown** + `expiry_missing` (never "active indefinitely"); `now < effective/onset` → unknown; `sent` older than `ALERT_MAX_AGE_H` → stale/expired; future-dated `sent` → unknown with a reason. Draft/Test messages are dropped. Helpers are pure functions so tests pin exact verdicts.
* **Conservative relevance ladder** (`assess_relevance`), first hit wins, every verdict carries `level`, `reason`, `matched_terms`:
  L1 whole-word locality match (resolved name + `admin2` + verified aliases incl. Devanagari, incl. `[^a-z\u0900-\u097F]` boundaries) in `areaDesc` ∪ headline ∪ description, or a matching LGD code → `relevant`;
  L2 explicit state-wide marker naming our state → `relevant`;
  L3 state named/`from_state_feed` + enumerated districts excluding ours → `not_relevant`; unreadable enumeration or "isolated places" → `uncertain`;
  L4 polygon/circle only when valid geometry is actually in the record (it never is today → `geometry_available: false` reported, polygon-URL-endpoint-403 documented). `uncertain` is **never** upgraded by similarity. Alert wording limits ("at isolated places") are echoed into the reason instead of hidden.
* **Three distinct states, never collapsed:** `checked` / `unavailable` (with `error` preserved) / `not_checked`, plus `mode` = `live` / `fixture_replay` / `disabled` / `not_run`. Expired-but-recent alerts go to `recent_expired`, a fourth labelled bucket.
* **Merged into Evidence without redesigning it:** `Evidence.alerts` object with counters (`items_in_feeds`, `details_fetched`, `rejected_stale/not_relevant/uncertain/duplicate`, `duration_ms`, `notes[]`), one `Source(type="official_alert", authority="official", name="NDMA SACHET")`, and a `checked, none relevant` warning that explicitly says this is **not** proof that no alert exists. `Evidence.alert_state` stays available for the Phase-1 trace/UI contract.
* **Politeness & reuse:** existing `http_client.get_text` (no second client, no Redis), shared in-memory TTL cache for feeds and CAP bodies, `ALERT_DETAIL_LIMIT=8` bodies per query at concurrency 4, content-level dedupe so the same alert on two feeds is shown once.
* **Offline replay path** (`ALERT_FIXTURE_RSS` / `ALERT_FIXTURE_CAP_DIR`) for rehearsal, clearly labelled `fixture_replay`.

## 3. Exact test command

```bash
cd weathergpt-mvp
python -m pytest tests                 # everything (needs internet for the 6 live tests)
python -m pytest tests -m "not live"   # offline only — this is what to re-run on the demo laptop
python -m pytest tests/test_phase2_alerts.py -v
python scripts/demo_phase2.py          # alert cases A/B/C, live
python scripts/demo_phase2.py --fixture  # same, from recorded SACHET files
```

## 4. Exact result

```
$ python -m pytest tests
53 passed in 9.57s

$ python -m pytest tests -m "not live"
47 passed, 6 deselected in 0.25s

$ python -m pyflakes backend/ tests/ scripts/
(clean)

$ python scripts/demo_phase1.py        # Phase-1 regression
6/6 scenarios matched expectations.

$ python scripts/demo_phase2.py
3/3 cases passed.          (CASE A checked-none-relevant · CASE B live active alert · CASE C not attached)
$ python scripts/demo_phase2.py --fixture
3/3 cases passed.          (CASE B = labelled replay of the recorded 2026-08-28 Pune/Satara record)
```

Composition: 15 Phase-1 offline + 32 Phase-2 offline + 3 Phase-1 live + 3 Phase-2 live.

## 5. Live SACHET check (actually run, 2026-08-31 20:44 UTC)

| Place | Result |
| --- | --- |
| **Pune** (Maharashtra) | `state=checked`, feeds `rss_maharashtra.xml` + `rss_india.xml`, 99 items in the 24 h window, **0 relevant**, 10 rejected as stale (the Maharashtra feed's newest item was ~33 h old), 0 detail fetches needed, ~2.0 s |
| **Mayurbhanj** (Odisha) | `state=checked`, 109 in-window, 8 details fetched → **1 active relevant alert**: *"Moderate Rain, Thunderstorm and Lightning is very likely to occur at many places over Mayurbhanj in next 3 hours"*, severity `Severe`, `L1_exact_locality`, LGD 365, window `2026-08-31T19:41Z → 22:41Z`, 2 items `not_relevant`, 5 `recent_expired`, ~2.3 s |
| **Ahmedabad** (Gujarat) | `state=checked`, 6 details, **0 relevant**, 1 `not_relevant` (the Dadra & Nagar Haveli/Navsari record — the false positive this phase explicitly kills), 4 stale |
| Forced failure (`SIMULATE_ALERT_FAILURE=true`) | `state=unavailable`, `error="SACHET feed unavailable (SIMULATE_ALERT_FAILURE=true)"`, `validation.alerts_valid=false`, `checks_run=[alerts_unavailable, …]`, `quality_breakdown.alert_intent_blocker` set — distinguishable from "no alerts" in every payload |
| `api.ndmainsafe.in` | still does not resolve from here — **not used**, not claimed anywhere |

## 6. Limitations discovered (all disclosed in code/payload, none papered over)

1. **Recall limit.** Only keyword-eligible feed items get their CAP body opened (`ALERT_DETAIL_LIMIT`), so an alert whose only mention of your district sits inside a translated body, or uses a spelling outside the 8-entry alias table, can be missed. Stated in `notes[]` verbatim ("recall limit, disclosed"). Widening it is a latency decision, not a bug fix.
2. **No geometry.** 0/20 sampled CAP records carry `polygon`/`circle`; the `FetchPolygonXMLFile` endpoint returns **403** here. L4 exists but is dormant code, and `geometry_available:false` is what we report.
3. **`description` is usually empty** in SACHET records, so matching leans on `headline` + `areaDesc`; an empty description is never treated as absence of relevance.
4. **Nowcast windows are ~3 h** and the India feed also serves ~1000 h-old items, so `ALERT_MAX_AGE_H` is mandatory. Consequence: for metros the honest answer is often "checked, none currently relevant" (Pune today) — the UI must phrase that carefully (Phase 5).
5. **State-word matching is deliberately excluded from L1** — after live testing this attached an Odisha/Bhadrak alert to Mayurbhanj and a Dadra/Navsari alert to Ahmedabad. Ambiguous cases now land in `uncertain`, so the demo sometimes shows "an official alert exists for this state, we cannot confirm your area" instead of a match. That is the intended trade.
6. **Feed slugs are the portal's own convention** (first word: `rss_uttar.xml`, `rss_tamil.xml`). Renaming by NDMA would silently weaken L3's `from_state_feed` signal even though the India-feed fallback keeps answering.
7. **No LGD gazetteer join** — district↔code mapping is not consulted, only compared when the record supplies codes. A full LGD table is post-MVP.
8. **No translation**: Marathi/Hindi blocks are preserved, not machine-translated; the English block is preferred for display text.
9. **No persistence**: the cache is in-process (restart = cold), and there is no alert history/replay store yet.
10. **`uncertain`/`not_relevant` are rendered identically by nothing yet** — the user-facing wording for those two states is Phase 5 UI work; Phase 2 only guarantees they are distinguishable in the payload.

## 7. Example normalized alert object — captured live, not authored

A real `active` record from the Odisha feed for Mayurbhanj, as the backend emits it:

```json
{
  "alert_id": "IN-1788205361926012_50",
  "source": "NDMA SACHET",
  "authority": "official",
  "sender": "IMD-Bhubaneswar",
  "author_name": "IMD Bhubaneswar",
  "event": "Moderate Rain",
  "headline": "Moderate Rain,Thunderstorm and Lightning  is very likely to occur at many places over Mayurbhanj in next 3 hours.",
  "description": null,
  "instruction": "Please follow SDMA guidelines.",
  "severity": "Severe",
  "urgency": "Expected",
  "certainty": "Likely",
  "category": "Met",
  "area_desc": "Mayurbhanj district of Odisha",
  "cap_status": "Actual",
  "msg_type": "Alert",
  "language": "en-IN",
  "lgd_district_codes": [
    "365"
  ],
  "sent_at": "2026-08-31T19:42:10Z",
  "effective_at": "2026-08-31T19:41:00Z",
  "onset_at": "2026-08-31T19:42:41Z",
  "expires_at": "2026-08-31T22:41:00Z",
  "validity": "active",
  "validity_reason": "within the effective-to-expiry window",
  "expiry_missing": false,
  "age_minutes": 61.3,
  "source_url": "https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=1788205361926012",
  "raw_source_url": "https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=1788205361926012",
  "feed_url": "https://sachet.ndma.gov.in/cap_public_website/rss/rss_odisha.xml",
  "match_reason": "areaDesc names this place (mayurbhanj); alert wording is limited ('at many places')",
  "relevance": {
    "status": "relevant",
    "level": "L1_exact_locality",
    "reason": "areaDesc names this place (mayurbhanj); alert wording is limited ('at many places')",
    "matched_terms": [
      "mayurbhanj"
    ],
    "area_text": "Mayurbhanj district of Odisha",
    "geometry_available": false
  },
  "raw_fields": {
    "polygon": null,
    "circle": null,
    "polygon_url": "https://sachet.ndma.gov.in/cap_public_website/FetchPolygonXMLFile?identifier=1788205361926012",
    "scope": "Public",
    "references": null,
    "info_count": 1,
    "info_languages": [
      "en-IN"
    ],
    "headlines_by_lang": {
      "en-IN": "Moderate Rain,Thunderstorm and Lightning  is very likely to occur at many places over Mayurbhanj in next 3 hours."
    }
  }
}
```

`relevance.status/level/reason/matched_terms` make the attachment decision auditable, and `raw_source_url` is the CAP document the claim came from.

## 8. Example `/api/query` response (weather + alert block, abridged)

`POST /api/query {"message":"Is there any weather alert for Mayurbhanj today?","include_pipeline":true}` — full file: `demo_outputs/phase2_api_query_example.json`.

```json
{
  "status": "grounded",
  "evidence": {
    "schema_version": "weathergpt-evidence/0.1",
    "status": "grounded",
    "request": {
      "message": "Is there any weather alert for Mayurbhanj today?",
      "intent": "official_alert",
      "intent_reason": "matched alert/warning keywords",
      "timeframe": "today",
      "timeframe_reason": "matched today-keywords",
      "target_date": null,
      "location_text": "Mayurbhanj"
    },
    "location": {
      "name": "Baripāda",
      "latitude": 21.93458,
      "longitude": 86.72852,
      "admin1": "Odisha",
      "admin2": "Mayurbhanj",
      "country": "India",
      "timezone": "Asia/Kolkata"
    },
    "weather": {
      "provider": "open-meteo",
      "kind": "live",
      "retrieved_at_utc": "2026-08-31T20:43:29Z",
      "current": {
        "time": "2026-09-01T02:00",
        "temperature_c": 25.8,
        "precipitation_mm": 1.4,
        "condition": "Thunderstorm with slight hail"
      }
    },
    "alert_state": "checked",
    "alerts": {
      "source": "NDMA SACHET",
      "authority": "official",
      "state": "checked",
      "mode": "live",
      "checked_at_utc": "2026-08-31T20:43:29.084900Z",
      "state_feed_used": "https://sachet.ndma.gov.in/cap_public_website/rss/rss_odisha.xml",
      "items_in_feeds": 109,
      "details_fetched": 8,
      "rejected_stale": 0,
      "rejected_not_relevant": 2,
      "rejected_uncertain": 0,
      "rejected_duplicate": 0,
      "duration_ms": 6
    },
    "alerts_items": 1,
    "alerts_recent_expired": 5,
    "alerts_notes": [
      "103 feed item(s) in window; 8 CAP detail record(s) fetched; 95 item(s) skipped as not naming this place/state (recall limit, disclosed - see alerts._pick_candidates)",
      "5 candidate alert(s) had already EXPIRED at check time (not 'no alert existed', and not active) - e.g. \"Moderate Rain,Thunderstorm and Lightning  is very likely to occur at a\""
    ],
    "sources": [
      {
        "name": "NDMA SACHET",
        "type": "official_alert",
        "timestamp": "2026-08-31T20:43:29.084900Z",
        "period": null,
        "url": "https://sachet.ndma.gov.in/cap_public_website/rss/rss_odisha.xml",
        "authority": "official",
        "note": "1 alert(s) verified relevant; 2 explicitly unrelated, 0 unconfirmable, 5 expired; feeds=2"
      },
      {
        "name": "Open-Meteo Geocoding",
        "type": "geocoding",
        "timestamp": "2026-08-31T20:43:29Z",
        "period": null,
        "url": null,
        "authority": "research_repro",
        "note": "exact name match, single distinct place"
      },
      {
        "name": "Open-Meteo",
        "type": "forecast",
        "timestamp": "2026-09-01T02:00",
        "period": "2026-08-31..2026-09-02",
        "url": "https://api.open-meteo.com/v1/forecast?latitude=21.9346&longitude=86.7285&current=temperature_2m%2Capparent_temperature%2Crelative_humidity_2m%2Cprecipitation%2Cweather_code%2Ccloud_cover%2Cwind_speed_10m%2Cwind_direction_10m%2Cpressure_msl&daily=weather_code%2Ctemperature_2m_max%2Ctemperature_2m_min%2Cprecipitation_sum%2Cprecipitation_probability_max%2Cwind_speed_10m_max&past_days=1&forecast_days=2&timezone=Asia%2FKolkata&wind_speed_unit=kmh",
        "authority": "research_repro",
        "note": "IMD is the intended primary Indian source; API access pending approval. This build uses Open-Meteo (model reanalysis blend) as the live provider."
      }
    ],
    "validation": {
      "ok": false,
      "sufficient": false,
      "fresh": null,
      "complete": null,
      "location_resolved": true,
      "timestamp_present": true,
      "values_plausible": null,
      "alerts_valid": true,
      "checks_run": [
        "alerts_consulted",
        "safety_critical_alert_present",
        "phase1_presence_checks_only"
      ],
      "failures": [],
      "warnings": [
        "validation and evidence_quality scoring are Phase 3 (not run in Phase 1)"
      ]
    },
    "evidence_quality": null,
    "risk": null
  },
  "pipeline_stages": [
    [
      "parse",
      "ok"
    ],
    [
      "geocode",
      "ok"
    ],
    [
      "retrieve_weather",
      "ok"
    ],
    [
      "retrieve_alerts",
      "checked"
    ],
    [
      "evidence",
      "ok"
    ]
  ]
}
```

Note the pipeline is still `parse → geocode → retrieve_weather → retrieve_alerts → evidence`, weather retrieval untouched, `evidence_quality`/`risk` still `null` (Phase 3), and no LLM call exists yet — the payload **is** the answer surface for Thursday's demo until Phase 4.

## 9. Is Phase 1 intact?

**Yes.** All 15 Phase-1 offline tests and the 3 live ones pass unchanged, `demo_phase1.py` is still 6/6, the abstain/clarify wording is byte-identical, and `/api/query` returns the same statuses (`grounded` / `clarify` / `abstain` / `error`) with the weather block produced by the same code path. Two deliberate deltas, both required by the wiring:

* `Evidence.alerts` changed from a list to the `AlertsEvidence` object (the plan called for counters and three states, which a bare list cannot carry). `alert_state` survives as a computed field, so anything reading the old key still works.
* one Phase-1 live test now expects `retrieve_alerts` inside the stage list. The assertion still checks order and presence, nothing was weakened or deleted.

No alert, LLM, grounding, quality-scoring, advisory or IMD behaviour from Phase 1 was replaced or duplicated.

## 10. Phase 3 (next): validation + Evidence Quality + advisory

1. `backend/services/validation.py` — a handful of if-checks over the **combined** evidence: coordinate sanity, `retrieved_at_utc` vs `WEATHER_MAX_STALENESS_MIN`, values inside `config.RANGES`, forecast-vs-current labelling, `alerts` availability (`checked` ≠ `unavailable`), and "alert referenced in the answer must exist in `alerts.items`".
2. `evidence.quality` scoring (name stays **Evidence Quality**): start from the weather block, subtract for staleness/gaps/failed checks, **cap at MEDIUM when `alerts.state == "unavailable"`** and cap at MEDIUM when the only alert signals are `uncertain`; keep `quality_breakdown` as the per-rule audit trail (already carries `alert_intent_blocker` from Phase 2).
3. Advisory layer: deterministic rules over evidence only (e.g. `severity in (Severe, Extreme)` + active + L1/L2 → travel risk `HIGH`; `uncertain` alert + heavy rain → `MEDIUM`/`UNCERTAIN`), phrased "Weather-related travel risk is HIGH based on …", never a safety guarantee.
4. Wire both into `/api/query` + the trace (`validate`, `quality`, `advise` stages), then extend the demo with the "forced stale data ⇒ LOW ⇒ abstain-ish answer" case (`SIMULATE_STALE_DATA=true`).
5. Reuse, do not rebuild: the alert counters in `AlertsEvidence` are the input for rules 1–2; if a rule needs data the pipeline does not have, it stays unimplemented.
