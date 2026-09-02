# Phase 3 report — validation, Evidence Quality, deterministic advisory

WeatherGPT · SIH26068 · written 2026-09-01 ~03:20 IST · previous state: Phases 1–2, 53 tests.

---

## 1. What already existed (and was reused, not rebuilt)

Verified by reading the files before writing anything:

| Existing thing | How Phase 3 uses it |
| --- | --- |
| `Validation` model with `ok/sufficient/fresh/complete/location_resolved/timestamp_present/values_plausible/alerts_valid/checks_run/failures/warnings` | **extended** with 3 optional fields (`labeling_consistent`, `alert_integrity`, `source_age_minutes`); nothing removed, nothing renamed |
| `config.WEATHER_MAX_STALENESS_MIN` (90) and `config.RANGES` (6 physical ranges) | freshness limit and the plausibility filter are read from there — no second copy of either |
| `weather.minutes_since_source()` + `weather._utc_now()` (Phase 1) | the ONE freshness computation; `_utc_now` is the injection point that makes it deterministic under test |
| `Evidence.evidence_quality`, `Evidence.quality_breakdown`, `Evidence.risk` | already-reserved slots, now filled; no new pipeline contract |
| Phase 2's `alert_intent_blocker` written into `quality_breakdown` | merged, not overwritten (`test_40` asserts it survives) |
| Phase 2's `AlertsEvidence.state` / `Alert.validity` / `Alert.relevance` | the alert half of validation, quality caps and the advisory's top rule |
| `parsing` intent labels incl. `advisory_risk`, `official_alert` | completeness requirement lists and the advisory's `activity` field key off them |
| `demo_outputs/` + `scripts/demo_phaseN.py` convention | `scripts/demo_phase3.py` follows it |

## 2. Where Phase 3 connects

```
backend/services/validation.py   (new)  six checks + validate_evidence() orchestrator + advisory_references_ok()
backend/services/quality.py      (new)  score_evidence(ev, validation) -> (label, breakdown)
backend/services/advisory.py     (new)  advise(ev) -> Advisory
backend/models.py              (edited) +Advisory model, +3 optional Validation fields, +Evidence.advisory
backend/services/evidence.py   (edited) one line: dropped the now-false "Phase 3 not run" warning
backend/main.py                (edited) after the evidence stage: validate -> quality -> advise;
                                      abstain when validation is insufficient; /health exposes the
                                      weights + thresholds; version 0.3.0-phase3
frontend/index.html            (edited) shows the advisory card + "why this was not trusted"
tests/test_phase3_units.py     (new) 40 offline tests · tests/test_phase3_live.py (new) 4 live tests
```

Pipeline stages are now exactly `parse → geocode → retrieve_weather → retrieve_alerts → evidence → validate → quality → advise`. The early clarify/abstain paths short-circuit **before** the new stages, exactly as they did in Phase 1, so those behaviours are untouched.

## 3. Conflicts between the spec and the existing code — and what I did about each

