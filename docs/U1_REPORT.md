# U1 report — disaster scenarios + official NDMA/SACHET alert UX

Date: 2026-09-02 IST · Scope: the U1 work item only. No U2/U3/U4, no new provider or NWP work,
no new external API, no disaster-prediction model, no new weather thresholds. The Phase 5A
provider architecture and every Phase 1–4 grounding/validation behaviour are preserved.

**The invariants U1 is built on (unchanged, now better exercised):**
WeatherGPT is a grounded weather-intelligence layer, not a forecasting model. NDMA SACHET
official alerts take precedence over generic weather interpretation; the deterministic
advisory/risk logic remains authoritative; the LLM never becomes the source of meteorological
truth. Nothing here fabricates alerts, evacuation orders, warnings or instructions.

---

## 1. What U1 changed, in one paragraph

The CAP `instruction` field was already parsed (Phase 2) and preserved on `Alert.instruction`,
but it reached no user-facing surface. U1 exposes it — quoted verbatim and attributed — in two
deterministic places: the advisory factors (R1/R2 active-alert path) and the deterministic
fallback answer, so the official instruction is visible even with no LLM key configured.
The frontend gained a prominent "official alert active" banner (rendered first, before the
status row), shows description + instruction on each alert card, and surfaces "What WeatherGPT
recommends" (the deterministic advisory) directly under the answer. Disaster-oriented demo
scenarios (heavy rain/flood, thunderstorm/lightning, strong wind, fog, heat) were added as
one-click chips — they exercise the **existing** evidence/advisory pipeline unchanged.

## 2. Files changed

| File | Change |
| --- | --- |
| `backend/services/advisory.py` | R1/R2 active-alert factors now include `official instruction, quoted from <sender>: "…"` when the CAP record carries an instruction (verbatim and attributed; never invented when absent). No rule, threshold or level logic touched. |
| `backend/services/llm.py` | `deterministic_payload()`: (a) leads with the alert and appends `Official instruction from <sender>: "…".` quoted verbatim, (b) a relevant alert whose temporal window the source cannot prove (`validity == "unknown"`, e.g. no expiry published) is no longer announced as "is active" — it is described as published-but-not-proven-active with the recorded `validity_reason`. New `_safe_quote()` keeps quotes balanced so the grounding verifier's quoted-span scrubber cannot be confused by official text containing `"`. |
| `frontend/index.html` | U1 banner `renderOfficialBanner()` rendered **before** the status row whenever `alerts.items` holds an `validity: "active"` alert (severity/urgency pills, area, headline, verbatim instruction, validity window, CAP link, precedence note). Alert cards now show `description` + verbatim `instruction`. Advisory card moved up under the answer and retitled "What WeatherGPT recommends · deterministic risk layer". 5 disaster-scenario chips added. Stale header tag fixed. CSS: `.alert-official`, `.instr`. |
| `backend/main.py` | `APP_VERSION` → `0.5.0-u1` (bookkeeping only). |
| `tests/test_u1_disaster_alerts.py` | **new, 37 offline tests** (see §4). |
| `scripts/check_frontend_render.mjs` | **extended in place** — the existing 7-payload offline render check (answer card, abstain/clarify, outage) is kept whole; the alert fixture now carries `description` + `instruction`, the alert case asserts the U1 invariants (banner first, verbatim instruction shown + attributed, precedence note, recommendation card), a new expired-only case proves expired records never render an active banner (`EXPECT_NOT`), and the script is executed from the pytest file so it runs in CI. |
| `docs/U1_REPORT.md` | this file. |
| `README.md` | Files-table row for the new test file; U1 pointer paragraph in §1. |

Nothing else. In particular: `models.py`, `alerts.py`, `evidence.py`, `grounding.py`,
`validation.py`, `quality.py`, `parsing.py`, the provider registry — all untouched.

## 3. Decisions worth explaining

1. **Instruction exposure is deterministic, not prompt-based.** The LLM already may not omit an
   alert (grounding `alert_presence`). Relying on the prompt to also carry the instruction would
   make a safety-critical text depend on a model's phrasing mood. It is therefore quoted by the
   deterministic fallback (verified by the same grounding checks) and by the advisory factors
   (visible in the evidence and the UI). The model may *also* quote it — it is in the evidence
   object it receives.
