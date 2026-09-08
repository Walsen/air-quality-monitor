"""Unit tests for the injected time boundary (task 4.1).

Requirement 12.1: in real-time mode the simulator produces one tick per sensor
per elapsed wall-clock minute, each tick aligned to the start of that minute.
Requirement 12.3: in Backfill_Mode the clock advances in Publish_Interval steps
without waiting on wall-clock time.

The Clock is the injected boundary that lets real-time and backfill differ only
in what drives time (engineering-practices §2), so no domain code calls
``datetime.now()``.
"""

from __future__ import annotations

import datetime as dt
from itertools import pairwise

from aqm_simulator.time.clock import BackfillClock, Clock, SystemClock


def _utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> dt.datetime:
    return dt.datetime(y, mo, d, h, mi, tzinfo=dt.UTC)


def test_system_and_backfill_are_clocks() -> None:
    assert isinstance(SystemClock(), Clock)
    assert isinstance(BackfillClock(start=_utc(2026, 7, 1), tick_minutes=1), Clock)


def test_backfill_wait_until_does_not_sleep() -> None:
    # Requirement 12.3: backfill does not wait on wall-clock time.
    clock = BackfillClock(start=_utc(2026, 7, 1), tick_minutes=1)
    before = __import__("time").monotonic()
    clock.wait_until(_utc(2027, 1, 1))  # a year ahead — must return immediately
    assert __import__("time").monotonic() - before < 0.5


def test_backfill_ticks_are_one_minute_apart_and_ordered() -> None:
    # Requirement 12.1/12.3: one tick per simulated minute, strictly increasing.
    clock = BackfillClock(start=_utc(2026, 7, 1, 0, 0), tick_minutes=1)
    ticks = [clock.now()]
    for _ in range(5):
        clock.advance()
        ticks.append(clock.now())
    deltas = [(b - a).total_seconds() for a, b in pairwise(ticks)]
    assert deltas == [60.0] * 5
    assert ticks == sorted(ticks)


def test_backfill_now_is_minute_aligned_utc() -> None:
    clock = BackfillClock(start=_utc(2026, 7, 1, 3, 15), tick_minutes=1)
    now = clock.now()
    assert now.tzinfo is dt.UTC
    assert now.second == 0 and now.microsecond == 0


def test_system_clock_now_is_minute_aligned_utc() -> None:
    # Requirement 12.1: real-time ticks align to the start of the minute.
    now = SystemClock().now()
    assert now.tzinfo is dt.UTC
    assert now.second == 0 and now.microsecond == 0


def test_backfill_respects_configurable_tick_interval() -> None:
    clock = BackfillClock(start=_utc(2026, 7, 1), tick_minutes=5)
    start = clock.now()
    clock.advance()
    assert (clock.now() - start).total_seconds() == 300.0
