"""Counters and gauges, exposed through one internal interface.

Requirement 29.10 makes metric transport and alarm definition a DEPLOYMENT
concern: this module only accumulates and hands out a snapshot, and a deployment
adapter publishes it. That is why nothing here talks to CloudWatch, and why
:meth:`MetricsRegistry.snapshot` returns plain data rather than pushing anywhere.

The freshness gauge (Requirement 29.7) takes the observation time as an argument
rather than reading the clock, so the registry holds no hidden time dependency and
its behaviour is deterministic under test.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

# A counter with no dimension still needs a key; the empty label keeps the shape
# uniform for a publisher iterating every counter the same way.
_NO_LABEL = ""


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """An immutable reading of every counter and gauge."""

    counters: dict[str, dict[str, int]] = field(default_factory=dict)
    reading_age_seconds: dict[str, int] = field(default_factory=dict)


class MetricsRegistry:
    """Accumulates the counters and gauges Requirements 29.6 and 29.7 name."""

    def __init__(self) -> None:
        """Start with every counter and gauge empty."""
        self._counters: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._latest_reading: dict[str, dt.datetime] = {}

    # --- counters (Requirement 29.6) --------------------------------------

    def _increment(self, name: str, label: str = _NO_LABEL) -> None:
        self._counters[name][label] += 1

    def record_ingested(self, transport: str) -> None:
        """Count one record ingested over a transport."""
        self._increment("records_ingested", transport)

    def record_quarantined(self, reason: str) -> None:
        """Count one record quarantined for a rejection reason."""
        self._increment("records_quarantined", reason)

    def record_reading(self, quality_flag: str) -> None:
        """Count one Reading stored under a Quality_Flag."""
        self._increment("readings", quality_flag)

    def record_site_fault(self, category: str) -> None:
        """Count one site exhibiting a fault category."""
        self._increment("site_faults", category)

    def record_dedup_conflict(self) -> None:
        """Count one deduplication conflict."""
        self._increment("dedup_conflicts")

    def record_forecast_degradation(self) -> None:
        """Count one degraded forecast or pollen enrichment."""
        self._increment("forecast_degradations")

    def record_auth_rejection(self, category: str) -> None:
        """Count one authentication rejection by category."""
        self._increment("auth_rejections", category)

    def record_response(self, route: str) -> None:
        """Count one response served on a route."""
        self._increment("responses", route)

    # --- gauge (Requirement 29.7) -----------------------------------------

    def observe_reading_accepted(
        self, site_code: str, interval_start: dt.datetime
    ) -> None:
        """Record the interval of the most recent accepted Reading for a site.

        A late-arriving OLDER record must not make the site look stale, so only a
        newer interval advances the mark.
        """
        current = self._latest_reading.get(site_code)
        if current is None or interval_start > current:
            self._latest_reading[site_code] = interval_start

    # --- publication ------------------------------------------------------

    def snapshot(self, now: dt.datetime | None = None) -> MetricsSnapshot:
        """Return an immutable copy of the counters, and ages when ``now`` is given.

        The copy matters: a publisher must be able to serialize a snapshot without
        it changing under an in-flight ingestion.
        """
        counters = {
            name: dict(labels) for name, labels in self._counters.items() if labels
        }
        ages: dict[str, int] = {}
        if now is not None:
            ages = {
                site: max(0, int((now - interval).total_seconds()))
                for site, interval in self._latest_reading.items()
            }
        return MetricsSnapshot(counters=counters, reading_age_seconds=ages)
