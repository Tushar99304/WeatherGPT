"""
geocoding.py — place name -> coordinates, with EXPLICIT ambiguity handling.

Primary endpoint (free, no key, no signup):
  GET https://geocoding-api.open-meteo.com/v1/search?name=Pune&count=10&language=en&format=json
  Docs: https://open-meteo.com/en/docs/geocoding-api   (GeoNames data)

Two hard product rules implemented here:

  (A) We never SILENTLY pick a location. If two materially different places share the
      name, we return status="ambiguous" and the caller asks the user.

  (B) We never treat noise as ambiguity. GeoNames (verified live, 2026-09-01) returns
      both "Nagpur, Maharashtra (pop 2.4M)" AND a hamlet called "Nagpur" in Uttar Pradesh.
      Asking the user to disambiguate Nagpur would be absurd, so a candidate only counts
      as a competing place if it is a real settlement: population >= AMBIGUITY_MIN_POP
      (default 100 000) or an administrative seat (feature_code PPLA*/PPLC).
      Anything else is ignored, and the ignored names are written into
      ResolvedLocation.resolution_note so the UI can DISCLOSE what we assumed.
      Honest and non-silent, but not obstructive.

  (C) GeoNames misses smaller Indian towns (verified: "Lonavala" => 0 results). Optional
      OpenStreetMap Nominatim fallback is tried when the primary source finds nothing; if
      that also fails we return unresolved -> the caller abstains. We never guess coordinates.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from backend import config
from backend.models import GeoCandidate, GeocodeResult, ResolvedLocation
from backend.services.http_client import UpstreamError, get_json

# Words that are part of the question, never part of the place name.
STOPWORDS = {
    "the", "a", "an", "of", "in", "at", "for", "to", "is", "are", "was", "were",
    "what", "whats", "what's", "how", "will", "would", "does", "do", "did", "be",
    "it", "there", "here", "please", "tell", "me", "weather", "forecast", "today",
    "tomorrow", "yesterday", "right", "now", "this", "next", "week", "day", "night",
    "morning", "evening", "rain", "raining", "temperature", "hot", "cold", "humid",
    "wind", "windy", "alert", "alerts", "warning", "warnings", "any", "current",
    "like", "going", "over", "during", "should", "i", "we", "safe", "travel",
    "check", "give", "expect", "expected", "chance", "probability", "much",
    "near", "around", "close", "side", "area", "district", "city",
}
ADMIN_SUFFIXES = re.compile(r"\b(district|city|taluka|tehsil|nagar|mandal)\b", re.I)

# feature codes that mark an administrative seat: these always count as a real place
SEAT_FEATURE_CODES = {"PPLC", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLH"}


def clean_location(text: str) -> str:
    """Strip question words/punctuation from an extracted location phrase."""
    t = re.sub(r"[^\w\s,.'-]", " ", (text or "").lower(), flags=re.UNICODE)
    words = [w for w in t.split() if w.strip(".,'") and w not in STOPWORDS]
    return " ".join(words).strip(" .,'-")


def _norm(value: Optional[str]) -> str:
    # keep latin + devanagari; everything else (diacritics stripped by the class) collapses
    return re.sub(r"[^a-z0-9\u0900-\u097F]+", " ", (value or "").lower()).strip()


def _norm_strict(value: Optional[str]) -> str:
    """Diacritic-insensitive comparison so 'Nāgpur' matches 'Nagpur'."""
    import unicodedata

    s = unicodedata.normalize("NFKD", (value or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9\u0900-\u097F ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _is_material(r: Dict[str, Any], min_pop: int) -> bool:
    pop = r.get("population") or 0
    if pop >= min_pop:
        return True
    return (r.get("feature_code") or "").upper() in SEAT_FEATURE_CODES


def _place_labels(rows: List[Dict[str, Any]], limit: int = 4) -> List[str]:
    """'Springfield (Missouri, United States)' style labels, most populous first."""
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(
            ((r.get("country_code") or "").upper(), _norm_strict(r.get("admin1"))), []
        ).append(r)
    out = []
    for rows_g in groups.values():
        r = max(rows_g, key=lambda x: x.get("population") or 0)
        where = ", ".join(x for x in [r.get("admin1"), r.get("country")] if x)
        out.append((-(r.get("population") or 0), f"{r.get('name')}{f' ({where})' if where else ''}"))
    return [label for _, label in sorted(out)[:limit]]


def _to_candidate(r: Dict[str, Any]) -> GeoCandidate:
    return GeoCandidate(
        name=r.get("name") or "",
        latitude=float(r["latitude"]),
        longitude=float(r["longitude"]),
        admin1=r.get("admin1"),
        admin2=r.get("admin2"),
        country=r.get("country"),
        country_code=r.get("country_code"),
        population=r.get("population"),
    )


def _to_location(r: Dict[str, Any], note: str) -> ResolvedLocation:
    return ResolvedLocation(
        name=r.get("name") or "unknown",
        latitude=float(r["latitude"]),
        longitude=float(r["longitude"]),
        country=r.get("country"),
        country_code=r.get("country_code"),
        admin1=r.get("admin1"),
        admin2=r.get("admin2"),
        timezone=r.get("timezone"),
        utc_offset_seconds=r.get("utc_offset_seconds"),
        population=r.get("population"),
        feature_code=r.get("feature_code"),
        geonames_id=r.get("id"),
        resolution_note=note,
    )


# --------------------------------------------------------------------------- #
# I/O: primary then optional fallback geocoder
# --------------------------------------------------------------------------- #
async def search(name: str, count: Optional[int] = None) -> Tuple[List[Dict[str, Any]], str]:
    """Return (results, source_name). Never raises for 'no match' — only for transport failure."""
    params: Dict[str, Any] = {
        "name": name,
        "count": count or config.GEO_MAX_RESULTS,
        "language": "en",
        "format": "json",
    }
    data = await get_json(config.OPEN_METEO_GEOCODING_URL, params=params, service="geocoding")
    results = data.get("results") or []
    return (results if isinstance(results, list) else []), "open-meteo-geocoding"


async def search_nominatim(name: str) -> List[Dict[str, Any]]:
    """
    OSM Nominatim, used ONLY when GeoNames has nothing. Free, no key.
    Policy respected: custom User-Agent, max 1 request/second, tiny volume per demo.
    Docs: https://nominatim.org/release-docs/latest/api/Search/  ·  Usage policy: https://usage-policy.nominatim.openstreetmap.org/
    """
    country = (config.GEO_COUNTRY_BIAS or "").lower()
    params: Dict[str, Any] = {"q": name, "format": "jsonv2", "limit": 5, "addressdetails": 1}
    if country:
        params["countrycodes"] = country
    try:
        raw = await get_json(
            config.NOMINATIM_URL,
            params=params,
            service="nominatim",
            retries=0,
            allow_list=True,
            headers={"User-Agent": config.NOMINATIM_USER_AGENT},
        )
    except UpstreamError:
        return []
    items = raw if isinstance(raw, list) else []
    out: List[Dict[str, Any]] = []
    for it in items:
        try:
            lat, lon = float(it["lat"]), float(it["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        addr = it.get("address") or {}
        out.append(
            {
                "name": it.get("name") or addr.get("city") or addr.get("town") or addr.get("village") or name,
                "latitude": lat,
                "longitude": lon,
                "country": addr.get("country"),
                "country_code": (addr.get("country_code") or "").upper() or None,
                "admin1": addr.get("state"),
                "admin2": addr.get("district") or addr.get("county"),
                "timezone": None,
                "population": None,
                "feature_code": f"OSM:{it.get('category', 'place')}/{it.get('type', '')}",
                "osm_display_name": it.get("display_name"),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Pure decision logic (no I/O) -> trivially unit-testable
# --------------------------------------------------------------------------- #
def disambiguate(
    query_place: str,
    results: List[Dict[str, Any]],
    context: Optional[str] = None,
    country_bias: Optional[str] = None,
) -> GeocodeResult:
    """
    Rules, in order:
      1. Exact name matches win (diacritic/case-insensitive) when present.
      2. Country bias (default IN) applied only if it leaves something behind.
      3. Explicit context ("Pune, Maharashtra") narrows first — user-supplied hints beat heuristics.
      4. Drop immaterial hamlets (see module docstring rule B).
      5. Group remaining by distinct place (country_code, admin1). >1 group => ask the user.
         Exactly 1 group => resolve, choosing the largest settlement, disclosing the rest.
      6. Nothing left => unresolved.
    """
    bias = (country_bias if country_bias is not None else config.GEO_COUNTRY_BIAS or "").upper()
    raw = [r for r in results if r.get("latitude") is not None and r.get("longitude") is not None]
    if not raw:
        return GeocodeResult(
            status="unresolved", query=query_place, evidence_gap="no_geocode_match"
        )

    want = _norm_strict(query_place)
    exact = [r for r in raw if _norm_strict(r.get("name")) == want]
    pool = exact or raw

    notes: List[str] = []
    if bias:
        biased = [r for r in pool if (r.get("country_code") or "").upper() == bias]
        if biased and len(biased) < len(pool):
            dropped = len(pool) - len(biased)
            pool = biased
            notes.append(f"restricted to country {bias} ({dropped} non-{bias} match(es) ignored)")
        elif not biased:
            # Nothing in the default country, but the name is real elsewhere (verified live:
            # "Springfield" => 5 distinct US cities). Do NOT pick one and do NOT pretend the
            # place does not exist: ask. GEO_COUNTRY_BIAS is ours, the user's intent may not be.
            labels = _place_labels(pool)
            if labels:
                return GeocodeResult(
                    status="ambiguous",
                    query=query_place,
                    candidates=[_to_candidate(r) for r in pool[:6]],
                    evidence_gap="out_of_default_country",
                    clarification=(
                        f"\u201c{query_place}\u201d doesn\u2019t match a place in India (my default scope is set by "
                        f"GEO_COUNTRY_BIAS={bias}). I found {', '.join(labels[:3])}. "
                        "Which location do you mean \u2014 and should I answer for it?"
                    ),
                )
            return GeocodeResult(
                status="unresolved",
                query=query_place,
                candidates=[_to_candidate(r) for r in pool[:6]],
                evidence_gap=f"no_match_in_country_{bias}",
                clarification=(
                    f"I couldn\u2019t verify \u201c{query_place}\u201d as a place in India, and I don\u2019t want "
                    "to guess weather for an unverified location. Which city or district did you mean?"
                ),
            )

    ctx = _norm_strict(context or "")
    if ctx and len(pool) > 1:
        narrowed = [
            r
            for r in pool
            if any(p and p in " ".join([_norm_strict(r.get("admin1")), _norm_strict(r.get("admin2"))]) for p in ctx.split())
        ]
        if narrowed:
            pool = narrowed
            notes.append("narrowed using your location hint")

    min_pop = config.AMBIGUITY_MIN_POP
    material = [r for r in pool if _is_material(r, min_pop)]
    ignored = [r for r in pool if r not in material]
    if material:
        if len(pool) > len(material):
            names = ", ".join(
                f"{r.get('name')} ({r.get('admin1') or r.get('country') or 'unknown area'})"
                for r in ignored[:4]
            )
            notes.append(
                f"same-named smaller places ignored for this query: {names}"
                + ("…" if len(ignored) > 4 else "")
            )
        pool = material

    if not pool:
        return GeocodeResult(status="unresolved", query=query_place, evidence_gap="no_geocode_match")

    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in pool:
        key = ((r.get("country_code") or "").upper(), _norm_strict(r.get("admin1")))
        groups.setdefault(key, []).append(r)

    candidates = [_to_candidate(r) for r in pool[:6]]

    if len(groups) > 1:
        labels = []
        for rows in list(groups.values())[:6]:
            row = max(rows, key=lambda r: r.get("population") or 0)
            where = ", ".join(x for x in [row.get("admin1"), row.get("country")] if x)
            labels.append(f"{row.get('name')}{f' ({where})' if where else ''}")
        return GeocodeResult(
            status="ambiguous",
            query=query_place,
            candidates=candidates,
            clarification=(
                f"I found multiple places matching \u201c{query_place}\u201d: {'; '.join(labels)}. "
                "Which location do you mean?"
            ),
            evidence_gap="ambiguous_location",
        )

    chosen = max(pool, key=lambda r: r.get("population") or 0)
    if len(pool) > 1:
        notes.append(f"{len(pool)} records describe this same area; quoting the largest settlement")
    return GeocodeResult(
        status="ok",
        query=query_place,
        location=_to_location(
            chosen, "; ".join(n for n in notes if n) or "exact name match, single distinct place"
        ),
        candidates=candidates,
    )


async def resolve(
    place: Optional[str],
    context: Optional[str] = None,
    country_bias: Optional[str] = None,
) -> GeocodeResult:
    place = clean_location(place or "")
    if not place:
        return GeocodeResult(
            status="unresolved",
            query=place or "",
            evidence_gap="missing_location",
            clarification="Which location should I check? Please give me a city or district name.",
        )

    # GeoNames-style gazetteer struggles with small towns -> also try the bare name
    # without "district/city" noise.
    attempts = [place]
    stripped = ADMIN_SUFFIXES.sub("", place).strip()
    if stripped and stripped != place:
        attempts.append(stripped)

    last_error: Optional[str] = None
    for attempt in attempts:
        try:
            results, source = await search(attempt)
        except UpstreamError as exc:
            last_error = exc.detail
            continue
        if results:
            res = disambiguate(attempt, results, context=context, country_bias=country_bias)
            if res.status != "unresolved":
                return res
            break  # real matches may still exist in the fallback source

    if config.GEO_FALLBACK.lower() == "nominatim" and last_error is None:
        fb = await search_nominatim(attempts[-1])
        if fb:
            res = disambiguate(attempts[-1], fb, context=context, country_bias=country_bias)
            if res.status == "ok" and res.location:
                res.location.resolution_note = (
                    (res.location.resolution_note + "; ")
                    + "coordinates from OpenStreetMap Nominatim (GeoNames had no match)"
                )
            if res.status != "unresolved":
                return res

    if last_error:
        return GeocodeResult(
            status="error", query=place, evidence_gap=f"geocoding_unavailable ({last_error})"
        )
    return GeocodeResult(
        status="unresolved",
        query=place,
        evidence_gap="no_geocode_match",
        clarification=(
            f"I couldn\u2019t verify \u201c{place}\u201d as a real location, so I don\u2019t want to guess weather for it."
        ),
    )