2. **Verbatim + attributed, never paraphrased.** An official instruction ("Please follow SDMA
   guidelines.") is the issuing authority's text. Paraphrasing can warp an order; inventing one
   is exactly the forbidden behaviour. Absence stays absence (`None` end-to-end; test 3).
3. **`validity == "unknown"` is not "active" — even in our own sentence.** The fallback used to
   announce any attached alert as "is active". A relevant alert without a provable window is now
   described as published-but-not-proven-active, because an undated alert sold as "active" is
   the same failure class as an expired one shown as live. Only `alerts.classify_validity()`
   declares an alert active. The UI banner reads `validity === "active"` — a backend-decided
   field, never a client-side guess (expired records live in `recent_expired` and cannot reach
   the banner).
4. **Disaster scenarios add zero new thresholds.** U1's hazard list maps onto what the evidence
   pipeline already knows: heavy rain/flood and wind via the documented `THRESHOLDS` heuristics;
   thunderstorms/lightning, fog and violent-rain via the existing WMO-code hazard table (R3);
   official heat-wave alerts via R1. For plain heat there is **no** temperature threshold and
   none was added — the UI reports the measured value verbatim and only an official alert can
   raise the advisory (test 16 pins this). A fake disaster-prediction model was explicitly not
   built.
5. **Official text may be quoted even where our own words are banned.** The grounding
   `safety_wording` check already exempts phrases present in the alert's own
   instruction/headline; the fallback's quoted, attributed rendering passes the full verifier
   (test 21), while the model issuing its own safety guarantee still fails.

## 4. Tests (all offline; `python -m pytest tests/test_u1_disaster_alerts.py -v` → 37 passed)

| # | What it proves |
| --- | --- |
| 1–3 | `instruction` parsed from the real recorded CAP records, survives normalization verbatim, and is **never invented** when the CAP record lacks it (no advisory factor, no sentence). |
| 4–7 | R1 precedence intact: active Severe/Extreme **or** Immediate official alert ⇒ HIGH, cited by id, factor-first; priority survives stale/bad weather evidence; instruction quoted in factors with attribution. |
| 8–10 | Deterministic fallback leads with the alert, quotes the instruction, passes the full verifier; quote-injection safety; `unknown` validity is not relabelled active. |
| 11–13 | Boundary: expired alert never active nor quoted; uncertain-relevance reported (R4) not attached; not-relevant alerts never steer the answer. |
| 14–15 | No-alert disaster scenarios still work on the existing pipeline: thunderstorm/lightning, heavy-rain-now, flood-level day totals, strong and damage-level wind, fog — matched risk level, rule id and factor wording. |
| 16 | Heat: grounded value reported, no invented heat-hazard classification; official Severe heat alert still forces HIGH via R1. |
| 17–21 | Grounding invariants for U1: LLM silence about an active alert rejected; severity softening rejected; "no alerts" claim rejected; risk cannot move; invented alert ids rejected; quoting official orders OK, issuing our own never. |
| 22 | The fallback with an active alert runs the full verifier checklist. |
| 23–25 | End-to-end pipeline (network-free stubs): alert + instruction reach the final answer with the 10-stage trace unchanged; an LLM answer that omits the alert is replaced by the deterministic alert-first answer; a no-alert thunderstorm query stays MEDIUM/R3 with no phantom active alert. |
| 26 | Real fixture replay: the recorded Pune CAP flows to a relevant, active item with instruction preserved — and after the window, to the expired bucket only. |
| 27–29 | Frontend render contract (banner fields, before-status-row order, chips), `scripts/check_frontend_render.mjs` executed, JS syntax `node --check`. |

Full offline suite after the change: **194 passed, 14 live tests deselected** (157 pre-existing
+ 37 new). `node scripts/check_frontend_render.mjs` → all 8 render cases OK (7 pre-existing
payloads plus the U1 expired-not-active case, incl. the new banner/instruction invariants).

## 5. Safety/grounding invariants — confirmation

* NDMA SACHET precedence: R1 untouched and explicitly re-pinned (tests 4–6, 23); the banner and
  the fallback sentence put the official alert physically/textually **before** any model-weather
  summary.
* No fabrication: instruction/headline/description are only ever quoted from the CAP record;
  absence stays `None` (test 3); alert ids cited anywhere must exist in evidence (test 20).
* No active-when-not-active: expired ⇒ `recent_expired` only (tests 11, 26); `unknown` validity
  is labelled unprovable, not active (test 10); relevance-uncertain alerts are disclosed, not
  attached (test 12).
* The LLM cannot omit or contradict the official alert: omission, softening, denial and risk
  movement are all rejected by the verifier (tests 17–19, 24).
* WeatherGPT never issues orders or safety guarantees of its own (test 21); the advisory
  disclaimer and "weather-related risk" wording are unchanged.
* Alert outage semantics unchanged: `unavailable` ≠ "no alerts" (existing Phase 2/3/4 suites all
  green).
