# WeatherGPT — 48-hour build plan (Tue → Thu), Phase 1 done

Reference docs honoured: *Minimal Working Demo* = scope; *Technical Explanation & Judge Q&A* =
architecture. Out of scope until after Thursday: multilingual, voice, GIS/map, push
notifications, WRF, Kubernetes/Docker, full IMD integration, large RAG.

---

## A. Final MVP architecture (one path, no shortcuts on grounding)

```
USER QUERY (text)
  ↓ 1 parse          intent + location phrase + timeframe        services/parsing.py     [P1 ✅]
  ↓ 2 resolve        name → coordinates, ambiguity → ask         services/geocoding.py   [P1 ✅]
  ↓ 3 retrieve       live weather (current/forecast/historical)  services/weather.py     [P1 ✅]
  ↓ 4 alerts         NDMA SACHET CAP/RSS, official only          services/alerts.py      [P2]
  ↓ 5 validate       7 checks → sufficient / insufficient        services/validation.py  [P3]
  ↓ 6 quality        HIGH / MEDIUM / LOW (evidence pipeline)     services/evidence.py    [P3]
  ↓ 7 ground         LLM phrases ONLY the evidence object        services/llm.py         [P4]
  ↓ 8 verify         numbers in answer must exist in evidence    services/grounding.py   [P4]
  ↓ 9 UI             answer + source + timestamp + badge + alert frontend/               [P4b]
  └─ any failure at 2/3/5 → status = clarify | abstain  (never a guess)
```

Invariant to repeat in the pitch: **the LLM has no weather tools, no retrieval, no web access —
its input is one JSON evidence object, its output is re-checked against that object before the UI
shows it.**

## B. Folder structure (final)

```
weathergpt/
├── backend/
│   ├── main.py                  # app, /api/query, pipeline stages, status decisions
│   ├── config.py                # env vars, defaults, physical plausibility ranges
│   ├── models.py                # Evidence + DTOs (single source of truth for the LLM)
│   ├── services/
│   │   ├── http_client.py       # timeout/retry, UpstreamError  [P1 ✅]
│   │   ├── parsing.py           # rule router: intent/timeframe/location  [P1 ✅]
│   │   ├── geocoding.py         # Open-Meteo geocoder + Nominatim fallback  [P1 ✅]
│   │   ├── weather.py           # WeatherProvider protocol + Open-Meteo (IMD slot)  [P1 ✅]
│   │   ├── evidence.py          # assemble Evidence + evidence_quality  [P1 ✅ / quality P3]
│   │   ├── alerts.py            # SACHET CAP parse + location relevance  [P2]
│   │   ├── validation.py        # 7 checks  [P3]
│   │   ├── advisory.py          # deterministic LOW/MED/HIGH/UNCERTAIN risk  [P3]
│   │   ├── llm.py               # Groq call, strict prompt, JSON schema  [P4]
│   │   └── grounding.py         # post-hoc verification + one regeneration  [P4]
│   └── (no routes/ package: one router file is enough at this size)
├── frontend/index.html · style.css · app.js   [P4b]
├── scripts/demo_phase1.py       # scenario sweep + saved evidence JSON  [P1 ✅]
├── tests/test_phase1_units.py   # 15 offline  [P1 ✅]
├── tests/test_phase1_live.py    # 3 live  [P1 ✅]
├── docs/PLAN_48H.md             # this file
├── demo_outputs/*.json          # recorded evidence (backup if Wi-Fi dies)
├── .env.example · requirements.txt · README.md
```

Dropped on purpose vs the suggested layout: `routes/` (one file), `storage`/SQLite (nothing to
persist for one question), `feedparser` (stdlib `xml.etree` handles CAP).

## C. APIs — and whether a key is needed (all verified live on Tue 2026-09-01)

