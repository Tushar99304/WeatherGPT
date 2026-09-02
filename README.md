# WeatherGPT — SIH26068 Minimal Working Demo

**Status: Phases 1–4 complete and tested live; Phase 5A (provider registry + model metadata) added;
U1 (disaster scenarios + official alert UX) added — see `docs/U1_REPORT.md`.
208 tests, 194 of them offline.** The whole
pitch pipeline now runs: natural-language question → intent + location + timeframe → geocoding →
live weather evidence → NDMA SACHET official alerts → validation → Evidence Quality → deterministic
risk advisory → **grounded LLM explanation** → answer + source + timestamp, or a graceful
clarification / abstention. Phase 4 added the language model *last and on a leash*: Groq only ever
sees the finished Evidence object and its reply is discarded unless a programmatic verifier agrees
with every number, source, timestamp, alert and risk word in it.

> The one-sentence version: **the LLM is a voice for the evidence, not a source of it.** Risk level,
> evidence quality and alert status are decided before a sentence is written, and the verifier — not
> the model — decides whether that sentence may be shown. If Groq is missing, slow or wrong, the
> user still gets a grounded answer from the same numbers.

> Positioning (say this in the pitch): WeatherGPT is **not** a weather prediction model.
> It is a **grounded conversational weather intelligence layer**. The LLM never becomes the
> source of meteorological truth — every number comes from retrieved, validated evidence.

**U1 (disaster scenarios + official alert UX).** An active, location-verified NDMA/SACHET alert
now dominates the entire UX: the CAP `instruction` is surfaced verbatim and attributed (advisory
factors + deterministic answer, so it needs no LLM key), the page leads with a prominent
official-alert banner before any model-weather summary, and "What WeatherGPT recommends" (the
deterministic advisory) sits directly under the answer. Disaster-oriented demo chips (heavy
rain/flood, thunderstorm/lightning, strong winds, fog, heat) exercise the **existing**
evidence/advisory pipeline — no new thresholds, no disaster-prediction model, and alerts that
are expired, relevance-uncertain or window-unproven are still never presented as active.

---

## 1. Run it

```bash
cd weathergpt-mvp
python3 -m venv .venv                    # Windows: py -3 -m venv .venv
source .venv/bin/activate                # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                     # Windows: copy .env.example .env

uvicorn backend.main:app --reload --port 8000
```

Then in a second terminal:

```bash
# TEST 1 — live current weather
curl -s localhost:8000/api/query -H 'Content-Type: application/json' \
  -d '{"message":"What is the weather in Nagpur right now?"}' | python3 -m json.tool

# TEST 2 — forecast (labelled, never called "current")
curl -s localhost:8000/api/query -H 'Content-Type: application/json' \
  -d '{"message":"Will it rain in Pune tomorrow?"}' | python3 -m json.tool

# TEST 4 — ambiguous location: ASK, never silently pick
curl -s localhost:8000/api/query -H 'Content-Type: application/json' \
  -d '{"message":"What is the weather in Springfield?"}' | python3 -m json.tool

# TEST 5 — unresolvable location: ABSTAIN, never fabricate
curl -s localhost:8000/api/query -H 'Content-Type: application/json' \
  -d '{"message":"What is the weather in Xylophoneistan?"}' | python3 -m json.tool

# TEST 3 — official alert check for a SACHET-covered district (Phase 2)
curl -s localhost:8000/api/query -H 'Content-Type: application/json' \
  -d '{"message":"Is there any weather alert for Mayurbhanj today?"}' | python3 -m json.tool

# Phase 2 component endpoint: alerts only, no weather call, fast to re-run on stage
curl -s "localhost:8000/api/alerts?place=Pune" | python3 -m json.tool
curl -s "localhost:8000/api/alerts?place=Mayurbhanj&context=Odisha" | python3 -m json.tool

# Force the third alert state for the demo (source DOWN, not "no alerts"):
SIMULATE_ALERT_FAILURE=true uvicorn backend.main:app --port 8000

# Deterministic offline rehearsal of the alert path (real recorded SACHET files, labelled replay):
ALERT_FIXTURE_RSS=refs/rss_fixture_pune.xml ALERT_FIXTURE_CAP_DIR=refs/cap_files \
  python scripts/demo_phase2.py --fixture
```

