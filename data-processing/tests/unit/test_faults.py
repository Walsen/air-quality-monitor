"""Unit tests for the Fault_Detector (task 12.2).

- 13.4: stuck — the configured consecutive intervals (default 6) equal within the
  configured tolerance (default 0.01 µg/m³).
- 13.5: dropout — the configured consecutive expected intervals (default 3) with no
  accepted Reading.
- 13.6: drift — the site's mean over the comparison window (default 24 h) differs from the
  MEDIAN of the same statistic across peers by more than the configured multiple
  (default 3.0) of that median, with at least the minimum peers (default 3) in radius
  (default 10 km).
- 13.7: too few peers — no drift flag, one `debug` event naming the site and peer count.
- 13.8: a raise or a clear each log one structured event.
- 13.9: cleared after the configured consecutive clean intervals (default 3).
- 13.11: fault counts are exposed as metrics.

The detector is pure over a supplied history (§2): it never reads a store or a clock, which
is what lets a stuck sensor be simulated exactly rather than approximately.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

import pytest

from aqm_ingestion.domain.faults import (
    DEFAULT_FAULT_THRESHOLDS,
    FaultCategory,
    FaultThresholds,
    PeerSeries,
    SiteHistory,
    detect_faults,
)
from aqm_ingestion.observability.logging import configure_logging
from aqm_ingestion.observability.metrics import MetricsRegistry


def _history(
    reported: Sequence[float | None] | None = None,
    corrected: Sequence[float | None] | None = None,
) -> SiteHistory:
    values: Sequence[float | None] = reported if reported is not None else [10.0] * 6
    return SiteHistory(
        site_code="CB0001",
        species="PM25",
        reported=tuple(values),
        corrected=tuple(corrected if corrected is not None else values),
    )


def _events(captured: str) -> list[dict[str, Any]]:
    return [
        cast("dict[str, Any]", json.loads(line))
        for line in captured.strip().splitlines()
        if line
    ]


# --- defaults ------------------------------------------------------------

def test_thresholds_default_to_the_documented_values() -> None:
    thresholds = DEFAULT_FAULT_THRESHOLDS
    assert thresholds.stuck_intervals == 6
    assert thresholds.stuck_tolerance == 0.01
    assert thresholds.dropout_intervals == 3
    assert thresholds.drift_window_hours == 24
    assert thresholds.drift_min_peers == 3
    assert thresholds.drift_radius_km == 10.0
    assert thresholds.drift_multiple == 3.0
    assert thresholds.clear_intervals == 3


# --- Req 13.4 stuck ------------------------------------------------------

def test_six_identical_intervals_are_stuck() -> None:
    assert FaultCategory.STUCK in detect_faults(_history([7.0] * 6)).categories


def test_five_identical_intervals_are_not_yet_stuck() -> None:
    assert FaultCategory.STUCK not in detect_faults(_history([7.0] * 5)).categories


def test_values_within_tolerance_count_as_equal() -> None:
    # a real sensor's stuck output wobbles in the last digit; Req 13.4's tolerance is
    # what makes the check useful rather than defeated by noise
    nearly = [7.0, 7.005, 6.995, 7.002, 7.0, 6.999]
    assert FaultCategory.STUCK in detect_faults(_history(nearly)).categories


def test_values_outside_tolerance_are_not_stuck() -> None:
    varying = [7.0, 7.5, 8.0, 7.2, 6.5, 7.1]
    assert FaultCategory.STUCK not in detect_faults(_history(varying)).categories


def test_stuck_looks_at_the_most_recent_run_only() -> None:
    # index 0 is the most recent, so the moving values are CURRENT and the flat run is
    # older: a sensor that has started moving again must not stay flagged
    history = _history([1.0, 2.0, 3.0, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0])
    assert FaultCategory.STUCK not in detect_faults(history).categories


def test_a_gap_breaks_a_stuck_run() -> None:
    # an absent interval is not an equal value; treating it as one would invent a run
    history = _history([7.0, 7.0, None, 7.0, 7.0, 7.0, 7.0])
    assert FaultCategory.STUCK not in detect_faults(history).categories


def test_the_stuck_tolerance_is_configurable() -> None:
    thresholds = FaultThresholds(stuck_tolerance=1.0)
    wobbly = [7.0, 7.5, 6.8, 7.2, 7.4, 6.9]
    result = detect_faults(_history(wobbly), thresholds=thresholds)
    assert FaultCategory.STUCK in result.categories


def test_stuck_uses_the_reported_value_not_the_corrected_one() -> None:
    # a stuck SENSOR repeats its raw output; the corrected value varies with humidity, so
    # judging on it would miss the fault this check exists to find
    stuck_reported = [7.0] * 6
    varying_corrected = [3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    history = _history(reported=stuck_reported, corrected=varying_corrected)
    assert FaultCategory.STUCK in detect_faults(history).categories


# --- Req 13.5 dropout ----------------------------------------------------

def test_three_consecutive_missing_intervals_are_a_dropout() -> None:
    # index 0 is the MOST RECENT interval, so a current dropout puts the gaps FIRST.
    # Gaps at the end of the list are an old outage the site has since recovered from.
    history = _history([None, None, None, 5.0, 5.1, 5.2])
    assert FaultCategory.DROPOUT in detect_faults(history).categories


def test_two_recent_missing_intervals_are_not_a_dropout() -> None:
    history = _history([None, None, 5.0, 5.1, 5.2, 5.3])
    assert FaultCategory.DROPOUT not in detect_faults(history).categories


def test_an_old_outage_is_not_a_current_dropout() -> None:
    # the site is reporting again now, so the fault is history rather than present
    history = _history([5.0, 5.1, 5.2, None, None, None])
    assert FaultCategory.DROPOUT not in detect_faults(history).categories


def test_non_consecutive_gaps_are_not_a_dropout() -> None:
    history = _history([None, 5.0, None, 5.1, None, 5.2])
    assert FaultCategory.DROPOUT not in detect_faults(history).categories


def test_the_dropout_threshold_is_configurable() -> None:
    thresholds = FaultThresholds(dropout_intervals=2)
    history = _history([None, None, 5.0, 5.1, 5.2, 5.3])
    assert FaultCategory.DROPOUT in detect_faults(history, thresholds=thresholds).categories


def test_an_entirely_absent_history_is_a_dropout() -> None:
    assert FaultCategory.DROPOUT in detect_faults(_history([None] * 6)).categories


# --- Req 13.6 drift ------------------------------------------------------

def _peers(*means: float) -> tuple[PeerSeries, ...]:
    return tuple(
        PeerSeries(site_code=f"PEER{index}", mean_corrected=mean, distance_km=5.0)
        for index, mean in enumerate(means)
    )


def test_a_site_far_from_the_peer_median_drifts() -> None:
    # peers median 10, site mean 100: 90 away, far beyond 3.0 x 10
    history = _history(corrected=[100.0] * 6)
    result = detect_faults(history, peers=_peers(9.0, 10.0, 11.0))
    assert FaultCategory.DRIFT in result.categories


def test_a_site_near_the_peer_median_does_not_drift() -> None:
    history = _history(corrected=[11.0] * 6)
    result = detect_faults(history, peers=_peers(9.0, 10.0, 11.0))
    assert FaultCategory.DRIFT not in result.categories


def test_drift_compares_against_the_median_not_the_mean() -> None:
    # one wild peer would drag a MEAN far up; the median is robust to it, which is why
    # Req 13.6 names the median
    history = _history(corrected=[40.0] * 6)
    result = detect_faults(history, peers=_peers(10.0, 10.0, 10.0, 1_000.0))
    # median is 10, so 40 is 30 away = 3x the median: just over the threshold
    assert FaultCategory.DRIFT in result.categories


def test_the_drift_multiple_is_configurable() -> None:
    history = _history(corrected=[20.0] * 6)
    thresholds = FaultThresholds(drift_multiple=0.5)
    result = detect_faults(history, peers=_peers(9.0, 10.0, 11.0), thresholds=thresholds)
    assert FaultCategory.DRIFT in result.categories


def test_peers_beyond_the_radius_are_ignored() -> None:
    far = tuple(
        PeerSeries(site_code=f"FAR{i}", mean_corrected=10.0, distance_km=50.0)
        for i in range(4)
    )
    history = _history(corrected=[100.0] * 6)
    result = detect_faults(history, peers=far)
    # every peer is out of radius, so drift is not evaluated at all
    assert FaultCategory.DRIFT not in result.categories


# --- Req 13.7 too few peers ---------------------------------------------

def test_too_few_peers_evaluates_no_drift() -> None:
    history = _history(corrected=[100.0] * 6)
    result = detect_faults(history, peers=_peers(10.0, 10.0))
    assert FaultCategory.DRIFT not in result.categories


def test_too_few_peers_logs_one_debug_event(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("debug")
    detect_faults(_history(corrected=[100.0] * 6), peers=_peers(10.0, 10.0))
    debugs = [e for e in _events(capsys.readouterr().out) if e["level"] == "debug"]
    assert len(debugs) == 1
    assert debugs[0]["SiteCode"] == "CB0001"
    assert debugs[0]["peer_count"] == 2


def test_enough_peers_logs_no_debug_event(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("debug")
    detect_faults(_history(corrected=[11.0] * 6), peers=_peers(9.0, 10.0, 11.0))
    debugs = [
        e
        for e in _events(capsys.readouterr().out)
        if e["level"] == "debug" and e.get("event") == "drift_skipped_too_few_peers"
    ]
    assert debugs == []


def test_a_zero_peer_median_does_not_divide_by_zero() -> None:
    # every peer reading 0 makes the threshold 0, so any positive mean drifts — but it
    # must not raise
    history = _history(corrected=[5.0] * 6)
    result = detect_faults(history, peers=_peers(0.0, 0.0, 0.0))
    assert FaultCategory.DRIFT in result.categories


# --- Req 13.8 raise and clear logging -----------------------------------

def test_raising_a_flag_logs_one_event(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    detect_faults(_history([7.0] * 6), previously_flagged=frozenset())
    events = [
        e for e in _events(capsys.readouterr().out) if e.get("event") == "fault_flag"
    ]
    assert len(events) == 1
    assert events[0]["SiteCode"] == "CB0001"
    assert events[0]["Species"] == "PM25"
    assert events[0]["category"] == FaultCategory.STUCK.value
    assert events[0]["transition"] == "raised"


def test_an_already_flagged_fault_is_not_re_logged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Req 13.8 logs a TRANSITION; re-logging every interval would bury the change
    configure_logging("info")
    detect_faults(
        _history([7.0] * 6), previously_flagged=frozenset({FaultCategory.STUCK})
    )
    events = [
        e for e in _events(capsys.readouterr().out) if e.get("event") == "fault_flag"
    ]
    assert events == []


def test_clearing_a_flag_logs_one_event(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    clean = _history([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    detect_faults(clean, previously_flagged=frozenset({FaultCategory.STUCK}))
    events = [
        e for e in _events(capsys.readouterr().out) if e.get("event") == "fault_flag"
    ]
    assert len(events) == 1
    assert events[0]["transition"] == "cleared"


# --- Req 13.9 clearing --------------------------------------------------

def test_three_clean_intervals_clear_a_flag() -> None:
    clean = _history([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    result = detect_faults(clean, previously_flagged=frozenset({FaultCategory.STUCK}))
    assert FaultCategory.STUCK not in result.categories
    assert FaultCategory.STUCK in result.cleared


def test_too_few_clean_intervals_hold_the_flag() -> None:
    # only two intervals of history, so the clear bar is not met and the flag persists —
    # clearing early would declare a sensor healthy on insufficient evidence
    result = detect_faults(
        _history([1.0, 2.0]), previously_flagged=frozenset({FaultCategory.STUCK})
    )
    assert FaultCategory.STUCK in result.categories


def test_the_clear_threshold_is_configurable() -> None:
    thresholds = FaultThresholds(clear_intervals=1)
    result = detect_faults(
        _history([1.0]),
        previously_flagged=frozenset({FaultCategory.STUCK}),
        thresholds=thresholds,
    )
    assert FaultCategory.STUCK not in result.categories


def test_a_still_faulty_sensor_keeps_its_flag() -> None:
    result = detect_faults(
        _history([7.0] * 6), previously_flagged=frozenset({FaultCategory.STUCK})
    )
    assert FaultCategory.STUCK in result.categories
    assert not result.cleared


# --- Req 13.3 / 13.11 assessment, not quarantine ------------------------

def test_the_detector_reports_categories_and_never_quarantines() -> None:
    result = detect_faults(_history([7.0] * 6))
    assert result.categories
    assert not hasattr(result, "quarantined")


def test_faults_are_counted_when_a_registry_is_supplied() -> None:
    metrics = MetricsRegistry()
    detect_faults(_history([7.0] * 6), metrics=metrics)
    assert metrics.snapshot().counters["site_faults"][FaultCategory.STUCK.value] == 1


def test_metrics_are_optional() -> None:
    assert detect_faults(_history([7.0] * 6)).categories


def test_categories_are_reported_in_a_defined_order() -> None:
    # §2: order reaching output must be defined
    history = _history([7.0] * 6 + [None, None, None])
    result = detect_faults(history)
    assert list(result.categories) == sorted(result.categories)


def test_a_clean_sensor_has_no_categories() -> None:
    assert not detect_faults(_history([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])).categories