| API | Endpoint | Key? | Verified result |
| --- | --- | --- | --- |
| Open-Meteo forecast | `GET https://api.open-meteo.com/v1/forecast` | **No** | 200, `current` at 15-min cadence, `daily.precipitation_probability_max` present |
| Open-Meteo geocoding | `GET https://geocoding-api.open-meteo.com/v1/search` | **No** | 200, `Pune → 18.51957, 73.85535, Maharashtra, IN` |
| Open-Meteo archive | `GET https://archive-api.open-meteo.com/v1/archive` | **No** | 200 for `2026-08-30/31` — powers `historical_climate` |
| Open-Meteo climate | `GET https://climate-api.open-meteo.com/v1/climate` | **No** | 200 with **`daily=`** only (`monthly=precipitation_sum` → 400) |
| NDMA SACHET India feed | `GET https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml` | **No** | 200, ~99 items, `category=Met`, `pubDate` in GMT |
| NDMA SACHET state feed | `GET .../rss/rss_maharashtra.xml` | **No** | 200, 10 items — pattern `rss_<statename>.xml` confirmed; district feeds 404 |
| SACHET single alert CAP | `GET .../FetchXMLFile?identifier=<guid>` | **No** | 200, full CAP 1.2: `severity/urgency/certainty/effective/expires/areaDesc` + LGD district codes |
| SACHET polygon geometry | `GET .../FetchPolygonXMLFile?identifier=<id>` | — | **403 from our sandbox** → do not build on it |
| `api.ndmainsafe.in/cell-alarm` | (documented elsewhere) | ? | **DNS does not resolve here → treat as unverified, optional only** |
| Groq chat completions | `POST https://api.groq.com/openai/v1/chat/completions` | **Yes** — free key at <https://console.groq.com/keys> | endpoint live (401 `invalid_api_key` without a key); key is the only credential the MVP needs |
| IMD API | intended primary Indian source | **pending approval** | not integrated; provider interface already reserves the slot |

Docs: <https://open-meteo.com/en/docs> · geocoding <https://open-meteo.com/en/docs/geocoding-api> ·
historical <https://open-meteo.com/en/docs/historical-weather-api> · SACHET portal
<https://sachet.ndma.gov.in/> → *CapFeed* page · Groq <https://console.groq.com/docs> ·
Nominatim policy <https://usage-policy.nominatim.openstreetmap.org/>

## D. Environment variables

See README §5 — `cp .env.example .env`, fill only `GROQ_API_KEY` (Phase 4). Everything else has a
working default; the demo runs with **zero** keys through Phase 3.

## E. Phase order for the remaining ~40 hours

| # | When | Deliverable | Owner | Acceptance test |
| --- | --- | ---| --- |
| **P1 ✅ done** | Tue eve | FastAPI + geocoding + live weather + Evidence object + abstain/clarify | backend | `python -m pytest tests` → 18 passed; `python scripts/demo_phase1.py` → 6/6 scenarios |
| **P2** ✅ done Tue night | `services/alerts.py`: SACHET RSS (state feed first, India feed second) → CAP detail fetch → filter to ≤`ALERT_MAX_AGE_H` and unexpired → relevance match → `alerts[]`, `alert_state="checked"`; 300 s cache; alert source authority = `official` | backend | "Is there any weather alert for Mumbai?" returns either a real alert with severity/headline/link **or** an explicit "checked, none active" (never a silent blank) |
| **P3** ✅ done Wed 03:00 | Wed morning–noon | `validation.py` (7 checks incl. ranges + `WEATHER_MAX_STALENESS_MIN`) and transparent `evidence_quality` scoring (authority 40 / freshness 30 / completeness 20 / agreement 10) + `advisory.py` deterministic risk; abstain when insufficient | backend+LLM | `SIMULATE_STALE_DATA=true` → quality drops to MEDIUM/LOW and answer says data may be stale; `SIMULATE_WEATHER_FAILURE=true` → no numbers in the reply |
| **P4** | Wed afternoon | `llm.py` (Groq, JSON-only, temp 0) + `grounding.py` (numeric presence check vs evidence, one regeneration, then safe fallback) | LLM | unit test: a deliberately bad LLM reply containing `34.7°C` when evidence says `25.5` must be **rejected** and replaced by the fallback |
| **P4b** | Wed evening | `frontend/index.html|css|js`: bubbles + source line + `Updated:` + badge + alert/risk block + "which one did you mean?" buttons from `candidates[]` | frontend | all 6 scenarios clickable, no console errors |
| **P5** | Wed night | Rehearsal + failure polish + record `demo.mp4` (screen recording) as Wi-Fi insurance; freeze code | pitch lead | run the 6-scenario script twice, <90 s total |
| **P6** | Thu morning | `GET /api/evidence/{id}` replay of saved `demo_outputs/*.json` if the internet dies mid-demo (flagged `RECORDED EVIDENCE`, never presented as live) | backend | offline demo with Wi-Fi off still shows the flow honestly |

Deliberately **not** scheduled (postpone, mention as roadmap): mBERT+LoRA serving (the rule router
already covers the 5 intents — the classifier becomes the Wed-afternoon stretch goal), embedding
RAG, LangGraph state machines, vector DB, multilingual, voice, maps, IMD live integration, Docker,
realtime SSE streaming, fine-tuning, monitoring.

## F0. Validation, Evidence Quality and advisory — as implemented (Phase 3)

`validation.py` → `quality.py` → `advisory.py`, run in that order after the Evidence object is
built, each emitting its own trace stage (`validate`, `quality`, `advise`).

