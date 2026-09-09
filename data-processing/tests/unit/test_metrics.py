"""Unit tests for the metrics and gauge interface (task 1.3).

- Req 29.6: counters for records ingested per transport, records quarantined per
  rejection reason, Readings per Quality_Flag, sites per fault category, dedup
  conflicts, forecast degradations, authentication rejections per category, and
  responses served per route.
- Req 29.7: the age of the most recent accepted Reading per site is a gauge, so a
  silent upstream is detectable.
- Req 29.10: metric transport and alarm definition are a DEPLOYMENT concern; the
  service exposes one internal interface a deployment adapter publishes.
"""

from __future__ import annotations

import datetime as dt

from aqm_ingestion.observability.metrics import MetricsRegistry

_T0 = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def test_counters_start_at_zero() -> None:
    metrics = MetricsRegistry()
    assert metrics.snapshot().counters == {}


def test_records_counted_per_transport() -> None:
    metrics = MetricsRegistry()
    metrics.record_ingested(transport="mqtt")
    metrics.record_ingested(transport="mqtt")
    metrics.record_ingested(transport="http_poll")
    counters = metrics.snapshot().counters
    assert counters["records_ingested"]["mqtt"] == 2
    assert counters["records_ingested"]["http_poll"] == 1


def test_quarantines_counted_per_reason() -> None:
    metrics = MetricsRegistry()
    metrics.record_quarantined(reason="unparseable")
    metrics.record_quarantined(reason="out_of_range")
    metrics.record_quarantined(reason="unparseable")
    counters = metrics.snapshot().counters
    assert counters["records_quarantined"] == {"unparseable": 2, "out_of_range": 1}


def test_readings_counted_per_quality_flag() -> None:
    metrics = MetricsRegistry()
    metrics.record_reading(quality_flag="valid")
    metrics.record_reading(quality_flag="suspect")
    assert metrics.snapshot().counters["readings"] == {"valid": 1, "suspect": 1}


def test_sites_counted_per_fault_category() -> None:
    metrics = MetricsRegistry()
    metrics.record_site_fault(category="stuck")
    assert metrics.snapshot().counters["site_faults"] == {"stuck": 1}


def test_dedup_conflicts_counted() -> None:
    metrics = MetricsRegistry()
    metrics.record_dedup_conflict()
    metrics.record_dedup_conflict()
    assert metrics.snapshot().counters["dedup_conflicts"][""] == 2


def test_forecast_degradations_counted() -> None:
    metrics = MetricsRegistry()
    metrics.record_forecast_degradation()
    assert metrics.snapshot().counters["forecast_degradations"][""] == 1


def test_auth_rejections_counted_per_category() -> None:
    metrics = MetricsRegistry()
    metrics.record_auth_rejection(category="expired")
    metrics.record_auth_rejection(category="missing")
    metrics.record_auth_rejection(category="expired")
    assert metrics.snapshot().counters["auth_rejections"] == {
        "expired": 2, "missing": 1
    }


def test_responses_counted_per_route() -> None:
    metrics = MetricsRegistry()
    metrics.record_response(route="/air-quality")
    assert metrics.snapshot().counters["responses"] == {"/air-quality": 1}


# --- Req 29.7 freshness gauge ---------------------------------------------

def test_gauge_reports_age_of_most_recent_reading_per_site() -> None:
    metrics = MetricsRegistry()
    metrics.observe_reading_accepted(site_code="CB0001", interval_start=_T0)
    ages = metrics.snapshot(now=_T0 + dt.timedelta(minutes=30)).reading_age_seconds
    assert ages["CB0001"] == 1800


def test_gauge_keeps_the_most_recent_per_site() -> None:
    metrics = MetricsRegistry()
    metrics.observe_reading_accepted(site_code="CB0001", interval_start=_T0)
    metrics.observe_reading_accepted(
        site_code="CB0001", interval_start=_T0 + dt.timedelta(hours=1)
    )
    ages = metrics.snapshot(now=_T0 + dt.timedelta(hours=1)).reading_age_seconds
    assert ages["CB0001"] == 0  # the newer interval wins


def test_gauge_ignores_an_older_arrival() -> None:
    metrics = MetricsRegistry()
    metrics.observe_reading_accepted(
        site_code="CB0001", interval_start=_T0 + dt.timedelta(hours=1)
    )
    metrics.observe_reading_accepted(site_code="CB0001", interval_start=_T0)
    ages = metrics.snapshot(now=_T0 + dt.timedelta(hours=1)).reading_age_seconds
    assert ages["CB0001"] == 0  # a late older record does not age the site


def test_gauge_tracks_sites_independently() -> None:
    metrics = MetricsRegistry()
    metrics.observe_reading_accepted(site_code="CB0001", interval_start=_T0)
    metrics.observe_reading_accepted(
        site_code="CB0002", interval_start=_T0 - dt.timedelta(hours=2)
    )
    ages = metrics.snapshot(now=_T0).reading_age_seconds
    assert ages["CB0001"] == 0
    assert ages["CB0002"] == 7200  # a silent upstream is visible per site


def test_snapshot_without_now_omits_ages() -> None:
    # §2: the age needs a time source; the registry never calls datetime.now()
    metrics = MetricsRegistry()
    metrics.observe_reading_accepted(site_code="CB0001", interval_start=_T0)
    assert metrics.snapshot().reading_age_seconds == {}


def test_snapshot_is_a_copy() -> None:
    metrics = MetricsRegistry()
    metrics.record_dedup_conflict()
    snapshot = metrics.snapshot()
    metrics.record_dedup_conflict()
    # a published snapshot must not change under the publisher's feet
    assert snapshot.counters["dedup_conflicts"][""] == 1
