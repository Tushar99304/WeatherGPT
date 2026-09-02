/*
 * scripts/check_frontend_render.mjs — offline render test for frontend/index.html.
 *
 * The page is one inline <script> with no build step, so it can be evaluated directly with a fake
 * `document`/`fetch`. The payloads cover the states that have broken this page before (missing
 * blocks, absent alerts, abstain/clarify), the Phase 4 answer card, and the U1 official-alert UX
 * (prominent banner first, verbatim instruction shown and attributed, expired records never
 * rendered as active). Nothing here needs a backend, a key, or the internet — the payloads are
 * written out below so the assertions are reviewable as text.
 *
 * Run:  node scripts/check_frontend_render.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const html = fs.readFileSync(path.join(ROOT, "frontend/index.html"), "utf8");
const src = html.match(/<script>([\s\S]*?)<\/script>/)[1];

const HEALTH = {
  weather_provider: "open-meteo",
  alerts: { enabled: true, max_age_h: 24, detail_limit: 6 },
  llm: { configured: false, provider: "groq", model: "llama-3.3-70b-versatile" },
  simulations: {},
};

const SRC_W = {
  name: "Open-Meteo", type: "forecast", timestamp: "2026-09-01T07:45",
  authority: "research_repro", url: "https://api.open-meteo.com/v1/forecast?fake=1",
};
const SRC_A = {
  name: "NDMA SACHET", type: "official_alert", timestamp: "2026-09-01T02:29:00Z",
  authority: "official", url: "https://sachet.ndma.gov.in/x",
};
const WEATHER = {
  provider: "open-meteo", kind: "live", requested_timeframe: "now",
  retrieved_at_utc: "2026-09-01T02:30:00Z", current: {
    time: "2026-09-01T07:45", temperature_c: 25.8, apparent_temperature_c: 28.0,
    humidity_pct: 88, precipitation_mm: 0, wind_speed_kmh: 12.4, condition: "Overcast",
  },
  today: { date: "2026-09-01", label: "Today", is_forecast: true, temperature_max_c: 30,
           temperature_min_c: 23, precipitation_sum_mm: 12,
           precipitation_probability_max_pct: 60, condition: "Light rain" },
  request_url: "https://api.open-meteo.com/v1/forecast?fake=1",
};
const LOCATION = {
  name: "Pune", latitude: 18.51957, longitude: 73.85535, admin1: "Maharashtra", admin2: "Pune",
  country: "India", timezone: "Asia/Kolkata", utc_offset_seconds: 19800, population: 3115431,
  geonames_id: 1259229, feature_code: "PPL",
};
const VALIDATION = {
  ok: true, sufficient: true, fresh: true, complete: true, location_resolved: true,
  timestamp_present: true, values_plausible: true, alerts_valid: true,
  source_age_minutes: 15, checks_run: ["freshness", "value_ranges"], failures: [], warnings: [],
};
const ADVISORY = {
  risk_level: "LOW", activity: "outdoor activity/travel",
  headline: "Weather-related travel risk is LOW based on validated model weather and an official-alert check that came back empty.",
  reason: "Current retrieved evidence shows no hazardous values for the asked timeframe.",
  factors: ["NDMA SACHET checked: no active official alert verifiably tied to this location"],
  rules_fired: ["R7_quiet"], alert_ids: [], evidence_quality: "HIGH",
  disclaimer: "Weather-related risk estimate derived from validated evidence — not an official order.",
};
function answer(text, over = {}) {
  return {
    text, source: "Open-Meteo + NDMA SACHET", timestamp: "2026-09-01T07:45", risk: "LOW",
    evidence_quality: "HIGH", alert_mentioned: false, origin: "groq_llm",
    grounding: {
      verified: true, numbers_checked: 3, numbers_rejected: [], attempts: 1, regenerated: false,
      llm_status: "ok", model: "llama-3.3-70b-versatile", latency_ms: 812.4, note: "",
      checks_run: ["required_fields", "numbers(3)", "source_identity"], failures: [],
    },
    ...over, grounding: {
      verified: true, numbers_checked: 3, numbers_rejected: [], attempts: 1, regenerated: false,
      llm_status: "ok", model: "llama-3.3-70b-versatile", latency_ms: 812.4, note: "",
      checks_run: ["required_fields", "numbers(3)", "source_identity"], failures: [],
      ...(over.grounding || {}),
    },
  };
}
function evidence(over = {}) {
  return {
    schema_version: "3", status: "grounded",
    request: { message: "weather in pune now", intent: "forecast_current", timeframe: "now" },
    location: LOCATION, weather: WEATHER,
    alerts: { source: "NDMA SACHET", authority: "official", state: "checked", mode: "live",
              checked_at_utc: "2026-09-01T02:29:00Z", items: [], items_in_feeds: 0,
              details_fetched: 0, feeds_considered: ["rss_maharashtra.xml"], notes: [],
              duration_ms: 640 },
    sources: [SRC_A, SRC_W], validation: VALIDATION, evidence_quality: "HIGH",
    quality_breakdown: { score: 86, label: "HIGH", weights: { authority: 40 } },
    risk: "LOW", advisory: ADVISORY, alert_state: "checked",
    ...over,
  };
}
const ALERT_ITEM = {
  alert_id: "IN-50", source: "NDMA SACHET", sender: "IMD Mumbai", event: "Heavy Rain",
  headline: "Heavy rain alert for Pune district",
  description: "Heavy to very heavy rain likely over parts of Pune district.",
  instruction: "Please follow SDMA guidelines.",
  area_desc: "Pune district of Maharashtra",
  severity: "Severe", urgency: "Immediate", certainty: "Likely", validity: "active",
  effective_at: "2026-09-01T02:00:00Z", expires_at: "2026-09-01T05:00:00Z",
  validity_reason: "inside the published window",
  relevance: { status: "relevant", level: "L1_exact_locality", reason: "areaDesc names this place",
               geometry_available: false },
  raw_source_url: "https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=50",
};
// U1 boundary fixture: the same alert after its window. It must only ever render inside the
// labelled transparency bucket — never in the active banner, never quoted as current guidance.
const ALERT_EXPIRED = {
  ...ALERT_ITEM, alert_id: "IN-OLD", validity: "expired", expires_at: "2026-09-01T04:00:00Z",
  validity_reason: "expired at 2026-09-01T04:00:00Z",
  instruction: "an expired instruction that must never render as active guidance",
};

const CASES = {
  groq_ok: {
    status: "grounded",
    evidence: evidence(),
    answer: answer("It is 25.8 °C in Pune right now, with wind at 12.4 km/h."),
    pipeline: { stages: [{ stage: "llm", status: "ok" }, { stage: "grounding", status: "ok" }] },
  },
  fallback_no_key: {
    status: "grounded",
    evidence: evidence(),
    answer: answer("Currently 25.8 °C, wind 12.4 km/h. Source: Open-Meteo + NDMA SACHET.", {
      origin: "deterministic_fallback",
      grounding: { llm_status: "no_key", attempts: 0, model: null, latency_ms: 1.2,
                   note: "GROQ_API_KEY is not set — deterministic evidence-based answer used" },
    }),
    pipeline: { stages: [{ stage: "llm", status: "skipped" }, { stage: "grounding", status: "ok" }] },
  },
  rejected_then_fallback: {
    status: "grounded",
    evidence: evidence(),
    answer: answer("Currently 25.8 °C in Pune.", {
      origin: "deterministic_fallback",
      grounding: {
        llm_status: "grounding_failed", attempts: 2, regenerated: true, verified: true,
        numbers_rejected: ["31.4 °c"],
        failures: ["[rejected model reply] answer states 31.4 c, but no such temperature value "
                   + "exists in the evidence (known: 25.8, 28)"],
      },
    }),
    pipeline: { stages: [{ stage: "llm", status: "fallback" }, { stage: "grounding", status: "ok" }] },
  },
  alert_active: {
    status: "grounded",
    evidence: evidence({
      alerts: { ...evidence().alerts, items: [ALERT_ITEM], items_in_feeds: 1, details_fetched: 1 },
      risk: "HIGH",
      advisory: { ...ADVISORY, risk_level: "HIGH", rules_fired: ["R1_severe_or_extreme_alert"],
                  alert_ids: ["IN-50"],
                  factors: ["official Severe Heavy Rain from IMD Mumbai (valid until 2026-09-01T05:00:00Z)",
                            'official instruction, quoted from IMD Mumbai: "Please follow SDMA guidelines."'],
                  headline: "Weather-related travel risk is HIGH based on an active official alert." },
    }),
    answer: answer("A Severe Heavy Rain alert is active for Pune; it is 25.8 °C right now.", {
      risk: "HIGH", alert_mentioned: true,
    }),
    pipeline: { stages: [{ stage: "llm", status: "ok" }, { stage: "grounding", status: "ok" }] },
  },
  u1_expired_not_active: {
    status: "grounded",
    evidence: evidence({
      alerts: { ...evidence().alerts, items: [], recent_expired: [ALERT_EXPIRED],
                items_in_feeds: 1, details_fetched: 1 },
    }),
    answer: answer("No active official alert was verifiably tied to this location when SACHET was "
                   + "checked; that is a checked result, not a promise that none exists. "
                   + "It is 25.8 °C now."),
    pipeline: { stages: [{ stage: "llm", status: "ok" }, { stage: "grounding", status: "ok" }] },
  },
  unavailable: {
    status: "grounded",
    evidence: evidence({
      alerts: { ...evidence().alerts, state: "unavailable", error: "HTTP 503 from sachet.ndma.gov.in",
                mode: "live", sources: undefined },
      sources: [SRC_W],
      risk: "UNCERTAIN",
      advisory: { ...ADVISORY, risk_level: "UNCERTAIN", rules_fired: ["R5_alerts_unverifiable"],
                  headline: "Weather-related travel risk is UNCERTAIN: the official alert service "
                            + "could not be consulted." },
    }),
    answer: answer("The official alert service could not be verified at this time, so whether any "
                   + "alert is active for this location is unknown. Currently 25.8 °C.", {
      risk: "UNCERTAIN", source: "Open-Meteo",
      grounding: { llm_status: "upstream_error", verified: true, attempts: 1 },
    }),
    pipeline: { stages: [{ stage: "llm", status: "fallback" }, { stage: "grounding", status: "ok" }] },
  },
  abstain: {
    status: "abstain",
    evidence: evidence({
      status: "abstain",
      abstain_reason: "I could not verify this evidence well enough to answer from it (stale). "
                    + "I will not present unverified numbers as fact.",
      validation: { ...VALIDATION, ok: false, sufficient: false, fresh: false,
                     failures: ["provider timestamp is 361 min old, over the 90 min limit"],
                     warnings: ["staleness"] },
      evidence_quality: "LOW", risk: "UNCERTAIN",
      quality_breakdown: { score: 56, label: "LOW" },
    }),
    answer: answer("I could not verify reliable weather information for this place and time, so I "
                   + "will not guess.", {
      risk: "UNCERTAIN", evidence_quality: "LOW", origin: "deterministic_fallback",
      grounding: { llm_status: "skipped", verified: true, numbers_checked: 0,
                   note: "evidence failed validation — abstention only" },
    }),
    pipeline: { stages: [{ stage: "llm", status: "skipped" }, { stage: "grounding", status: "ok" }] },
  },
  clarify: {
    status: "clarify",
    evidence: evidence({
      status: "clarify", weather: null, sources: [], alerts: { ...evidence().alerts, state: "not_checked" },
      clarification: "I found multiple places matching “springfield”. Which location do you mean?",
      validation: { ...VALIDATION, ok: false, sufficient: false, complete: null },
      advisory: null, risk: null, evidence_quality: null, quality_breakdown: {}, alert_state: "not_checked",
    }),
    answer: null,
    pipeline: { stages: [{ stage: "llm", status: "skipped" }, { stage: "grounding", status: "ok" }] },
  },
};

const EXPECT = {
  groq_ok: [/class="banner"/, /Answer<\/h2>/, /answer: Groq, grounded/, /grounding: verified/,
            /checked 3 number claim\(s\)/, /1 model attempt\(s\)/, /llm: ok/,
            /deterministic risk layer/, /Evidence Quality/, /exact API call the numbers came from/],
  fallback_no_key: [/deterministic \(LLM not used or rejected\)/, /grounding: verified/,
                    /llm: no_key/, /no model attempt/, /GROQ_API_KEY is not set/],
  rejected_then_fallback: [/grounding: verified/, /rejected: 31\.4 °c/,
                           /2 model attempt\(s\) · regenerated once after the verifier/,
                           /\[rejected model reply\]/],
  alert_active: [/authority: official/, /IN-50/, /Severe/, /active alert mentioned/,
                 /R1_severe_or_extreme_alert/, /cites IN-50/,
                 // ---- U1: the official alert is impossible to miss ----
                 /official NDMA \/ SACHET alert active/,                    // the prominent banner
                 /alert-official[\s\S]*class="banner"/,                     // rendered BEFORE the status row
                 /urgency Immediate/,                                       // urgency shown
                 /Please follow SDMA guidelines\./,                          // instruction quoted verbatim
                 /Official instruction/,                                    // labelled as the authority's words
                 /quoted verbatim from the CAP record/,                     // provenance of the quote
                 /outranks all model-weather interpretation/,               // precedence is stated
                 /What WeatherGPT recommends/,                              // recommendation beside the answer
                 /official instruction, quoted from IMD Mumbai/],            // advisory factor quote
  u1_expired_not_active: [/SACHET was checked/, /shown for transparency only/, /IN-OLD/,
                 // the expired instruction may only appear AFTER the transparency label
                 /shown for transparency only[\s\S]*expired instruction/],
  unavailable: [/SACHET could not be reached/, /not evidence that no alert exists/,
                /risk <b>UNCERTAIN<\/b>/, /llm: upstream_error/],
  abstain: [/abstained|could not verify/i, /Why this was not trusted/, /provider timestamp is 361 min old/,
            /answer above was generated from this exact payload/],
  clarify: [/needs clarification|multiple places/i, /raw Evidence JSON/, /no answer sentence was produced/i],
};

// "must NOT appear" assertions (U1): an expired alert's banner must never exist, and nothing
// must promise an active alert when the evidence holds none.
const EXPECT_NOT = {
  u1_expired_not_active: [/official NDMA \/ SACHET alert active/],
  groq_ok: [/official NDMA \/ SACHET alert active/],
  fallback_no_key: [/official NDMA \/ SACHET alert active/],
  abstain: [/official NDMA \/ SACHET alert active/],
  clarify: [/official NDMA \/ SACHET alert active/],
};

let failures = 0;
for (const [name, data] of Object.entries(CASES)) {
  const els = {};
  const document = {
    getElementById: (id) => (els[id] ||= { innerHTML: "", textContent: "", value: "", disabled: false,
                                           addEventListener: () => {} }),
    addEventListener: () => {},
  };
  const fetch = async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    return { json: async () => (url === "/health" ? HEALTH : data) };
  };
  let ask, render;
  try {
    ({ ask, render } = new Function("document", "fetch", `${src}\n;return { ask, render };`)(document, fetch));
  } catch (e) {
    console.log(`${name.padEnd(24)} SCRIPT EVAL FAILED -> ${e.message}`);
    failures++;
    continue;
  }
  try {
    render(data);
    await ask("probe");
  } catch (e) {
    console.log(`${name.padEnd(24)} RENDER THREW -> ${String(e.stack).split("\n").slice(0, 3).join(" | ")}`);
    failures++;
    continue;
  }
  const out = els.out.innerHTML || "";
  const leaked = ["undefined", "NaN", "[object Object]"].filter((t) => out.includes(t));
  const missing = EXPECT[name].filter((re) => !re.test(out));
  const forbidden = (EXPECT_NOT[name] || []).filter((re) => re.test(out));
  const status = !missing.length && !leaked.length && !forbidden.length ? "OK " : "BAD";
  console.log(`${name.padEnd(24)} ${status}  html=${String(out.length).padStart(6)} chars` +
              `${leaked.length ? `  leaked=${leaked.join(",")}` : ""}` +
              `${missing.length ? `  missing=${missing.map(String).join(" , ")}` : ""}` +
              `${forbidden.length ? `  forbidden=${forbidden.map(String).join(" , ")}` : ""}`);
  if (status === "BAD") failures++;
}
console.log(failures ? `FAILURES: ${failures}` : `ALL ${Object.keys(CASES).length} RENDER CASES OK`);
process.exit(failures ? 1 : 0);
