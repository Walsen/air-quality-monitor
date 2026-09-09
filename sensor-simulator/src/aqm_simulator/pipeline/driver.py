"""Real-time and Backfill_Mode drivers.

Both drivers run the SAME :class:`PublishPipeline` over successive
Publish_Intervals; they differ ONLY in what advances time (engineering-practices
§2), so their output for a given simulated interval is identical
(mode-equivalence, Requirement 12.4).

- :func:`backfill` generates every Publish_Interval whose start lies in
  ``[start, end)`` in non-decreasing ``DateTime`` order, without waiting on the
  wall clock (Requirement 12.3).
- :func:`real_time` advances one Publish_Interval at a time from ``start``,
  waiting for each interval boundary through the injected clock so records are
  emitted within 5 seconds of the boundary (Requirements 12.1, 12.7). Bounded by
  ``max_intervals`` so a caller (or a test) can run a finite number of intervals
  without a real sleep.

Each yields the ``SensorDataRecord`` list for one interval, so a caller can
serialize and publish (or buffer) per interval.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

from aqm_simulator.contract.records import SensorDataRecord
from aqm_simulator.pipeline.publish import PublishPipeline
from aqm_simulator.time.clock import Clock, SystemClock


def _interval(minutes: int) -> dt.timedelta:
    return dt.timedelta(minutes=minutes)


def backfill(
    pipeline: PublishPipeline,
    start: dt.datetime,
    end: dt.datetime,
    reference_time: dt.datetime,
) -> Iterator[SensorDataRecord]:
    """Yield records for every Publish_Interval whose start is in [start, end)."""
    step = _interval(pipeline.publish_minutes)
    current = start
    while current < end:
        yield from pipeline.run_interval(current, reference_time=reference_time)
        current = current + step


def real_time(
    pipeline: PublishPipeline,
    start: dt.datetime,
    max_intervals: int,
    reference_time: dt.datetime,
    clock: Clock | None = None,
) -> Iterator[SensorDataRecord]:
    """Yield records for successive Publish_Intervals, waiting on each boundary.

    ``clock`` drives the wall-clock wait so real-time and backfill differ only in
    the clock; the default is a :class:`SystemClock`. ``max_intervals`` bounds the
    run (a test passes a small value; a service passes a large one).
    """
    driving_clock = clock if clock is not None else SystemClock()
    step = _interval(pipeline.publish_minutes)
    current = start
    for _ in range(max_intervals):
        driving_clock.wait_until(current + step)  # wait for the interval to close
        yield from pipeline.run_interval(current, reference_time=reference_time)
        current = current + step
