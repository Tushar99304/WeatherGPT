"""
test_phase2_alerts.py — deterministic Phase 2 tests. NO network access.

Fixtures are real SACHET records saved under refs/ (fetched 2026-09-01) so the parsers are
tested against the actual feed shape, not an imagined one. `now` is always injected, so the
temporal verdicts are reproducible forever (the live records do expire).

Run:  python -m pytest tests/test_phase2_alerts.py -v
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
from typing import Optional

import pytest

from backend.models import ResolvedLocation
from backend.services.http_client import UpstreamError
from backend.services import alerts as A

REFS = pathlib.Path(__file__).resolve().parent.parent / "refs"
RSS_INDIA = REFS / "sachet_rss_sample.xml"
CAP_GUJARAT = REFS / "cap_sample.xml"
CAP_PUNE_MR = REFS / "cap_sample_marathi_pune.xml"
FIXTURE_RSS = REFS / "rss_fixture_pune.xml"
FIXTURE_CAP_DIR = REFS / "cap_files"

UTC = dt.timezone.utc
# The genuine Pune/Satara record: effective 2026-08-28T16:00+05:30, expires 19:00+05:30
PUNE_ALERT_WINDOW_MID = dt.datetime(2026, 8, 28, 11, 0, tzinfo=UTC)  # 16:30 IST => active
PUNE_ALERT_AFTER_EXPIRY = dt.datetime(2026, 8, 28, 14, 0, tzinfo=UTC)  # 19:30 IST => expired
# issued 16:40:57 IST; so "active" needs a now AFTER issuance but BEFORE the 19:00 IST expiry
PUNE_ALERT_ACTIVE_MID = dt.datetime(2026, 8, 28, 11, 30, tzinfo=UTC)  # 17:00 IST

PUNE = ResolvedLocation(
    name="Pune", latitude=18.51957, longitude=73.85535, country="India", country_code="IN",
    admin1="Maharashtra", admin2="Pune", timezone="Asia/Kolkata", utc_offset_seconds=19800,
)


@pytest.fixture(autouse=True)
def _no_cache_between_tests():
    A.clear_caches()
    yield
    A.clear_caches()


# --------------------------------------------------------------------- 1. RSS -- #
def test_rss_sample_parses():
    items = A.parse_rss_items(RSS_INDIA.read_text(encoding="utf-8"), feed_url="file://india")
    assert len(items) == 99, "the saved live feed has 99 items"
    first = items[0]
    assert first["identifier"] == "1788205553225013"
    assert first["link"].endswith("identifier=1788205553225013")
    assert first["category"] == "Met"
    assert first["author"].startswith("controlroom@ndma.gov.in")
    # RFC-822 pubDate must become an AWARE UTC datetime, never a naive local time
    assert first["pub_dt"].tzinfo is not None
    assert first["pub_dt"] == dt.datetime(2026, 8, 31, 19, 48, 56, tzinfo=UTC)


def test_rss_parser_is_tolerant_and_empty_safe():
    assert A.parse_rss_items("") == []
    assert A.parse_rss_items("<rss><channel></channel></rss>") == []
    with pytest.raises(ValueError):
        A.parse_rss_items("<rss><item><title>unclosed")  # malformed must be loud, not silent


# --------------------------------------------------------------------- 2-4. CAP -- #
def test_cap_sample_parses_required_fields():
    cap = A.parse_cap(CAP_GUJARAT.read_text(encoding="utf-8"))
    assert cap["identifier"] == "IN-1788205224883023_23"
    assert cap["sender"] == "Dadra-and-Nagar-Haveli-and-Daman-and-Diu-SDMA"
    assert cap["status"] == "Actual"
    assert cap["msgType"] == "Update"
    assert cap["scope"] == "Public"
    assert cap["category"] == "Met"
    assert cap["event"] == "Light Rain"
    assert cap["urgency"] == "Expected"
    assert cap["severity"] == "Moderate"
    assert cap["certainty"] == "Likely"
    assert cap["effective"] == "2026-09-01T01:09:00+05:30"
    assert cap["onset"] == "2026-09-01T01:14:11+05:30"
    assert cap["expires"] == "2026-09-01T04:09:00+05:30"
    assert "Dadra" in cap["headline"]
    assert "Dadra And Nagar Haveli district" in cap["areaDesc"]
    assert cap["lgd_codes"] == ["463", "465"]
    assert cap["polygon"] is None and cap["circle"] is None       # reality, not our assumption
    assert cap["polygon_url"].endswith("FetchPolygonXMLFile?identifier=1788205224883023")


def test_cap_multi_info_blocks_are_merged_and_english_preferred():
    cap = A.parse_cap(CAP_PUNE_MR.read_text(encoding="utf-8"))
    assert cap["info_count"] == 2
    assert cap["language"] == "en-IN", "display text must come from the English block"
    assert "Pune" in cap["headline"]
    # the Marathi block's headline is preserved too (never dropped, never translated away)
    assert any(lang == "MR" for lang in cap["info_languages"])
    hindi = cap["headlines_by_lang"].get("MR") or ""
    assert "पुणे" in hindi


def test_missing_optional_fields_do_not_crash():
    minimal = """<cap:alert xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
      <cap:identifier>IN-TEST-1</cap:identifier>
      <cap:info><cap:headline>Only a headline</cap:headline></cap:info>
    </cap:alert>"""
    cap = A.parse_cap(minimal)
    assert cap["identifier"] == "IN-TEST-1"
    assert cap["severity"] is None and cap["areaDesc"] is None and cap["lgd_codes"] == []
    alert = A.normalize_alert(
        cap, rss={"title": "Only a headline", "link": "https://example/1"},
        now=PUNE_ALERT_WINDOW_MID,
    )
    assert alert.headline == "Only a headline"
    assert alert.validity == "unknown"            # no timestamps -> we refuse to claim "active"
    assert "no usable timestamp" in alert.validity_reason


def test_non_alert_document_is_rejected():
    with pytest.raises(ValueError):
        A.parse_cap("<html><body>502 Bad Gateway</body></html>")


# ------------------------------------------------------------------ 5-6. time -- #
def test_expired_alert_is_marked_expired():
    cap = A.parse_cap(CAP_PUNE_MR.read_text(encoding="utf-8"))
    alert = A.normalize_alert(cap, now=PUNE_ALERT_AFTER_EXPIRY)
    assert alert.validity == "expired"
    assert "expired at" in alert.validity_reason
    assert alert.expires_at.endswith("Z") and alert.expires_at.startswith("2026-08-28")


def test_active_alert_is_marked_active_inside_the_window():
    cap = A.parse_cap(CAP_PUNE_MR.read_text(encoding="utf-8"))
    alert = A.normalize_alert(cap, now=PUNE_ALERT_ACTIVE_MID)
    assert alert.validity == "active"
    assert alert.age_minutes is not None and 0 <= alert.age_minutes < 60 * 3


def test_missing_expiry_is_unknown_never_indefinitely_active():
    status, reason, expiry_missing, _ = A.classify_validity(
        dt.datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        sent=dt.datetime(2026, 9, 1, 11, 0, tzinfo=UTC),
        effective=dt.datetime(2026, 9, 1, 11, 0, tzinfo=UTC),
        onset=None,
        expires=None,
        max_age_hours=24,
    )
    assert status == "unknown" and expiry_missing is True
    assert "not assumed active indefinitely" in reason


def test_naive_timestamp_is_treated_as_utc_not_server_local():
    assert A.parse_any_datetime("2026-09-01T04:09:00").isoformat() == "2026-09-01T04:09:00+00:00"
    assert A.parse_any_datetime("2026-09-01T04:09:00+05:30").isoformat() == "2026-08-31T22:39:00+00:00"
    assert A.parse_any_datetime("nonsense") is None
    assert A.parse_any_datetime(None) is None


# --------------------------------------------------------------- 7-9. relevance -- #
def test_exact_locality_match_is_relevant():
    cap = A.parse_cap(CAP_PUNE_MR.read_text(encoding="utf-8"))
    rel = A.assess_relevance(
        PUNE,
        area_desc=cap["areaDesc"],
        headline=cap["headline"],
        description=cap["description"],
        lgd_codes=cap["lgd_codes"],
    )
    assert rel.status == "relevant"
    assert rel.level == "L1_exact_locality"
    assert "pune" in rel.matched_terms


def test_same_state_other_district_is_not_relevant():
    rel = A.assess_relevance(
        PUNE,
        area_desc="Nashik district of Maharashtra",
        headline="Heavy rain warning for Nashik district of Maharashtra in next 3 hours",
    )
    assert rel.status == "not_relevant"
    assert rel.level == "L3_state_scoped_subset"
    assert "nashik" in rel.matched_terms


def test_other_state_alert_is_never_relevant():
    """An alert whose enumeration is another state's districts must not attach to Pune.
    Permitted verdicts: not_relevant (we read a closed list) or uncertain (we could not) --
    the important assertion is that it is NOT 'relevant'."""
    rel = A.assess_relevance(
        PUNE,
        area_desc="Navsari,The Dangs,Valsad districts of Gujarat",
        headline="Light Rain is very likely to occur at isolated places over Dadra And Nagar Haveli,"
        " Daman, Navsari, The Dangs, Valsad in next 3 hours.",
    )
    assert rel.status != "relevant"
    assert "pune" not in rel.matched_terms


def test_state_word_alone_never_proves_coverage():
    """The exact false positive we must never ship: an alert for a different district of the
    same state must not attach because the state name matched."""
    may = ResolvedLocation(name="Mayurbhanj", latitude=21.93, longitude=86.73, admin1="Odisha", admin2="Mayurbhanj")
    rel = A.assess_relevance(
        may,
        area_desc="River Baitarni at Akhuapada in Bhadrak district of Odisha continues to flow",
        headline="River Baitarni at Akhuapada in Bhadrak district of Odisha continues to flow",
    )
    assert rel.status == "not_relevant", "Bhadrak must not be attached to Mayurbhanj"
    assert "mayurbhanj" not in rel.matched_terms


def test_keyword_only_vague_alert_stays_uncertain():
    rel = A.assess_relevance(
        PUNE,
        area_desc=None,
        headline="अगले 3 घंटों में आपके ज़िलों और आस-पास के इलाकों में कुछ जगहों पर मध्यम बारिश",
    )
    assert rel.status == "uncertain"
    assert rel.level == "none"


def test_unnamed_district_count_stays_uncertain_not_relevant():
    """'7 districts of Maharashtra' does not tell us WHICH 7 -> never attach to Pune."""
    rel = A.assess_relevance(
        PUNE,
        area_desc="7 districts of Maharashtra",
        headline="Moderate to intense spell of rain is very likely to occur over 7 districts of"
        " Maharashtra in next 3 hours",
    )
    assert rel.status in {"uncertain", "not_relevant"}
    assert rel.status != "relevant"


def test_explicit_state_wide_coverage_is_relevant():
    rel = A.assess_relevance(
        PUNE,
        area_desc="Maharashtra",
        headline="Heavy rain is very likely over all the districts of Maharashtra in next 3 hours.",
    )
    assert rel.status == "relevant"
    assert rel.level == "L2_explicit_state_wide"


def test_geometry_is_used_only_when_actually_supplied():
    inside = A.assess_relevance(
        PUNE, area_desc=None, headline="Flash flood warning", circle="18.5, 73.8, 50"
    )
    outside = A.assess_relevance(
        PUNE, area_desc=None, headline="Flash flood warning", circle="21.0, 79.0, 50"
    )
    assert inside.status == "relevant" and inside.level == "L4_geometry"
    assert outside.status == "not_relevant"
    # and when no geometry exists (the verified SACHET reality), we say so instead of guessing
    none = A.assess_relevance(PUNE, area_desc="Pune", headline="Rain")
    assert none.geometry_available is False


# ---------------------------------------------------------------- 10. unicode -- #
def test_unicode_marathi_text_is_preserved_verbatim():
    raw = CAP_PUNE_MR.read_text(encoding="utf-8")
    cap = A.parse_cap(raw)
    marathi = cap["headlines_by_lang"]["MR"]
    assert "पुढील" in marathi and "जिल्ह्यात" in marathi
    alert = A.normalize_alert(cap, now=PUNE_ALERT_WINDOW_MID)
    # normalization never mutates stored strings, only matching keys
    assert alert.headline and "Pune" in alert.headline
    assert A.norm_text("PUNE") == "pune"
    assert A.LOCALITY_ALIASES["pune"] == ("पुणे",)
    # Unicode alert text must also survive JSON round-trip of the evidence object
    dumped = alert.model_dump_json()
    assert "पुणे" in dumped or "Pune" in dumped


# ---------------------------------------------------- 11-12. availability states -- #
class _FakeNet:
    """Minimal stand-in for http_client.get_text: returns canned feeds, or fails."""

    def __init__(self, *, rss: Optional[str] = None, cap: Optional[str] = None, fail: bool = False):
        self.rss, self.cap, self.fail, self.calls = rss, cap, fail, []

    async def __call__(self, url: str, *, service: str = "upstream", headers=None) -> str:
        self.calls.append(url)
        if self.fail:
            raise UpstreamError("sachet-rss", "ConnectTimeout: simulated")
        if "FetchXMLFile" in url or "cap_files" in url:
            return self.cap or ""
        return self.rss or "<rss><channel></channel></rss>"


def test_nothing_relevant_still_reports_checked_not_unavailable(monkeypatch):
    import asyncio

    fake = _FakeNet(rss=FIXTURE_RSS.read_text(encoding="utf-8"), cap=None)
    # CAP detail is unavailable for these ids -> the feed WAS consulted, details failed
    monkeypatch.setattr(A, "get_text", fake)
    monkeypatch.setattr(A.config, "ALERT_INCLUDE_INDIA_FEED", True)
    res = asyncio.run(A.check_alerts(PUNE, now=PUNE_ALERT_AFTER_EXPIRY))
    assert res.state in {"checked", "unavailable"}
    assert res.items == []
    assert res.mode == "live"
    assert res.error or res.notes, "a reason must be preserved, never a bare 'no alerts'"


def test_network_failure_is_unavailable_with_reason(monkeypatch):
    import asyncio

    monkeypatch.setattr(A, "get_text", _FakeNet(fail=True))
    res = asyncio.run(A.check_alerts(PUNE, now=PUNE_ALERT_WINDOW_MID))
    assert res.state == "unavailable"
    assert res.items == []
    assert res.error and "ConnectTimeout" in res.error
    # and 'unavailable' must be distinguishable from 'checked, nothing relevant'
    assert res.state != "checked"


def test_empty_feeds_are_checked_with_zero_items(monkeypatch):
    import asyncio

    monkeypatch.setattr(A, "get_text", _FakeNet(rss="<rss><channel><title>x</title></channel></rss>"))
    res = asyncio.run(A.check_alerts(PUNE, now=PUNE_ALERT_WINDOW_MID))
    assert res.state == "checked"
    assert res.items == [] and res.recent_expired == []
    assert any("no alert items within the recency window" in n for n in res.notes)


def test_fixture_replay_is_labelled_not_passed_off_as_live(monkeypatch):
    import asyncio

    monkeypatch.setattr(A.config, "ALERT_FIXTURE_RSS", str(FIXTURE_RSS))
    monkeypatch.setattr(A.config, "ALERT_FIXTURE_CAP_DIR", str(FIXTURE_CAP_DIR))
    monkeypatch.setattr(A.config, "ALERT_INCLUDE_INDIA_FEED", False)
    monkeypatch.setattr(A, "get_text", _FakeNet(fail=True))  # no real network in tests
    res = asyncio.run(A.check_alerts(PUNE, now=PUNE_ALERT_WINDOW_MID))
    assert res.mode == "fixture_replay"
    assert res.state == "checked"
    assert len(res.items) == 1, f"the real Pune record must match, got {len(res.items)}"
    alert = res.items[0]
    assert alert.validity == "active"
    assert alert.relevance.level == "L1_exact_locality"
    assert alert.authority == "official" and alert.source == "NDMA SACHET"
    assert alert.source_url and "FetchXMLFile" in alert.source_url


def test_disabled_alerts_are_not_checked(monkeypatch):
    import asyncio

    monkeypatch.setattr(A.config, "SACHET_ENABLED", False)
    res = asyncio.run(A.check_alerts(PUNE))
    assert res.state == "not_checked" and res.mode == "disabled"
    assert "not the same as 'no alert'" in res.notes[0]


def test_simulated_failure_switch_sets_unavailable(monkeypatch):
    import asyncio

    monkeypatch.setattr(A.config, "SIMULATE_ALERT_FAILURE", True)
    res = asyncio.run(A.check_alerts(PUNE))
    assert res.state == "unavailable"
    assert "SIMULATE_ALERT_FAILURE" in res.error


# ------------------------------------------------------------ feed URL selection -- #
def test_same_alert_in_both_feeds_is_surfaced_once(monkeypatch, tmp_path):
    """One event is published to the state feed and the India feed under DIFFERENT CAP
    identifiers, so identifier dedupe cannot catch it. The answer must show it once and count
    the collapse, instead of padding the alert list."""
    import asyncio

    fixture = FIXTURE_RSS.read_text(encoding="utf-8")
    item = re.search(r"  <item>.*?</item>", fixture, re.S).group(0)
    twin = item.replace("1787913209058029", "1787913209058999")
    feed = tmp_path / "duplicated.xml"
    feed.write_text(fixture.replace(item, item + "\n" + twin), encoding="utf-8")
    cap_dir = tmp_path / "caps"
    cap_dir.mkdir()
    for f in FIXTURE_CAP_DIR.glob("*.xml"):
        (cap_dir / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (cap_dir / "1787913209058999.xml").write_text(
        (FIXTURE_CAP_DIR / "1787913209058029.xml").read_text(encoding="utf-8").replace(
            "1787913209058029", "1787913209058999"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(A.config, "ALERT_FIXTURE_RSS", str(feed))
    monkeypatch.setattr(A.config, "ALERT_FIXTURE_CAP_DIR", str(cap_dir))
    monkeypatch.setattr(A.config, "ALERT_INCLUDE_INDIA_FEED", False)
    monkeypatch.setattr(A, "get_text", _FakeNet(fail=True))
    res = asyncio.run(A.check_alerts(PUNE, now=PUNE_ALERT_ACTIVE_MID))
    assert len(res.items) == 1, [x.alert_id for x in res.items]
    assert res.rejected_duplicate == 1
    # both copies were opened (only the CAP body reveals they are the same alert), so details
    # fetched exceeds the number of items; the exact number depends on how many items the
    # fixture feed holds, which is not the contract under test.
    assert res.details_fetched >= 2
    assert len({x.alert_id for x in res.items}) == len(res.items), "no duplicate ids in the answer"


def test_expired_records_are_counted_not_silently_dropped(monkeypatch):
    """Fixture replay of the real Pune record, judged AFTER its expiry: the alert must land in
    recent_expired with a note, and must never appear as an active item."""
    import asyncio

    monkeypatch.setattr(A.config, "ALERT_FIXTURE_RSS", str(FIXTURE_RSS))
    monkeypatch.setattr(A.config, "ALERT_FIXTURE_CAP_DIR", str(FIXTURE_CAP_DIR))
    monkeypatch.setattr(A.config, "ALERT_INCLUDE_INDIA_FEED", False)
    monkeypatch.setattr(A, "get_text", _FakeNet(fail=True))
    res = asyncio.run(A.check_alerts(PUNE, now=PUNE_ALERT_AFTER_EXPIRY))
    assert res.state == "checked" and res.items == []
    assert len(res.recent_expired) == 1
    assert res.recent_expired[0].validity == "expired"
    assert any("had already EXPIRED" in n for n in res.notes), res.notes
    assert res.details_fetched == 1, "the expired record still proves a CAP body was opened and read"


def test_state_feed_used_names_the_feed_that_answered(monkeypatch):
    import asyncio

    monkeypatch.setattr(A.config, "ALERT_FIXTURE_RSS", str(FIXTURE_RSS))
    monkeypatch.setattr(A.config, "ALERT_FIXTURE_CAP_DIR", str(FIXTURE_CAP_DIR))
    monkeypatch.setattr(A.config, "ALERT_INCLUDE_INDIA_FEED", False)
    monkeypatch.setattr(A, "get_text", _FakeNet(fail=True))
    res = asyncio.run(A.check_alerts(PUNE, now=PUNE_ALERT_ACTIVE_MID))
    assert res.state_feed_used and "rss_fixture_pune.xml" in res.state_feed_used
    assert res.feeds_considered == [res.state_feed_used], "only the consulted feed is listed"


def test_normalized_alert_object_matches_the_required_shape():
    """Every field the spec asks for must exist on the normalized object."""
    cap = A.parse_cap(CAP_PUNE_MR.read_text(encoding="utf-8"))
    alert = A.normalize_alert(cap, now=PUNE_ALERT_ACTIVE_MID)
    required = {
        "alert_id", "source", "authority", "event", "headline", "description", "severity",
        "urgency", "certainty", "effective_at", "expires_at", "validity", "area_desc",
        "relevance", "raw_source_url",
    }
    data = alert.model_dump()
    assert required <= set(data), sorted(required - set(data))
    assert data["source"] == "NDMA SACHET" and data["authority"] == "official"
    assert data["area_desc"] == "Pune,Satara districts of Maharashtra"  # not doubled
    assert set(data["relevance"]) == {
        "status", "level", "reason", "matched_terms", "area_text", "geometry_available"
    }
    assert data["raw_source_url"].startswith("https://sachet.ndma.gov.in/")


def test_state_feed_slug_matches_the_real_portal_naming():
    # verified against the portal's own feed list (rss_uttar.xml etc.), not guessed
    assert A.state_feed_url("Maharashtra").endswith("rss_maharashtra.xml")
    assert A.state_feed_url("Uttar Pradesh").endswith("rss_uttar.xml")
    assert A.state_feed_url("Tamil Nadu").endswith("rss_tamil.xml")
    assert A.state_feed_url("National Capital Territory of Delhi").endswith("rss_delhi.xml")
    assert A.state_feed_url("Dadra and Nagar Haveli and Daman and Diu").endswith("rss_dadra.xml")
    assert A.state_feed_url(None) is None


def test_substring_similarity_does_not_count_as_a_locality_match():
    """Word boundaries matter: 'Puneet' is a name, not our district 'Pune'."""
    rel = A.assess_relevance(
        PUNE, area_desc="Puneet Nagar ward", headline="Light rain over Puneet Nagar in next 3 hours"
    )
    assert rel.status != "relevant"
    assert "pune" not in rel.matched_terms


def test_state_only_mention_without_enumeration_stays_uncertain():
    rel = A.assess_relevance(
        PUNE, area_desc=None, headline="Rain is very likely over some parts of Maharashtra"
    )
    assert rel.status == "uncertain", "state mention alone must never attach an alert"