* Validation is six small checks — location sanity, freshness (`weather.minutes_since_source` vs
  `WEATHER_MAX_STALENESS_MIN`), value ranges (`config.RANGES`), current-vs-forecast labelling
  (including: a missing "tomorrow" block is a FAILURE, never a silent fallback to today),
  per-intent completeness, and alert availability/integrity. It EXTENDS the Validation object
  Phase 2 filled; `None` means "not judgeable", which is kept distinct from `False`.
* Evidence Quality = authority 40 + freshness 30 + completeness 20 + agreement 10; HIGH ≥ 80,
  MEDIUM ≥ 55. Authority 40 requires official evidence for the question asked, so a weather-only
  answer tops out at 26/40 while Open-Meteo is the provider (it is `research_repro`, never
  relabelled). Agreement is only measured when ≥2 comparable sources exist — with one source it is
  reported neutral with a note, never invented, and disagreement is surfaced rather than averaged.
  Five caps act on the label: alert outage → ≤MEDIUM; uncertain-only alert coverage → ≤MEDIUM;
  required evidence missing → LOW; stale → LOW; unresolved location → LOW.
* Advisory: R1 active+verified Severe/Extreme alert → HIGH (nothing downgrades it), R2 other active
  alert → MEDIUM, R3 hazards from validated numbers → MEDIUM/HIGH, R4 unconfirmable alert coverage
  → UNCERTAIN (or MEDIUM when a hazard already exists), R5 alerts unverifiable → never LOW,
  R6 insufficient evidence → UNCERTAIN, R7 quiet → LOW. Thresholds live in `THRESHOLDS` with a
  rationale each and are labelled engineering heuristics, not IMD criteria; severity from SACHET is
  used verbatim instead.
* One deliberate deviation from the earlier plan: an SACHET outage does NOT invalidate a weather
  answer (that would abstain on every question during an alert outage). It caps quality at MEDIUM,
  makes the advisory refuse an all-clear, and becomes a hard validation failure only when the
  question was actually about alerts.

## F. Grounding design (write it down before coding it)

Evidence → LLM: **only** the `Evidence` JSON (`model_dump()`), no chat history, no tools,
`temperature=0`, `max_tokens≈400`, `response_format={"type":"json_object"}`.

System prompt (final wording, matches the reference doc):

> You are WeatherGPT's explanation layer. You are NOT a weather forecasting engine. Use ONLY the
> values present in the supplied evidence object. Do not invent, estimate, infer, calculate, or
> modify weather numbers. Do not introduce weather facts absent from the evidence. If
> `evidence_quality` is LOW or `validation.sufficient` is false, state that reliable weather
> information could not be verified. If an alert exists, lead with it and never downplay it. Distinguish
> current conditions from forecasts (`weather.current` vs `weather.tomorrow`, `is_forecast`). For
> safety/travel questions describe weather-related risk and evidence; never guarantee personal
> safety. Preserve `sources[].name` and `sources[].timestamp` verbatim. Return exactly:
> `{"answer": str, "risk": "LOW|MEDIUM|HIGH|UNCERTAIN|null", "source": str, "timestamp": str, "evidence_quality": "HIGH|MEDIUM|LOW"}`

Verification (`grounding.py`) — deterministic, no LLM judging the LLM:
1. parse JSON; required keys present, else regenerate once with a stricter suffix, else fallback.
2. every number in `answer` must appear in the evidence value set (`±0.1` rounding tolerance),
   including any number spelled in words (a cheap word→digit map) — this catches the classic
   "it will feel like 40°C" hallucination;
3. `source` string must equal a `sources[].name`; `timestamp` must equal that source's timestamp;
4. if `alerts` non-empty, `answer` must contain an alert cue (one of the headline words) — an
   alert must never be dropped for a calm forecast;
5. failure ⇒ return the template fallback built directly from evidence, and mark
   `grounding.verified=false` in the UI so we can show the judges the guard actually fired.

## G. SACHET matching — implemented (Phase 2), stricter than planned

Feed items carry **no coordinates**. Verified CAP structure of a `FetchXMLFile` record:
`identifier`, `sender`, `status`, `msgType`, `scope`, `category`, `event`, `urgency`, `severity`,
`certainty`, `sent`, `effective`, `onset`, `expires`, `headline`, `area.areaDesc`
(e.g. *"Pune,Satara districts of Maharashtra"*), optional `instruction`, `description` (**usually
empty** — so an empty description is never a reason to drop a match), 0–N `geocode` LGD district
codes, and a `parameter` *"Polygon URL"* whose endpoint returns **403** here. `sent/effective/
expires` are IST-offset (`+05:30`); RSS `pubDate` is GMT — all are converted to UTC and kept as
aware datetimes internally, rendered with `Z`.