1. **"If alert retrieval is unavailable → cap quality at MEDIUM" vs treating it as a validation failure.** Read literally as a failure, every weather answer would abstain during a SACHET outage. Resolution: an outage is a *warning* + `alerts_valid=False` + the rule-1 cap + a refusal by the advisory to call conditions low-risk (R5); it becomes a hard validation failure **only when the question was about alerts**. Test `test_07` pins both halves.
2. **The spec's `quality_breakdown` shape is `{score,label,breakdown{…}}` but `Evidence.quality_breakdown` is a flat dict that Phase 2 already writes into.** Resolution: I kept the flat dict, put the spec's exact keys at the top level (`score`, `label`, `breakdown` with the four weights + `caps_applied`) and merged over the existing content, so `alert_intent_blocker` survives. No second container added.
3. **"Expired alerts must not increase current evidence quality" vs `validate_evidence` also running for an archive answer.** An archive lookup is *correctly* old, so applying the staleness rule to it would make every "rain on 2026-06-14?" question abstain. Resolution: freshness (and its rule-4 cap) is skipped when `weather.kind == "historical"`; an alert-only question with no weather block is dated from `alerts.checked_at_utc` instead of scoring 0.
4. **`sources[].timestamp` semantics.** For a "tomorrow" answer, evidence.py stamps the source with the *current* block time (it means "as of"). The spec's "current vs forecast" check could have been satisfied by rewriting that, which would change Phase 1's contract. Resolution: the labelling check verifies the block we actually answer from (its `date`, `label`, `is_forecast`, and that a "tomorrow" answer cannot silently fall back to today — that case is now a **failure**, which is a real bug the test caught), and the source timestamp is left alone. Phase 4's prompt must read `weather.tomorrow`, not `sources[].timestamp`, to describe the day.
5. **Agreement needs ≥2 comparable sources, and only one weather provider exists.** Not invented: with one source, agreement is reported as a neutral full 10/10 *with a note saying it is not measurable*; the comparison code (period + value comparison, never averaging) is implemented and exercised by `test_18` with a second synthetic source, so it is ready for IMD rather than written on top of a guess.
6. **One Phase-1 test changed.** `tests/test_phase1_live.py` asserted the exact stage list; it now asserts the full 8-stage list, still exactly and still in order, plus `validation.ok/sufficient`, the quality label, and `risk == advisory.risk_level`. Nothing was loosened; the only previous exception was `test_no_alerts_for_nagpur`, untouched.
7. **`Evidence.risk` vs a new `Advisory` object** — both exist, and `risk` is assigned from `advisory.risk_level` in the same statement, so the scalar the UI reads and the structured reason can never drift apart.

## 4. Exact commands and results (actually run, this session)

```
$ python3 -m pytest tests
97 passed in 16.23s

$ python3 -m pytest tests -m "not live"
87 passed, 10 deselected in 0.38s

$ python3 -m pyflakes backend/ tests/ scripts/
(no output = clean)

$ python3 scripts/demo_phase1.py
6/6 scenarios matched expectations.

$ python3 scripts/demo_phase2.py            # live
3/3 cases passed.
$ python3 scripts/demo_phase2.py --fixture
3/3 cases passed.

$ python3 scripts/demo_phase3.py            # live
5/5 cases passed: case1:PASS, case2:PASS, case3:PASS, case4:PASS, case5:PASS
```

Composition: Phase 1 15 offline + 3 live · Phase 2 32 offline + 3 live · Phase 3 40 offline + 4 live = 97.

## 5. What the live run showed

| Case | Live result |
| --- | --- |
| 1 · Nagpur now | `grounded`, quality **HIGH (86)** = authority 26/40 + freshness 30 + completeness 20 + agreement 10, risk **LOW**, rule `R7_quiet` |
| 2 · live alert | discovered **Giridih (Jharkhand)**, severity Moderate, `L1_exact_locality` → quality **HIGH (100)** (authority 40/40: official source consulted), risk **MEDIUM** via `R2_active_official_alert` — earlier the same evening Mayurbhanj's *Severe* alert produced **HIGH** via `R1` |
| 3 · Pune, nothing relevant | `grounded`, quality HIGH (100), risk LOW, warning keeps the exact wording *"that is NOT the same as 'no alert exists'"* |
| 4 · `SIMULATE_ALERT_FAILURE=true` | `alerts_valid=False`, error preserved, quality **MEDIUM (86)** with `rule 1` recorded in `caps_applied`, risk **UNCERTAIN** via `R5_alerts_unverifiable` |
| 5 · `SIMULATE_STALE_DATA=true` | `fresh=False` ("provider timestamp is 361 min old, over the 90 min limit"), quality **LOW (56)** with `rule 4` + `rule 3`, status **abstain**, risk **UNCERTAIN** via `R6_insufficient_evidence` — and the numbers stay in the payload rather than being hidden |

## 6. Example real payload (captured live after the final code change, not authored)

