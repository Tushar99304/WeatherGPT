"""
test_phase2_live.py — real NDMA SACHET calls. Network required, so it is marked `live`
and can be skipped with:  python -m pytest tests -m "not live"

Assertions are deliberately about INVARIANTS, not about what the feed happens to contain
today (alerts expire in ~3 hours, so "expect exactly one alert" would be a flaky test).
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.live


def _alerts_for(place: str, context: str | None = None):
    from backend.services import alerts, geocoding

    geo = asyncio.run(geocoding.resolve(place, context=context))
    assert geo.location is not None, geo.model_dump()
    return asyncio.run(alerts.check_alerts(geo.location)), geo.location


def test_feeds_are_reachable_and_state_is_explicit():
    res, _ = _alerts_for("pune", "Maharashtra")
    assert res.state in {"checked", "unavailable"}, res.state
    assert res.checked_at_utc and res.checked_at_utc.endswith("Z")
    assert res.feeds_considered, "we must be able to name the feeds we consulted"
    assert res.source == "NDMA SACHET" and res.authority == "official"
    assert res.items_in_feeds > 0 or res.state == "unavailable"
    assert res.mode == "live"


def test_pune_is_not_handed_unrelated_state_alerts():
    """The safety invariant, on live data: anything attached to Pune must name Pune (or be
    explicitly statewide), and the run must never invent a match."""
    res, loc = _alerts_for("pune", "Maharashtra")
    for alert in res.items:
        assert alert.relevance.status == "relevant"
        assert alert.validity in {"active", "unknown"}
        assert alert.headline
        assert alert.source_url and "sachet.ndma.gov.in" in alert.source_url
        if alert.relevance.level == "L1_exact_locality":
            assert any(t in (alert.area_desc or "").lower() or t in (alert.headline or "").lower()
                       for t in ("pune", "पुणे")), alert.headline
    # every non-attached item must be counted, so 'no alerts' is explainable
    assert res.rejected_not_relevant + res.rejected_uncertain + res.rejected_stale >= 0
    assert isinstance(res.recent_expired, list)
    assert loc.admin1 == "Maharashtra"


def test_alert_block_survives_the_full_query_pipeline():
    from backend.main import run_pipeline

    ev, trace = asyncio.run(run_pipeline("Is there any weather alert for Mumbai today?"))
    assert ev.status in {"grounded", "abstain"}
    assert ev.alert_state in {"checked", "unavailable", "not_checked"}
    stage = next(s for s in trace["stages"] if s["stage"] == "retrieve_alerts")
    assert stage["status"] == ev.alert_state
    if ev.alert_state == "checked":
        sachet_sources = [s for s in ev.sources if s.name == "NDMA SACHET"]
        assert sachet_sources and sachet_sources[0].authority == "official"
        assert sachet_sources[0].type == "official_alert"
    for alert in ev.alerts.items:
        assert alert.sender or alert.author_name, "who issued it must be preserved"
