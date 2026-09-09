"""The injected time boundary.

Domain code never calls ``datetime.now()``; it takes a :class:`Clock`
(engineering-practices §2). Real-time mode and Backfill_Mode differ only in
which concrete clock drives time:

- :class:`SystemClock` — real-time. ``now()`` is the current wall-clock instant
  truncated to the start of the minute (Requirement 12.1); ``wait_until``
  sleeps until the target instant.
- :class:`BackfillClock` — historical generation. ``now()`` starts at a
  supplied instant and ``advance()`` steps it by the tick interval;
  ``wait_until`` is a no-op so backfill never waits on wall-clock time
  (Requirement 12.3).
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Protocol, runtime_checkable


def _floor_to_minute(instant: dt.datetime) -> dt.datetime:
    return instant.replace(second=0, microsecond=0)


@runtime_checkable
class Clock(Protocol):
    """The time source injected into every time-dependent component."""

    def now(self) -> dt.datetime:
        """Return the current simulated instant (UTC, minute-aligned)."""
        ...

    def advance(self) -> None:
        """Advance the clock by one tick interval."""
        ...

    def wait_until(self, target: dt.datetime) -> None:
        """Block until ``target`` (real-time) or return immediately (backfill)."""
        ...


class SystemClock:
    """Real-time clock: ``now()`` is the wall clock floored to the minute."""

    def __init__(self, tick_minutes: int = 1) -> None:
        self._tick = dt.timedelta(minutes=tick_minutes)

    def now(self) -> dt.datetime:
        return _floor_to_minute(dt.datetime.now(dt.UTC))

    def advance(self) -> None:  # real time advances on its own
        return None

    def wait_until(self, target: dt.datetime) -> None:
        remaining = (target - dt.datetime.now(dt.UTC)).total_seconds()
        if remaining > 0:
            time.sleep(remaining)


class BackfillClock:
    """Backfill clock: steps in tick-interval increments without waiting."""

    def __init__(self, start: dt.datetime, tick_minutes: int = 1) -> None:
        if start.tzinfo is None:
            raise ValueError("BackfillClock start must be timezone-aware (UTC)")
        self._current = _floor_to_minute(start.astimezone(dt.UTC))
        self._tick = dt.timedelta(minutes=tick_minutes)

    def now(self) -> dt.datetime:
        return self._current

    def advance(self) -> None:
        self._current = self._current + self._tick

    def wait_until(self, target: dt.datetime) -> None:  # backfill never waits
        return None
