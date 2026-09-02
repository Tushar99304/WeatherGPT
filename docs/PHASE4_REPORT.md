# Phase 4 report — the grounded LLM explanation layer

Date: 2026-08-31/09-01 IST · Scope: Phase 4 of `docs/PLAN_48H.md` only (no Phase 5 work, no rebuild
of Phases 1–3, no frontend redesign beyond the answer card Phase 4 needs).

**The one invariant this phase is built on:** the LLM is given exactly `evidence.model_dump()` and
nothing else — no tools, no conversation history, no second retrieval, no follow-up call — and what
it says is discarded unless a programmatic verifier agrees with every number, source, timestamp,
alert reference, risk word and quality label in it. The model phrases the decision; it never makes
one, and it never removes one.

---

## 1. Files changed

| File | Change |
| --- | --- |
| `backend/services/grounding.py` | **new (678 lines)** — the verifier: evidence value sets, unit-aware claim extraction, the 10 grounding checks, `alert_mentioned()`, `verify()` |
| `backend/services/llm.py` | **new (531 lines)** — strict system prompt, `build_messages()`, Groq transport, `parse_json_object()`, `deterministic_payload()` (the fallback answer), `explain()` (attempt → verify → one regeneration → fallback) |
| `backend/services/http_client.py` | `post_json()` added to the one shared client (POST + JSON, bearer-friendly, `retries=0`, never echoes the request body on error, no sleep after the final attempt) |
| `backend/config.py` | LLM block: `LLM_ENABLED`, `LLM_CHAT_COMPLETIONS_PATH`, `LLM_TIMEOUT_S=30`, `LLM_MAX_TOKENS=500`, `LLM_TEMPERATURE=0.0`, `LLM_JSON_MODE`, `LLM_MAX_ATTEMPTS=2`; demo switches `SIMULATE_LLM_FAILURE`, `SIMULATE_LLM_HALLUCINATION` |
| `backend/models.py` | `GroundingReport`, `GroundedAnswer`, `QueryResponse.answer` (the pre-Phase-4 placeholder comment was replaced by the field) |
| `backend/main.py` | two new stages `llm` + `grounding` appended after `advise`; `trace["answer"]`; `/api/query` returns `answer`; `/health.llm = {configured, provider, model}`; header docstring updated |
| `frontend/index.html` | answer card (text + credited source/as-of + origin/grounding/llm pills + numbers-checked + rejection reasons), honest note in the Validation card, "raw Evidence JSON (exactly what the LLM is given)" |
| `tests/test_phase4_units.py` | **new, 56 offline tests** |
| `tests/test_phase4_live.py` | **new, 4 live tests** (the real-round-trip one skips itself with no key) |
| `tests/test_phase1_live.py`, `tests/test_phase3_live.py`, `tests/test_phase3_units.py` | stage-list assertions **extended** to the 10-stage pipeline (see §6 — no assertion was deleted or loosened) |
| `scripts/demo_phase4.py` | **new** — 7 judge-facing cases, writes `demo_outputs/phase4_*.json` |
| `scripts/check_frontend_render.mjs` | **new** — offline render test of the page (7 payloads incl. the three answer origins) |
| `.env.example`, `README.md` | mirrored the new variables and rewrote the status/test/API/architecture sections |

## 2. Architecture

```
parse → geocode → retrieve_weather ∥ retrieve_alerts → evidence
      → validate → quality → advise            (Phases 1–3: all decisions are made here)
      → llm        (Phase 4: Groq sees the finished evidence, returns JSON)
      → grounding  (Phase 4: verify() re-reads the reply against that same evidence)
      → QueryResponse{ status, evidence, pipeline, answer{ text, source, timestamp,
                        risk, evidence_quality, alert_mentioned, origin, grounding } }
```

Ordering is the design. `advisory` fixes `risk_level` and `quality` fixes `evidence_quality` **before
any sentence exists**, so the only way for the answer to be wrong about them is to be caught.
`explain()` is called with `ev` after the integrity gate; it receives a dump (a copy), returns a
value object, and `verify()` is a pure function of `(evidence, payload)` — side-effect free, safe to
call twice, and also run on our own fallback so a bug there cannot hide behind "it came from our code".

**Statuses.** `llm` stage: `ok` (model answer verified) · `skipped` (not consulted: no key, disabled,
or evidence not trustworthy) · `fallback` (called, reply replaced by the deterministic answer) ·
`failed` (reserved for the case where even the fallback fails to verify — a bug in this build, made
loud on purpose). `grounding` stage: `ok` / `failed` plus the numbers: `checks_run`,
`numbers_checked`, `numbers_rejected`, `failures`, `alert_mentioned`.

