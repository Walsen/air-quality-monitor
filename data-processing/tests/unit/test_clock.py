"""Unit tests for the Clock port (task 3.1).

- Req 27.1: time enters the service through ONE injected boundary, so no domain
  module reads the wall clock. SystemClock sits at the process edge; FixedClock and
  AdvanceableClock serve tests and backfill.
- Req 27.8: every instant is timezone-aware UTC, so a naive datetime can never
  reach a comparison and silently mean local time.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aqm_ingestion.ports.clock import (
    AdvanceableClock,
    Clock,
    FixedClock,
    SystemClock,
)

_T0 = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def test_system_clock_satisfies_the_protocol() -> None:
    assert isinstance(SystemClock(), Clock)


def test_fixed_clock_satisfies_the_protocol() -> None:
    assert isinstance(FixedClock(_T0), Clock)


def test_advanceable_clock_satisfies_the_protocol() -> None:
    assert isinstance(AdvanceableClock(_T0), Clock)


# --- Req 27.8 timezone-aware UTC -----------------------------------------

def test_system_clock_returns_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == dt.timedelta(0)


def test_fixed_clock_returns_the_supplied_instant() -> None:
    assert FixedClock(_T0).now() == _T0


def test_fixed_clock_does_not_move() -> None:
    clock = FixedClock(_T0)
    assert clock.now() == clock.now() == _T0


def test_naive_instant_is_rejected() -> None:
    naive = dt.datetime(2026, 7, 1, 12)
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(naive)


def test_non_utc_instant_is_normalised_to_utc() -> None:
    offset = dt.timezone(dt.timedelta(hours=5))
    clock = FixedClock(dt.datetime(2026, 7, 1, 17, tzinfo=offset))
    assert clock.now() == _T0  # same instant, expressed in UTC
    assert clock.now().utcoffset() == dt.timedelta(0)


# --- AdvanceableClock ----------------------------------------------------

def test_advanceable_clock_starts_at_the_supplied_instant() -> None:
    assert AdvanceableClock(_T0).now() == _T0


def test_advance_moves_time_forward() -> None:
    clock = AdvanceableClock(_T0)
    clock.advance(dt.timedelta(hours=2))
    assert clock.now() == _T0 + dt.timedelta(hours=2)


def test_advance_accumulates() -> None:
    clock = AdvanceableClock(_T0)
    clock.advance(dt.timedelta(minutes=30))
    clock.advance(dt.timedelta(minutes=30))
    assert clock.now() == _T0 + dt.timedelta(hours=1)


def test_advance_rejects_a_backward_step() -> None:
    # a clock that can go backwards would make "most recent" meaningless
    clock = AdvanceableClock(_T0)
    with pytest.raises(ValueError, match="backward"):
        clock.advance(dt.timedelta(seconds=-1))


def test_advance_by_zero_is_allowed() -> None:
    clock = AdvanceableClock(_T0)
    clock.advance(dt.timedelta(0))
    assert clock.now() == _T0


def test_set_moves_to_an_explicit_instant() -> None:
    clock = AdvanceableClock(_T0)
    clock.set(_T0 + dt.timedelta(days=1))
    assert clock.now() == _T0 + dt.timedelta(days=1)


def test_set_rejects_a_naive_instant() -> None:
    clock = AdvanceableClock(_T0)
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(dt.datetime(2026, 7, 2))
