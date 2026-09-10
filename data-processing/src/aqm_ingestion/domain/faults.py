"""Detecting a misbehaving sensor from its recent history.

A fault here is an ASSESSMENT, never a validation failure (Requirement 13.3): the reading is
kept and flagged, because a stuck sensor's output is still evidence of what the sensor did,
and discarding it would hide the fault rather than record it. There is deliberately no
quarantine path in this module.

Pure over a supplied history (§2) — no store, no clock. That is what lets a stuck sensor be
simulated exactly rather than approximately, and it is why Requirement 13.12's determinism
is structural.

Two choices worth stating, because the obvious alternative is wrong:

- **Stuck is judged on the REPORTED value, not the corrected one.** A stuck sensor repeats
  its raw output; the corrected value moves with humidity, so a corrected series from a
  stuck sensor varies and would hide the very fault this check exists to find.
- **Drift compares against the peer MEDIAN, not the mean** (Requirement 13.6). One
  malfunctioning peer would drag a mean far enough to mask a genuine drift, or to invent
  one; a median absorbs it.

Series convention matches the NowCast module: index 0 is the MOST RECENT interval,
increasing into the past.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from enum import StrEnum

from aqm_ingestion.observability.logging import get_logger
from aqm_ingestion.observability.metrics import MetricsRegistry

_logger = get_logger("domain.faults")


class FaultCategory(StrEnum):
    """The fault conditions Requirement 13 names."""

    STUCK = "stuck"
    DROPOUT = "dropout"
    DRIFT = "drift"


@dataclass(frozen=True, slots=True)
class FaultThresholds:
    """The configured bounds fault detection uses (Requirement 13.4-13.9).

    A narrow parameter object rather than the whole configuration (§1). Defaults are the
    documented ones.
    """

    stuck_intervals: int = 6
    stuck_tolerance: float = 0.01
    dropout_intervals: int = 3
    drift_window_hours: int = 24
    drift_min_peers: int = 3
    drift_radius_km: float = 10.0
    drift_multiple: float = 3.0
    clear_intervals: int = 3


DEFAULT_FAULT_THRESHOLDS = FaultThresholds()
"""Requirement 13's documented defaults."""


@dataclass(frozen=True, slots=True)
class SiteHistory:
    """One site and species' recent intervals, most recent first.

    Both series are carried because the two checks need different ones: stuck judges the
    reported value, drift the corrected one. Keeping them together makes it impossible to
    pass one where the other was meant.
    """

    site_code: str
    species: str
    reported: tuple[float | None, ...]
    corrected: tuple[float | None, ...]


@dataclass(frozen=True, slots=True)
class PeerSeries:
    """A neighbouring site's statistic for the drift comparison (Requirement 13.6)."""

    site_code: str
    mean_corrected: float
    distance_km: float


@dataclass(frozen=True, slots=True)
class FaultResult:
    """Which categories currently hold, and which just cleared."""

    categories: tuple[FaultCategory, ...]
    cleared: tuple[FaultCategory, ...] = ()


def detect_faults(
    history: SiteHistory,
    *,
    peers: tuple[PeerSeries, ...] = (),
    previously_flagged: frozenset[FaultCategory] = frozenset(),
    thresholds: FaultThresholds = DEFAULT_FAULT_THRESHOLDS,
    metrics: MetricsRegistry | None = None,
) -> FaultResult:
    """Evaluate every fault condition for one site and species.

    Args:
        history: the site's recent intervals, most recent first.
        peers: neighbouring sites' statistics for the drift comparison.
        previously_flagged: categories already flagged, so a raise or a clear can be
            reported as a TRANSITION (Requirement 13.8) rather than re-logged every
            interval.
        thresholds: the configured bounds.
        metrics: optional registry for Requirement 13.11's per-category counter.

    Returns:
        The categories currently holding and those that just cleared.
    """
    detected: set[FaultCategory] = set()

    if _is_stuck(history.reported, thresholds):
        detected.add(FaultCategory.STUCK)
    if _is_dropout(history.reported, thresholds):
        detected.add(FaultCategory.DROPOUT)
    if _is_drifting(history, peers, thresholds):
        detected.add(FaultCategory.DRIFT)

    # Requirement 13.9: a flag only clears once enough consecutive intervals satisfy no
    # condition. Clearing on the strength of a shorter history would declare a sensor
    # healthy on insufficient evidence.
    clean_enough = len(history.reported) >= thresholds.clear_intervals
    still_flagged = {
        category
        for category in previously_flagged
        if category in detected or not clean_enough
    }
    cleared = previously_flagged - still_flagged

    current = detected | still_flagged
    _report_transitions(history, detected - previously_flagged, cleared, metrics)

    return FaultResult(
        categories=tuple(sorted(current)),  # defined order (§2)
        cleared=tuple(sorted(cleared)),
    )