## 3. Groq integration (exact contract)

* Endpoint `POST https://api.groq.com/openai/v1/chat/completions` (`GROQ_BASE_URL` +
  `LLM_CHAT_COMPLETIONS_PATH`, both configurable), header `Authorization: Bearer ${GROQ_API_KEY}`.
* Body: `model=llama-3.3-70b-versatile`, `temperature=0.0`, `max_tokens=500`, `stream=false`,
  `response_format={"type":"json_object"}` when `LLM_JSON_MODE` — the reply is therefore JSON and
  needs no prose parsing (though `parse_json_object()` still tolerates code fences and a leading
  sentence, because models do that anyway).
* `messages` is always **exactly two entries**: the system prompt plus one user message containing
  `json.dumps(ev.model_dump(mode="json", exclude_none=False))`. On the regeneration the user turn is
  *replaced* (same evidence + the verifier's complaints) — the model's previous answer is never sent
  back, so there is no history to inherit. `tools`/`functions` are not present at all (asserted in
  `test_36`).
* Timeout `LLM_TIMEOUT_S=30`, `retries=0` at the transport, and one attempt only on error: a dead
  Groq costs 30 s once, never 60 s (asserted in `test_34`).
* `GROQ_API_KEY` is the only secret in the project. It is never in the prompt, never printed, never
  returned by `/health` (`test_04`, `test_49`), and `post_json` deliberately does not echo the request
  body when the upstream errors.
* **This environment has no key** (`GROQ_API_KEY` empty, no `.env` file), so the live round trip is
  *not* claimed anywhere: `test_live_groq_round_trip_only_when_a_key_exists` skips, and the demo
  labels every model reply as `OFFLINE STUB`. The code path is identical with a key exported.

The prompt is the PLAN §F wording **extended**, not replaced: 12 hard rules covering
every-number-must-exist, no tools, do not change risk/quality, must mention an active alert, only
cite alert ids that exist, `unavailable` ≠ "no alerts", missing → "not available", day-block values
must be attributed to their day, quote the as-of time, admit unverified evidence, never promise
safety or order evacuation, ≤60 words, JSON only.

## 4. The grounding checks

`verify()` runs all of them and reports every failure at once (a judge sees the whole picture, not
the first stumble).

1. **Required shape** — `answer`, `source`, `risk`, `evidence_quality`, and `timestamp` *when the
   evidence has a stamp to copy*; `source` likewise only when it has sources. An abstention with no
   geocode is allowed to omit them; inventing them is not.
2. **Every number is in the evidence** — claims are extracted unit-aware (`°C`, `%`/percent, `mm`,
   `km/h`, `m/s`, `hPa`, `in`, …) and checked against the value set of *that category* (±0.1, so
   "25.9 °C" passes against 25.8 while "26.4" fails). `25.8 °C` on evidence with 25.8 → pass;
   `31.4 °C` → `answer states 31.4 c, but no such temperature value exists in the evidence (known:
   …)`. `100 %` on a day block with 100 → pass; `80 %` → fail. A right number under a wrong unit
   (`25.8 mm` where 25.8 is a temperature) also fails (`test_12`).
   Deliberate non-claims are scrubbed first, not guessed at: ISO dates/datetimes, `HH:MM`, `UTC±05:30`
   offsets, `IN-…`/5+-digit ids, `86/100` score denominators, and unit-less integers ≤60 (durations,
   day numbers, "next 3 hours"). 25.8-with-a-unit is never exempted.
3. **Source identity** — the credited string is split on `+ , & / and plus "as well as"`; every part
   must be a `sources[].name` (or URL) of this evidence. `"Open-Meteo + NDMA SACHET"` passes,
   `"IMD"` fails with *"Naming a source we did not consult is a grounding failure."*
4. **Timestamp is an "as of", not a forecast day** — allowed set = `sources[].timestamp` ∪
   `weather.retrieved_at_utc` ∪ `weather.current.time` ∪ `alerts.checked_at_utc`. A `today/tomorrow/
   target_day` *date* is explicitly rejected with `…is the day the forecast COVERS, not the 'as of'
   time of the data (allowed: …)`, and with no timestamps available, an answer that states one fails.
5. **Alerts may not be swallowed** — when active/relevance-verified items exist the answer must
   mention them (cue words or the event name), may not claim "no alerts", and must carry the words
   `Severe`/`Extreme` when the alert does: *"an active warning must never be swallowed by a calm
   forecast summary"*, *"severity may not be softened"*.
6. **Alert ids must exist** — ids in the answer are checked with `validation.alert_ids_present()`
   (the Phase-2/3 function; no duplicated logic). A calm `state="checked"` result may not be
   shortened to "no alert exists", and `state="unavailable"` must be admitted.
7. **Risk is copied** — case-insensitive match against `advisory.risk_level`; any difference in
   either direction is a failure, never a rewrite: *"The explanation layer cannot move the risk
   level."* With no advisory decision (abstain/clarify) only `UNCERTAIN`/empty is accepted.
8. **Evidence Quality is copied** — same rule for `evidence_quality`; a mismatch reports the score
   too: *"the score computed HIGH (score 86/100). The label is not the model's to choose."*
   **A risk/quality mismatch is a grounding failure; the underlying fields are never overwritten.**
9. **Current vs forecast wording** — judged clause by clause (`; . ! ? but while however`), because a
   correct answer mixes both. A clause carrying a value that exists *only* in a day block
   (`is_forecast=true`) and no day cue (`tomorrow`, `forecast`, `will`, `today`, an explicit ISO date)
   fails as *"states 100 % as if it were a current observation"*. Present-tense wording with no
   current block fails too.
10. **Unverified evidence must be admitted, and no safety theatre** — with
    `validation.sufficient=false` the answer must contain an admission ("could not be verified",
    "won't guess", …) and may not use confident words (`definitely`, `guaranteed`, …). Independently:
    `it is safe to travel`, `you are safe`, `do not travel`, `evacuate immediately` etc. are
    rejected unless the phrase is literally quoted from the alert's own instruction, and quoted
    spans are ignored by the wording scans (a sentence may quote "no alert exists" precisely to
    reject it — the fallback does). `verified` answers *faithfulness to the evidence*; admissibility
    stays with `validation.sufficient`, reported as a note so the two never get conflated.

## 5. Fallback behaviour

`explain()` never raises. Every exit is a grounded sentence:

| Trigger | `llm_status` | answer | `llm` stage |
| --- | --- | --- | --- |
| reply verified | `ok` | Groq's JSON, origin `groq_llm` | `ok` |
| no `GROQ_API_KEY` | `no_key` | deterministic | `skipped` |
| `LLM_ENABLED=false` | `disabled` | deterministic | `skipped` |
| `validation.sufficient=false` | `skipped` | deterministic **abstention** (model not called at all) | `skipped` |
| HTTP error / timeout / `SIMULATE_LLM_FAILURE` | `upstream_error` | deterministic | `fallback` |
| reply is not a JSON object (after one stricter retry) | `malformed_json` | deterministic | `fallback` |
| reply fails any check twice | `grounding_failed` | deterministic | `fallback` |
| the deterministic answer itself fails to verify | — | deterministic | `failed` (bug-flag, unreachable in the suite) |

`deterministic_payload()` is assembled from evidence values only: the active alert (event, severity,
area, expiry, headline) if any, else the honest alert-state line ("could not be verified … so whether
any alert is active is unknown", or "no active official alert was verifiably tied to this location at
<checked-at>"), then the measurement sentence for the block the question was about (via
`validation.answered_day`, the same selector Phase 3 used), then `advisory.headline` + the score,
then the credited source and as-of stamp. Stamps in prose are minute-precision with the clock named
(`2026-08-31T22:00Z`, `2026-09-01T03:30 local`). **The fallback is put through the same 10 checks**
(`test_44`, parametrised over five evidence shapes including alert-present, alert-unavailable,
forecast-day and historical).

Availability consequence: with Groq dead, `/api/query` still answers in ~5 ms instead of failing —
`evidence`, `advisory`, `validation` and `alerts` are untouched (`test_46` determinism,
`test_live_evidence_is_untouched_by_the_llm_layer`).

## 6. What was deliberately not done

No rebuilding or redesigning: Phases 1–3 kept their files, functions and behaviour; `grounding.py`
reuses `validation.alert_ids_present`/`answered_day` rather than re-implementing them. The 97
pre-existing tests were not removed or weakened. **Only three assertions were changed, all of them
the pipeline stage list, which Phase 4 necessarily extends:** `tests/test_phase1_live.py` (now the
exact 10-stage list, plus new answer-contract assertions added on top), `tests/test_phase3_live.py`
(`[-3:] == [validate, quality, advise]` → `[-5:] == [validate, quality, advise, llm, grounding]`,
still an exact ordered slice), and the parametrised stage test in `tests/test_phase3_units.py`
(same extension). No other pre-existing assertion was touched, and all 97 still pass.

## 7. Tests added — exact results

```
python -m pytest tests                          → 156 passed, 1 skipped, 1 warning in 21.13s
python -m pytest tests -m "not live"            → 143 passed, 14 deselected in 0.61s
python -m pytest tests/test_phase1_units.py … test_phase3_live.py (the previous 97)
                                                → 97 passed in 16.73s
python -m pyflakes backend/ tests/ scripts/     → clean
node scripts/check_frontend_render.mjs          → ALL 7 RENDER CASES OK
```
New: `tests/test_phase4_units.py` (56 tests, no network — the Groq call is always stubbed) and
`tests/test_phase4_live.py` (4 live tests; the one that needs a key skips with a message instead of
pretending). Coverage highlights, in the numbering the brief asked for:
`test_06` 25.8 °C accepted · `test_07` 31.4 °C rejected and named in `numbers_rejected` ·
`test_08` 100 % accepted / 80 % rejected · `test_18/40` alert present but unmentioned → rejected, and
the fallback mentions it · `test_23/27/39` risk change refused in both directions, evidence unchanged ·
`test_25` quality label refused · `test_16` forecast day date rejected as an as-of stamp ·
`test_21/22` unavailable ≠ none, checked-empty may not be shortened · `test_29` insufficient evidence
must be admitted · `test_30` no safety guarantees/evacuation orders · `test_32–43` every failure mode
of the transport, the regeneration protocol (`len(calls) == LLM_MAX_ATTEMPTS`, one stricter retry, the
complaints carried, the previous reply *not* carried), the exact request body, `test_42` the model is
not consulted for unverified evidence, `test_44–46` the fallback is held to the same bar, and
`test_47–51` the pipeline/`/api/query`/`/health`/transport wiring.

## 8. Demo results (`python scripts/demo_phase4.py`, live data)

```
7/7 cases passed: case1_no_key:PASS, case2_accepted_reply:PASS, case3_hallucination:PASS,
case4_alert_omission:PASS, case5_groq_down:PASS, case6_risk_change_refused:PASS,
case7_unverified_evidence:PASS      (record: demo_outputs/phase4_20260831T220036Z.json)
```
* **Case 1** — no key, live Pune evidence → `llm: skipped (no_key)`, `grounding: ok`, answer is the
  deterministic one, `verified=True`, 5 number claims checked.
* **Case 2** — accepted reply (labelled `OFFLINE STUB`) → `origin=groq_llm`, one user turn,
  `user turn == evidence dump: True`, request body `{model, temperature 0.0, max_tokens 500,
  response_format json_object}` printed with the key excluded.
* **Case 3** — `SIMULATE_LLM_HALLUCINATION=true` → 2 transport calls, then
  `- [rejected model reply] answer states 987.6 c, but no such temperature value exists in the evidence (known: 22.3, 22.5, 22.7, …)`
  and `…states 12345 %, but no such probability value exists… (known: 63, 88, 95, 100)`;
  `numbers_rejected=['987.6 c','12345 %']`; the user sees the fallback, which contains neither.
* **Case 4** — a `Severe Heavy Rain` alert attached (no live alert on the Maharashtra feed at that
  minute, so the case says `SYNTHETIC REHEARSAL DATA` in every line) → the alert-free reply is
  rejected twice: *"the answer does not mention an alert at all — an active warning must never be
  swallowed by a calm forecast summary"* + *"severity Severe but the answer does not carry that
  word"*; the fallback leads with *"An official Severe Heavy Rain alert is active for Pune district
  of Maharashtra until 2026-09-01T01:00Z."*
* **Case 5** — `SIMULATE_LLM_FAILURE=true` → `llm_status=upstream_error`, `llm` stage `fallback`,
  evidence `verified=True`, `grounding: ok`, Mumbai answer still delivered in ~2 ms.
* **Case 6** — reply claims `HIGH` while the advisory decided `LOW` → rejected with *"The explanation
  layer cannot move the risk level"*; the shown risk is `LOW` and the evidence still says `LOW`.
* **Case 7** — `SIMULATE_STALE_DATA=true` → `validation.sufficient=false` → `llm_status=skipped`,
  **0 transport calls**, answer: *"I could not verify reliable weather information for this place and
  time, so I will not guess. Reason: provider timestamp is 361 min old, over the 90 min limit
  (WEATHER_MAX_STALENESS_MIN). Source: …, as of 2026-08-31T22:00Z."*, `status=abstain`.

Regression sweep, same session, same code: `demo_phase1.py` → `6/6 scenarios matched expectations.`,
`demo_phase2.py` → `3/3 cases passed.`, `demo_phase3.py` →
`5/5 cases passed: case1:PASS, case2:PASS, case3:PASS, case4:PASS, case5:PASS`.

## 9. Judge-defensible explanation (say this in the pitch)

> "Between the model and the user there is a verifier. It re-reads the reply against the same
> evidence object and rejects it if a single number is not in there, if it credits a source we never
> called, if it calls tomorrow's forecast 'right now', if it fails to mention an active warning, or if
> it tries to change the risk level or the quality label. A rejection costs one regeneration, and if
> that fails too the user gets our own sentence, built from the same evidence and held to the same ten
> checks. So the honest claim is not 'the LLM is careful' — it is 'an ungrounded sentence cannot reach
> the user'."

**Q: What happens if the model hallucinates?** It cannot reach the user. Its numbers are matched
against the evidence (±0.1, per unit category), its source against `sources[]`, its timestamp against
the stamps the evidence carries, its alert ids against the retrieved alerts, and its risk/quality
words against the deterministic objects. On failure the exact complaints are fed back for **one**
regeneration; if it fails again the deterministic evidence-based sentence is shown, the rejected
attempt stays in the trace with `verified=false` and the offending tokens listed, and the evidence
itself is never modified in either direction. Demo case 3 shows it firing on a real payload.

**Q: What if Groq is down, slow, or unconfigured?** The same fallback runs: the weather product stays
available, `llm` stage says `skipped`/`fallback` with `reason=no_key|disabled|upstream_error|
malformed_json|grounding_failed`, and `/health` shows whether a key is configured so nobody has to
guess. There is no request-retry pile-up (one call, 30 s ceiling, `retries=0`), and no unverified
evidence is ever dressed up — with `validation.sufficient=false` the model is not contacted at all.
Demo cases 1, 5 and 7 show the three shapes of that.

## 10. Limitations (ours, stated plainly)

1. **No live model call in this environment.** There is no `GROQ_API_KEY` here (and no `.env` file), so
   the accept path is demonstrated through the real `explain()` code with a stubbed transport, clearly
   labelled in the demo and the tests. `tests/test_phase4_live.py::test_live_groq_round_trip_only_when_a_key_exists`
   runs the true call the moment a key exists; nothing in this phase claims it did.
2. **Grounding is a faithfulness check, not a correctness proof.** A number copied from the evidence
   can still be a *wrong answer* if the evidence is wrong; that is what Phases 2–3 (source authority,
   freshness, relevance, validation caps) and the `research_repro` badge are for. `verify()` proves
   the sentence did not invent or move anything — deliberately a narrower claim.
3. **Number extraction is a heuristic with a documented boundary**: unit-less integers ≤60 and
   1900–2199 are treated as durations/day-numbers/years (so "next 3 hours" and "15-minute interval"
   are not measured), which means a bare, unit-less invented integer in that range can pass. Anything
   carrying a weather unit — including absurd ones like `12345 %` — is always checked.
4. **Clause/wording rules are English-specific** and cue-based (present-tense cues, day cues, quoted
   spans ignored). Multilingual phrasing arrives in Phase 5 with its own cue lists; the numeric,
   source, alert-id, risk and quality checks are language-independent already.
5. **`±0.1` tolerance and one-decimal comparison** mean a model rounding to whole degrees ("23 °C"
   against 22.8) is rejected. That is intentional for a risk product; it can be relaxed per category
   later if it turns out to cost too many regenerations.
6. **One provider, so one source of numbers**: agreement-based checks are neutral (Phase 3, capped at
   10/10 with a note) and `authority` stays 26/40 until a real official meteorological feed (IMD) is
   connected; the grounding layer cannot raise either.
7. **The fallback sentence is engineered for honesty, not for style.** It is deterministic and
   complete but obviously templated; when Groq is available the accepted model reply is what users see,
   and `origin` tells them which one they got (the frontend shows it).
8. `SIMULATE_LLM_HALLUCINATION` exists to prove the guard fires. Its injected text is never displayed —
   after rejection the user sees the fallback — so no demo output can be mistaken for a model success.

## 11. Phase 5 remainder (not started, per this phase's scope)

Multilingual input/output (Marathi/Hindi) including voice (STT/TTS) and a Hindi prompt/response pair;
IMD/rain-gauge or other official source integration to lift `authority` beyond `research_repro`;
GIS/polygon-level alert matching (the CAP polygon endpoint is 403 and inline geometry was 0/20 records,
so `geometry_available=False` remains documented); RAG-style retrieval over historical archives for
"how unusual is this" comparisons; deployment hardening (Docker/K8s, Redis-backed caches, rate limits),
session memory for multi-turn clarification, and the map/timeline visualisations in the frontend.