The page itself (single file, no build): served at <http://localhost:8000/> — it shows the answer
card with the grounding verdict, then the evidence it was derived from. For a raw trace view, open <http://localhost:8000/api/pipeline?message=What%20is%20the%20weather%20in%20Nagpur%20right%20now%3F>
in a browser — it returns the answer **plus the stage-by-stage trace** (parse → geocode →
retrieve → evidence), which is what the judges actually want to see.

Full scenario sweep (writes evidence JSON to `demo_outputs/`):

```bash
python scripts/demo_phase1.py     # 6 retrieval scenarios (Phase 1)
python scripts/demo_phase2.py     # 3 alert cases: none-relevant / live alert / not-attached
python scripts/demo_phase2.py --fixture   # same, from the recorded SACHET files (no network)
python scripts/demo_phase3.py             # 5 cases incl. the two forced-failure switches
python scripts/demo_phase4.py             # 7 grounding cases: accept / hallucinate / omit alert /
                                          # Groq down / risk moved / stale data / no key
```

## 2. Tests

```bash
python -m pytest tests                 # 208 tests: 194 offline logic + 14 live network
python -m pytest tests -m "not live"   # no internet needed (hotel Wi-Fi / judges' laptop)
python -m pytest tests -v -k alerts    # Phase 2 only
python -m pytest tests -v -k phase3    # Phase 3 (validation / quality / advisory), all offline
python -m pytest tests -v -k phase4    # Phase 4 (grounding checks + every LLM failure mode), offline
python -m pytest tests -v -k phase5a   # Phase 5A (provider registry + model metadata), all offline
python -m pytest tests -v -k u1        # U1 (instruction surfacing, precedence, hazard scenarios), offline
node scripts/check_frontend_render.mjs # 8 render cases for the page incl. the U1 alert UX, no backend needed
python -m pytest tests -v -k geocoding # single area
```

The Phase-2 offline tests never touch the network: they replay the real feed/CAP files saved in
`refs/` and inject a fixed `now`, so the timestamp verdicts stay reproducible after the live
records expire. Only `tests/test_phase1_live.py` and `tests/test_phase2_live.py` make calls.