def _is_stuck(reported: tuple[float | None, ...], thresholds: FaultThresholds) -> bool:
    """Requirement 13.4: a run of equal-within-tolerance values ending at the present.

    Anchored at the MOST RECENT interval, so a flat stretch the sensor has since moved on
    from does not flag it. An absent interval breaks the run rather than extending it — a
    gap is not an equal value, and treating it as one would invent a run.
    """
    if len(reported) < thresholds.stuck_intervals:
        return False
    window = reported[: thresholds.stuck_intervals]
    if any(value is None for value in window):
        return False
    values = [value for value in window if value is not None]
    return all(
        math.isclose(value, values[0], abs_tol=thresholds.stuck_tolerance)
        for value in values
    )


def _is_dropout(
    reported: tuple[float | None, ...], thresholds: FaultThresholds
) -> bool:
    """Requirement 13.5: consecutive expected intervals with no accepted Reading.

    Anchored at the present for the same reason as stuck: an old outage the site has
    recovered from is history, not a current fault.
    """
    if thresholds.dropout_intervals <= 0:
        return False
    window = reported[: thresholds.dropout_intervals]
    if len(window) < thresholds.dropout_intervals:
        return False
    return all(value is None for value in window)


def _is_drifting(
    history: SiteHistory,
    peers: tuple[PeerSeries, ...],
    thresholds: FaultThresholds,
) -> bool:
    """Requirement 13.6: the site's mean far from the peer median."""
    in_radius = [
        peer for peer in peers if peer.distance_km <= thresholds.drift_radius_km
    ]
    if len(in_radius) < thresholds.drift_min_peers:
        # Requirement 13.7: no drift flag, and one debug event — debug rather than
        # warning because a thinly instrumented area is expected, not a problem.
        _logger.debug(
            "drift_skipped_too_few_peers",
            SiteCode=history.site_code,
            Species=history.species,
            peer_count=len(in_radius),
            required=thresholds.drift_min_peers,
        )
        return False

    own = [value for value in history.corrected if value is not None]
    if not own:
        return False  # a dropout, which the dropout check owns

    own_mean = statistics.fmean(own)
    peer_median = statistics.median(peer.mean_corrected for peer in in_radius)
    # Multiplying rather than dividing keeps a zero peer median well-defined: the
    # threshold becomes 0, so any nonzero difference drifts, and nothing divides by zero.
    return abs(own_mean - peer_median) >= thresholds.drift_multiple * peer_median


def _report_transitions(
    history: SiteHistory,
    raised: set[FaultCategory],
    cleared: frozenset[FaultCategory],
    metrics: MetricsRegistry | None,
) -> None:
    """Log one event per TRANSITION and count each raise (Requirements 13.8, 13.11).

    Transitions only: re-logging a standing fault every interval would bury the change an
    operator actually needs to see.
    """
    for category in sorted(raised):
        _logger.info(
            "fault_flag",
            SiteCode=history.site_code,
            Species=history.species,
            category=category.value,
            transition="raised",
        )
        if metrics is not None:
            metrics.record_site_fault(category=category.value)
    for category in sorted(cleared):
        _logger.info(
            "fault_flag",
            SiteCode=history.site_code,
            Species=history.species,
            category=category.value,
            transition="cleared",
        )