```json
{
  "validation": {
    "ok": true,
    "sufficient": true,
    "fresh": true,
    "complete": true,
    "location_resolved": true,
    "timestamp_present": true,
    "values_plausible": true,
    "alerts_valid": true,
    "labeling_consistent": true,
    "alert_integrity": true,
    "source_age_minutes": 6.3,
    "checks_run": [
      "alerts_consulted",
      "phase1_presence_checks_only",
      "location_sanity",
      "freshness",
      "value_ranges",
      "current_vs_forecast_labelling",
      "completeness_for_intent",
      "timestamp_present",
      "advisory_alert_references"
    ],
    "failures": [],
    "warnings": [
      "SACHET was checked: no active official alert is verifiably tied to this location (that is NOT the same as 'no alert exists')"
    ]
  },
  "evidence_quality": "HIGH",
  "quality_breakdown": {
    "score": 86,
    "label": "HIGH",
    "weights": {
      "authority": 40,
      "freshness": 30,
      "completeness": 20,
      "agreement": 10
    },
    "breakdown": {
      "authority": 26.0,
      "freshness": 30.0,
      "completeness": 20.0,
      "agreement": 10.0,
      "caps_applied": []
    }
  },
  "risk": "LOW",
  "advisory": {
    "risk_level": "LOW",
    "activity": "outdoor activity/travel",
    "headline": "Weather-related travel risk is LOW based on validated model weather and an official-alert check that came back empty.",
    "reason": "Current retrieved evidence shows no hazardous values for the asked timeframe, and SACHET was checked with no active official alert verifiably tied to this location. That is a checked result, not a promise that none exists — and not a statement about anyone's personal safety.",
    "factors": [
      "NDMA SACHET checked: no active official alert verifiably tied to this location"
    ],
    "rules_fired": [
      "R7_quiet"
    ],
    "alert_ids": [],
    "evidence_quality": "HIGH",
    "disclaimer": "Weather-related risk estimate derived from validated evidence (official alerts + model weather). It is not an official order, an evacuation instruction, or a guarantee of personal safety."
  }
}
```

## 7. Limitations (all visible in the payload)

1. The weights and the `HIGH ≥ 80 / MEDIUM ≥ 55` thresholds are a documented MVP convention; they are printed in `quality_breakdown.weights/thresholds` so anyone can disagree with a specific number instead of guessing.
2. Authority is capped at 26/40 for weather while the only provider is Open-Meteo. That is honesty, not a bug: the number rises when IMD is connected, and nothing here pretends otherwise.
3. `agreement` cannot be measured with one provider — reported as such, never simulated.
4. Completeness is judged against per-intent required-field lists I wrote; a new intent needs its list extended (it is one function, `required_fields`).
5. Advisory thresholds (7.5 mm/15 min, 50/115 mm/day, 40/85 km/h wind, WMO hazard codes) are engineering heuristics with a rationale each in `advisory.THRESHOLDS`; they are not IMD criteria and the demo page says so.
6. No per-hour/nowcast granularity: a daily forecast block and a 15-minute current block are treated as the same "hazard evidence".
7. Validation does not judge the *relevance ladder* itself (Phase 2 owns it); it judges whether the ladder's output is well-formed and honestly attached.
8. Nothing yet checks `past_days` values against the archive's own latency (archive data can lag ~5 days; only `kind`/labelling is checked).
9. There is still no user-facing answer sentence: `/api/query` returns evidence + decision, and Phase 4 will phrase it under grounding rules.

## 8. Phase 4 next (grounded LLM explanation — not started)

1. `backend/services/llm.py`: Groq via `GROQ_API_KEY` (`https://api.groq.com/openai/v1/chat/completions`, OpenAI-compatible, `response_format={"type":"json_object"}`, `temperature=0`), payload = `evidence.model_dump()` only, no chat history. Documented model: `llama-3.3-70b-versatile`.
2. System prompt exactly as drafted in `docs/PLAN_48H.md` §F, extended with: it must echo `advisory.headline`'s risk level and may not change it; if `validation.sufficient` is false it must say reliable information could not be verified.
3. `backend/services/grounding.py`: required keys present; every number in the answer must appear in the evidence value set (±0.1, incl. word-numbers); `source`/`timestamp` must equal a `sources[]` entry; **if `alerts.items` is non-empty the answer must mention the alert**; reuse `validation.alert_ids_present(ev.alerts, ids_from_answer)` so a hallucinated alert id is caught by the same rule the advisory is held to. One stricter regeneration, then the deterministic fallback text built from the evidence, with `grounding.verified=false` visible in the response.
4. Fallback path must keep working with no key set (the demo cannot depend on a key).
5. Then Phase 5's UI polish; Phase 3's fields are already rendered by `frontend/index.html`.