## 3. API surface

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/query` | `{ "message": "...", "location_hint"?: "...", "include_pipeline"?: true }` → status + Evidence (+trace) |
| `GET` | `/api/pipeline?message=` | same pipeline, GET form (easy curl/PowerShell/browser) |
| `GET` | `/api/geocode?name=&context=` | component test for place resolution |
| `GET` | `/api/weather?latitude=&longitude=&timeframe=` | component test for raw weather retrieval |
| `GET` | `/api/alerts?place=&context=` | **Phase 2** component test: geocode + SACHET check only |
| `GET` | `/health` | provider, alert config, `llm {configured, provider, model}` (never the key) |

Response statuses: `grounded` (evidence exists → the LLM may phrase it, if it verifies) ·
`clarify` (ambiguous or missing location) · `abstain` (no verifiable evidence) · `error` (internal
failure). Every response also carries `answer` (`QueryResponse.answer`): the sentence, the credited
source + as-of timestamp, the copied risk/quality, `origin` (`groq_llm` or `deterministic_fallback`)
and the full `grounding` report — `verified`, the checks that ran, how many number claims were
checked and which were rejected, attempts, regeneration, `llm_status`, model and latency.

## 4. The Evidence object (the only thing the LLM will ever see)

```jsonc
{
  "schema_version": "weathergpt-evidence/0.1",
  "status": "grounded",
  "request":   { "message": "...", "intent": "forecast_current", "timeframe": "now", "location_text": "nagpur" },
  "location":  { "name": "Nagpur", "latitude": 21.14631, "longitude": 79.08491,
                 "admin1": "Maharashtra", "country": "India", "timezone": "Asia/Kolkata",
                 "resolution_note": "restricted to country IN (1 non-IN match ignored); same-named smaller places ignored..." },
  "weather":   { "provider": "open-meteo", "model": "best_match", "kind": "live",
                 "retrieved_at_utc": "2026-08-31T20:01:40Z",
                 "current":  { "time": "2026-09-01T01:30", "temperature_c": 25.5, "apparent_temperature_c": 28.3,
                               "precipitation_mm": 0.0, "wind_speed_kmh": 11.7, "condition": "Overcast",
                               "units": { "temperature_2m": "°C" } },
                 "today": {...}, "tomorrow": { "label": "Tomorrow", "precipitation_probability_max_pct": 100.0, ... },
                 "request_url": "https://api.open-meteo.com/v1/forecast?..." },
  "alerts": {                                       // Phase 2 — one object, not a bare list
    "source": "NDMA SACHET", "authority": "official",
    "state": "checked",                             // checked | unavailable | not_checked
    "mode": "live",                                 // live | fixture_replay | disabled | not_run
    "checked_at_utc": "2026-08-31T20:32:18Z",
    "feeds_considered": ["https://sachet.ndma.gov.in/cap_public_website/rss/rss_maharashtra.xml",
                         ".../rss_india.xml"],
    "items_in_feeds": 99, "details_fetched": 0,
    "rejected_stale": 10, "rejected_not_relevant": 0, "rejected_uncertain": 0,
    "items": [ { "alert_id": "IN-1787913209058029_29", "event": "Moderate Rain",
                 "severity": "Moderate", "urgency": "Expected", "certainty": "Likely",
                 "effective_at": "...", "expires_at": "...", "validity": "active",
                 "relevance": { "status": "relevant", "level": "L1_exact_locality",
                                "reason": "areaDesc names this place (pune)", "matched_terms": ["pune"] },
                 "raw_source_url": "https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=..." } ],
    "recent_expired": [], "notes": ["...recall limit, disclosed..."]
  },
  "alert_state": "checked",                         // derived from `alerts.state` (single source of truth)
  "sources": [ { "name": "Open-Meteo", "type": "forecast", "timestamp": "2026-09-01T01:30",
                 "authority": "research_repro", "url": "..." } ],
  "validation": { "ok": true, "sufficient": true, "fresh": true, "complete": true,
                  "values_plausible": true, "labeling_consistent": true, "alerts_valid": true,
                  "alert_integrity": true, "source_age_minutes": 12.0,
                  "checks_run": ["location_sanity", "freshness", "value_ranges", "..."],
                  "failures": [], "warnings": [] },                      // Phase 3 — populated
  "evidence_quality": "HIGH",     // HIGH | MEDIUM | LOW — Evidence Quality, NOT a forecast probability
  "quality_breakdown": { "score": 86, "label": "HIGH",
      "weights": { "authority": 40, "freshness": 30, "completeness": 20, "agreement": 10 },
      "breakdown": { "authority": 26.0, "freshness": 30.0, "completeness": 20.0, "agreement": 10.0,
                     "caps_applied": [] },
      "notes": ["authority 26/40: single research/reanalysis source. IMD is the intended ...", "..."],
      "disagreements": [], "meaning": "Evidence Quality measures how trustworthy this retrieved ..." },
  "risk": "LOW",
  "advisory": { "risk_level": "LOW", "activity": "outdoor activity/travel",
      "headline": "Weather-related travel risk is LOW based on validated model weather and an
                   official-alert check that came back empty.",
      "reason": "...", "factors": ["NDMA SACHET checked: no active official alert verifiably tied ..."],
      "rules_fired": ["R7_quiet"], "alert_ids": [], "evidence_quality": "HIGH",
      "disclaimer": "Weather-related risk estimate ... not an official order ... personal safety." }
}
```

This object is *exactly* what the model is sent — `json.dumps(evidence.model_dump())` as one user
message, no tools, no history, no second retrieval — and it is also what the verifier re-reads to
judge the reply. Nothing else is in the prompt. `sources[].authority` stays `research_repro` for
Open-Meteo and `official` for SACHET — the badge never upgrades a research source, and the answer
must credit a source that appears in this list or it is thrown away.

## 5. Environment variables

| Variable | Used from | Meaning |
| --- | --- | --- |
| `GROQ_API_KEY` | Phase 4 | LLM key. Empty ⇒ no LLM call; the deterministic grounded answer is used and the trace says `llm_status="no_key"` |
| `GROQ_MODEL` | Phase 4 | default `llama-3.3-70b-versatile` |
| `LLM_ENABLED` / `LLM_MAX_TOKENS` / `LLM_TEMPERATURE` / `LLM_JSON_MODE` / `LLM_TIMEOUT_S` / `LLM_MAX_ATTEMPTS` | Phase 4 | the whole LLM contract: kill switch, output budget, `0.0` for reproducibility, `response_format=json_object`, 30 s ceiling, first answer + exactly one regeneration |
| `SIMULATE_LLM_FAILURE` | Phase 4 | acts like api.groq.com being dead → `upstream_error` → fallback answer, endpoint stays available |
| `SIMULATE_LLM_HALLUCINATION` | Phase 4 | injects `987.6 °C / 12345 %` (numbers absent from the evidence) so the guard can be shown firing on demand |
| `WEATHER_PROVIDER` | Phase 1/5A | provider registry key. **`open-meteo`** = CURRENT/live (implemented). **`imd` / `gfs` / `wrf`** = registered **architecture-ready stubs** — discoverable in the registry and `/health`, but `fetch()` raises the standard `UpstreamError` (honest abstain/fallback), never fabricating data (see §6) |
| `OPEN_METEO_MODEL` | Phase 5A | optional single Open-Meteo NWP model (`models=` param); empty ⇒ Open-Meteo's `best_match`. Reported on `weather.model` / `/health`. NOT multi-model ensemble retrieval; archive (historical) calls ignore it |
| `OPEN_METEO_FORECAST_URL` / `OPEN_METEO_ARCHIVE_URL` / `OPEN_METEO_GEOCODING_URL` | Phase 1 | endpoints, overridable for offline/replay testing |
| `GEO_COUNTRY_BIAS` | Phase 1 | `IN` by default; empty allows any country |
| `GEO_MAX_RESULTS`, `AMBIGUITY_MIN_POP` | Phase 1 | ambiguity detection (§7) |
| `GEO_FALLBACK` | Phase 1 | `nominatim` \| `none` — for towns the primary gazetteer misses |
| `WEATHER_MAX_STALENESS_MIN` | Phase 3 | 90 by default; a stale `current` ⇒ evidence insufficient |
| `SACHET_ENABLED` | Phase 2 | `false` ⇒ `alert_state="not_checked"` (explicitly *not* "no alerts") |
| `SACHET_RSS_BASE`, `SACHET_CAP_URL`, `SACHET_USER_AGENT` | Phase 2 | feed/CAP endpoints as configured strings, never hardcoded in the service |
| `ALERT_CACHE_TTL_S` (300) | Phase 2 | in-memory TTL for feed + CAP bodies, so a demo re-run does not hammer NDMA |
| `ALERT_MAX_AGE_H` (24) | Phase 2 | recency cut-off — the India feed still serves ~1000 h-old items, so this is mandatory |
| `ALERT_DETAIL_LIMIT` (8) / `ALERT_DETAIL_CONCURRENCY` (4) | Phase 2 | how many CAP bodies one query may open, and how politely |
| `ALERT_INCLUDE_INDIA_FEED` | Phase 2 | read `rss_india.xml` as well as the state feed |
| `ALERT_FIXTURE_RSS` / `ALERT_FIXTURE_CAP_DIR` | Phase 2 | offline replay of recorded SACHET files; reported as `mode="fixture_replay"` |
| `SIMULATE_ALERT_FAILURE` | Phase 2 | forces `unavailable`, to demo the third state on command |
| `SIMULATE_WEATHER_FAILURE` / `SIMULATE_STALE_DATA` / `SIMULATE_LATENCY_MS` | Phases 3/5 | deterministic failure cases for the demo |

No key is required for Phases 1–2: Open-Meteo (weather + geocoding) and NDMA SACHET's public
CAP/RSS feed are all key-free. (`api.ndmainsafe.in` is *not* used — it does not resolve here;
SACHET's public RSS + `FetchXMLFile` CAP documents are the working path.)

## 6. Weather providers: a registry, not a rewrite (Phase 5A)

Weather retrieval goes through the same `WeatherProvider` protocol in `backend/services/weather.py`
it always did. Phase 5A moved provider **selection and metadata** into a small data-driven registry
under `backend/services/providers/`, and made the rest of the pipeline provider-agnostic (the
weather `Source` name/authority come from the registry; validation/quality no longer hardcode the
word "Open-Meteo").

```
backend/services/providers/
├── __init__.py     # exports the registry API + stub classes
├── registry.py     # ProviderInfo catalogue + create_provider() factory + /health report
└── stubs.py        # IMDStubProvider / GFSStubProvider / WRFStubProvider (architecture-ready)
```

Provider status, stated plainly (and surfaced in `GET /health` → `weather_providers`):

| Key | Label | Status in this build | Source authority |
| --- | --- | --- | --- |
| `open-meteo` | Open-Meteo | **CURRENT / live** — forecast + archive (reanalysis) | `research_repro` |
| `imd` | IMD | **ARCHITECTURE-READY stub** — API access pending approval | `official` *when live* |
| `gfs` | NOAA GFS | **ARCHITECTURE-READY stub** — direct adapter not wired* | `research_repro` |
| `wrf` | WRF | **ARCHITECTURE-READY stub** — no local grid/endpoint in build | `research_repro` |

\* GFS fields *can* already be reached **through** Open-Meteo by setting `OPEN_METEO_MODEL=gfs_seamless`
(Open-Meteo acts as a documented, key-free proxy); a direct GFS (THREDDS/OpenDAP) adapter is not wired.

`get_provider()` (still the single call site the pipeline uses) delegates to
`providers.create_provider(WEATHER_PROVIDER)`:

* **`open-meteo`** returns the working `OpenMeteoProvider` — behaviour unchanged.
* **`imd` / `gfs` / `wrf`** return stub providers. They satisfy the same interface but `fetch()`
  raises the project's standard `UpstreamError` — exactly what a live upstream outage raises — so
  the pipeline abstains honestly, the LLM is never asked to invent numbers, Evidence Quality goes
  LOW and the advisory UNCERTAIN. Nothing is presented as live data.
* an **unknown** key is a selection-time `RuntimeError` listing the registered providers.

**Model metadata (additive).** `WeatherBundle` now carries `model` (e.g. `best_match`, an explicit
`OPEN_METEO_MODEL` value, or `reanalysis_archive` for historical calls). It is shown on the weather
evidence/source; it does **not** change the grounding contract — the LLM still only sees the
Evidence object, and numbers are still verified against it.

Honest line for the panel: *"Open-Meteo is our live weather evidence provider today; IMD is our
intended authoritative national source and, with GFS and WRF, is an architecture-ready slot in a
provider registry — none of them are faked as live. This build retrieves live evidence from
Open-Meteo and official disaster alerts from NDMA SACHET; the source label on every answer states
that plainly."*

Related honesty detail: `sources[].authority = "research_repro"` for Open-Meteo (a model/reanalysis
blend, **not** station observations) and `"official"` for SACHET alerts. The badge never upgrades a
research source to an official one — a weather provider earns `official` only when a real
meteorological service is wired behind its registry key.

## 7. Four decisions worth explaining under questioning

**a) Ambiguity is judged by significance, not string equality.** GeoNames returns both
`Nagpur, Maharashtra (2.4M)` and a hamlet called `Nagpur` in Uttar Pradesh. Treating those as
"ambiguous" would make the system ask a pointless question about India's fourth-largest city.
So a same-named place competes only if it is a real settlement (population ≥ `AMBIGUITY_MIN_POP`
or an administrative seat), and the ignored names are written into `resolution_note` and shown in
the UI. Assumption disclosed ≠ assumption hidden. `Springfield` (5 real US cities) still asks.

**b) Relative days are computed on the location's clock, not the server's.** A bug we actually hit
while building: "tomorrow" resolved against UTC, and during an evening demo the system silently
queried *today*. Now `today/tomorrow/yesterday` are resolved inside the provider using the resolved
place's timezone, and only an explicit `2026-08-25` pins a calendar date. Past dates go to the
archive endpoint with `sources[].type = "historical"`; `is_forecast` is set per day so yesterday is
never presented as a forecast or as current conditions.

**c) Alert relevance is a conservative ladder, and it is allowed to say "uncertain".**
A headline that merely contains the state name is *not* evidence that it covers your city — that
is how a Nashik warning would end up on a Pune screen. So `services/alerts.py` only escalates like
this: L1 the resolved locality/district (or a verified alias, incl. Devanagari) appears as a whole
word in `areaDesc`/headline/description or its LGD district code matches → `relevant`; L2 the
record says state-wide (`all the districts of …`) and names the state → `relevant`; L3 the state is
named (or the record came from that state's feed) and the districts are *enumerated* without ours →
`not_relevant`; a vague "some places / isolated places" list we cannot read → `uncertain`;
L4 geometry (polygon/circle) only when SACHET actually supplied valid geometry — it currently
supplies none, so `geometry_available: false` is reported instead of pretending to test it.
`uncertain` is never upgraded by keyword similarity. Every judgement carries the `reason` and the
`matched_terms` that produced it, and the alert's own wording limitations ("at isolated places") are
echoed in the reason.

**d) Three alert states are three different facts.** `checked` + zero items = *we looked, nothing
applies*; `unavailable` = *the feed failed* (with `error` preserved); `not_checked` = *disabled in
this configuration*. They are separate enum members on purpose — collapsing them is how a prototype
ends up telling people there is no warning during a real one. `recent_expired` is kept as a fourth,
clearly-labelled bucket so "an alert existed and ended" stays visible without counting as active.

**e) Evidence Quality is an engineering heuristic about the EVIDENCE, and it is capped by rules
a human can read.** Weights are fixed and printed in every response: `authority 40 / freshness 30 /
completeness 20 / agreement 10` (sum 100); `HIGH ≥ 80`, `MEDIUM ≥ 55`. Open-Meteo can only ever
contribute 26/40 for authority because it is honestly labelled `research_repro`, not because we
dislike it — that number rises when a real official meteorological source is connected. Then five
caps are applied to the *label*: alert source unavailable → not HIGH; only-uncertain alert coverage
→ not HIGH; required evidence missing → LOW; stale data → LOW; unresolved location → LOW. It is
never called "confidence" and it is never the probability of rain: `quality_breakdown.meaning`
says so inside the payload, so even the LLM cannot quietly reinterpret it.

**f) Risk is decided before the LLM exists, and an alert cannot be buried.** `advisory.py` is a
list of if-rules over validated evidence, each with an id that ends up in `rules_fired`: an active
official alert verified relevant to this district forces HIGH (R1) whatever the forecast says;
hazards in the retrieved numbers raise it (R3); unverifiable alerts or failed validation push the
answer to UNCERTAIN (R5/R6) instead of an all-clear. The wording is fixed — "Weather-related travel
risk is HIGH based on …" — and one test asserts the strings "it is safe"/"unsafe to travel" never
appear. The advisory may only cite alert ids that exist in the evidence
(`validation.advisory_references_ok`), the same gate Phase 4's grounding verifier runs.

**g) The LLM answers to a verifier, and the verifier is the product.** `grounding.py` re-reads the
reply against the evidence object: every number must exist under the unit it is claimed with
(±0.1, dates/ids/score-denominators excluded), the credited source must be in `sources[]`, the
as-of timestamp must be a stamp the evidence actually carries (a forecast *day date* is not), an
active alert must be mentioned with its severity, cited alert ids must exist, the risk level and
evidence quality must be copied exactly, a day-block value may not be phrased as "right now", an
insufficient-evidence answer must say so, and no sentence may promise safety or order an evacuation
(10 numbered rules + a wording layer). A failure gets **one** regeneration carrying the exact
complaints; if it fails again, the deterministic evidence-based sentence is shown and
`grounding.verified=false` is recorded on the rejected attempt — never silently patched. Risk and
quality mismatches are failures in *both* directions: the model may not raise them, and may not
lower them either. Groq being down, slow, keyless or wrong changes the answer's `origin`, never its
right to exist.

## 8. Files

```
weathergpt-mvp/
├── backend/
│   ├── main.py                 # FastAPI app + pipeline stages + status decisions
│   ├── config.py               # every env var, defaults, physical ranges
│   ├── models.py               # Evidence schema (the single source of truth for the LLM)
│   └── services/
│       ├── http_client.py      # timeouts, retry, UpstreamError -> abstain
│       ├── geocoding.py        # place -> coordinates, ambiguity + fallback rules
│       ├── parsing.py          # rule-based intent/timeframe/location extraction
│       ├── weather.py          # WeatherProvider protocol + Open-Meteo provider + get_provider()
│       ├── providers/          # Phase 5A: provider registry + imd/gfs/wrf stubs (see §6)
│       │   ├── registry.py     #   ProviderInfo catalogue + factory + /health report
│       │   └── stubs.py        #   architecture-ready IMD/GFS/WRF providers (raise UpstreamError)
│       ├── alerts.py           # Phase 2: SACHET RSS + CAP parse, recency, relevance ladder, TTL cache
│       ├── validation.py       # Phase 3: location/freshness/values/labelling/completeness/alerts
│       ├── quality.py          # Phase 3: Evidence Quality weights + caps + per-part breakdown
│       ├── advisory.py         # Phase 3: deterministic risk rules over validated evidence only
│       ├── grounding.py        # Phase 4: the 10 grounding checks (numbers/source/ts/alerts/risk/…)
│       ├── llm.py              # Phase 4: Groq client, strict prompt, regeneration + deterministic fallback
│       └── evidence.py         # assemble the normalized Evidence object (weather + alerts)
├── refs/                       # real recorded SACHET feed/CAP samples used by the offline tests
├── scripts/demo_phase1.py      # runs all demo scenarios, saves raw JSON
├── scripts/demo_phase2.py      # 3 alert cases; discovers a live positive, else labelled replay
├── scripts/demo_phase3.py      # 5 cases: quiet / live alert / checked-none / alert outage / stale
├── scripts/demo_phase4.py      # 7 cases: accepted / hallucination / alert omitted / Groq down / …
├── scripts/check_frontend_render.mjs  # offline render test for the single-file frontend
├── frontend/index.html         # single-file demo page (served at "/"), renders the Evidence object
├── tests/                      # offline logic tests + live smoke tests
├── docs/PLAN_48H.md            # phase plan, verified endpoint recon, grounding design
├── docs/U1_REPORT.md           # U1: disaster scenarios + official alert UX (what/why/tests)
├── .env.example
└── requirements.txt
```

## 9. Common errors

| Symptom | Cause → fix |
| --- | --- |
| `ModuleNotFoundError: backend` | Not in `weathergpt-mvp/`, or ran `python backend/main.py`. Run `uvicorn backend.main:app` **from the project root**. |
| `error during connection ... 404 on /api/query` | Server not started / port taken: `uvicorn backend.main:app --port 8000`; check `curl localhost:8000/health`. |
| `UpstreamError: open-meteo: ConnectTimeout` | College Wi-Fi blocking outbound HTTPS, or Open-Meteo hiccup. Backend already returns `abstain` (correct behaviour). Re-run; verify with `curl "https://api.open-meteo.com/v1/forecast?latitude=18.52&longitude=73.85&current=temperature_2m"`. |
| `status: "clarify"` for a city you know exists | The geocoder split the name (`"pune district"` / `"near"`). Add a hint: `{"location_hint": "Maharashtra"}`, or set `GEO_COUNTRY_BIAS=` (empty). |
| `status: "abstain"` + `"couldn't verify ... as a place in India"` | `GEO_COUNTRY_BIAS=IN` rejected a foreign place. Expected; set it empty if you want the global answer. |
| Tests fail with `17 passed` locally but `no tests ran` on Windows | Missing venv activation. Re-activate, then `python -m pytest tests -m "not live"`. |
| Live tests fail but offline pass | Network/VPN. Run `-m "not live"` and use `demo_outputs/*.json` as the fallback evidence in the demo. |
| `alert_state: "unavailable"` + `error: "... 404 ..."` | The state-feed filename is not what SACHET publishes. Slugs use the **first word** of the state (`rss_uttar.xml`, `rss_tamil.xml`); the code probes and falls back to `rss_india.xml` and still answers. |
| `alert_state: "checked"`, `items: []`, but the feed clearly has alerts | Correct, not a bug: those alerts are for other districts/states, or all of them were older than `ALERT_MAX_AGE_H` (see `rejected_stale`). Check `notes[]` and `rejected_*` counts. |
| Alert demo needs an active alert that does not exist right now | Do not invent one. Use `python scripts/demo_phase2.py --fixture` (labelled `fixture_replay` from `refs/`) or `SIMULATE_ALERT_FAILURE=true` for the unavailable state. |
| `DeprecationWarning`/crash on a CAP file | SACHET sometimes serves an HTML error page where CAP XML is expected — `parse_cap` raises `ValueError` and the alert is dropped, `unavailable` is reported; never silently treated as "no alerts". |