Ladder as actually implemented in `backend/services/alerts.py`:
1. **Scope**: `rss_<state>.xml` from `location.admin1` **plus** `rss_india.xml` (both, deduped by
   CAP identifier). Slug = lowercase first word of the state — because that is literally what the
   portal publishes (`rss_uttar.xml`, `rss_tamil.xml`, `rss_maharashtra.xml`), with 4 special cases
   (NCT of Delhi→`delhi`, J&K→`jammu`, Andaman & Nicobar→`andaman`, DNH&Daman & Diu→`dadra`).
   Unknown/404 slug ⇒ fall back to the India feed and still answer.
2. **Recency** (`classify_validity`): `now >= expires` ⇒ **expired**; `expires` missing ⇒
   **unknown** + `expiry_missing` (never "active indefinitely"); `now < effective/onset` ⇒ unknown;
   feed item older than `ALERT_MAX_AGE_H` or `sent` in the future ⇒ unknown/expired with reason.
   Draft/Test messages are skipped.
3. **Relevance** (`assess_relevance`), L1→L4, first hit wins, everything explainable:
   * **L1** whole-word locality match of the resolved place (name + `admin2` + verified aliases incl.
     Devanagari) in `areaDesc` ∪ headline ∪ description across *all* language blocks, or a matching
     LGD code ⇒ `relevant`.
   * **L2** explicit state-wide marker (`all the districts of …`, `whole of …`) naming our state ⇒
     `relevant`.
   * **L3** state named (or the record came from that state's feed) and its districts are enumerated
     without ours ⇒ `not_relevant`; an unreadable list ("7 districts of …") or vague wording ⇒ `uncertain`.
   * **L4** polygon/circle geometry, **only** when valid geometry was actually supplied in the record;
     otherwise `geometry_available: false` is reported.
4. **Never** upgrade `uncertain` from keyword similarity, and the state word alone is *not* L1:
    the plan originally said "state-wide ⇒ applies to all its districts". Rejected after live
    testing — an Odisha feed item for Bhadrak attached itself to Mayurbhanj. Being wrong about an
    active warning in a disaster app costs more than missing one, so ambiguous cases now land in
    `uncertain`, which the UI must render as "an official alert exists for this state; we cannot
    confirm it covers your area".
5. **Alert states** (`AlertsEvidence.state`): `checked` / `unavailable` / `not_checked`, with `mode`
   (`live` / `fixture_replay` / `disabled` / `not_run`) kept separate so recorded replay can never be
   mistaken for live data. Counters (`items_in_feeds`, `details_fetched`, `rejected_stale`,
   `rejected_not_relevant`, `rejected_uncertain`, `recent_expired`, `notes[]`) make every "no alerts"
   answer explainable, including the disclosed recall limit: only keyword-eligible feed items get
   their CAP body opened, so an alert whose only mention of your district is inside a translated body
   can still be missed.

Safety rule kept: **never invent or paraphrase an alert.** We quote `headline`, `severity`,
`expires`, `source_url` from the CAP record verbatim; `authority="official"` is set on the alert
source, and the alert block renders above the forecast text.

## H. Demo script (Thu), 90 seconds

1. `What is the weather in Nagpur right now?` → live numbers, `Open-Meteo · Updated 2026-09-01T01:30 (local)`, badge.
2. `Will it rain in Pune tomorrow?` → "Forecast for Tomorrow", probability + amount; point out it is **not** labelled current.
3. `Is there any weather alert for Mumbai today?` → alert priority (or honest "checked, none relevant").
   3a. `curl "localhost:8000/api/alerts?place=Mayurbhanj&context=Odisha"` → a live active alert with
       severity + window + `L1` reason + CAP link, or the honest empty case.
   3b. `curl "localhost:8000/api/alerts?place=Pune"` → shows *checked with zero relevant items* while
       the feed is full of other districts' alerts (the false-positive guard, visible on stage).
   3c. `SIMULATE_ALERT_FAILURE=true` → `unavailable`, phrased as "alerts could not be verified", never
       as "no alerts". Backup if the network dies: `python scripts/demo_phase2.py --fixture`.
4. `What is the weather in Springfield?` → clarification with the 3 candidate places; **no** weather retrieved.
5. `What is the weather in Xylophoneistan?` → abstain, no numbers anywhere in the payload (`demo_outputs/phase1_*.json` proves it).
6. Kill it live: `SIMULATE_WEATHER_FAILURE=true uvicorn ...` → same graceful abstain.
7. Close on the invariant + `GET /health` showing `authority: research_repro`, the SACHET block
   (`enabled`, feeds base, `max_age_h`) and the IMD sentence.
