"""Tests for the Clock boundary (task 2.1).

Requirement 25.2 makes every instant in a response come from an injected Clock, which is what
makes
a turn reproducible: a wall-clock read inside the pipeline would make two runs over identical
inputs
differ, and Requirement 25.1's byte-identical claim unprovable.

A NAIVE INSTANT IS REFUSED rather than assumed to be UTC. A naive datetime compares as local
time,
so accepting one would corrupt every window and expiry comparison downstream while looking
completely fine in a test that happened to run in UTC.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aqm_advisor.ports.clock import Clock, FixedClock, SystemClock

_INSTANT = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


# --- the protocol -------------------------------------------------------

def test_both_implementations_satisfy_the_protocol() -> None:
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FixedClock(_INSTANT), Clock)


def test_the_protocol_exposes_only_now() -> None:
    # A narrow port (§1): a clock that could also sleep or advance would invite a caller to
    # drive
    # time from the domain.
    members = {name for name in dir(Clock) if not name.startswith("_")}
    assert members == {"now"}


# --- SystemClock -------------------------------------------------------

def test_the_system_clock_returns_an_aware_utc_instant() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == dt.timedelta(0)


def test_the_system_clock_advances() -> None:
    clock = SystemClock()
    assert clock.now() <= clock.now()


# --- FixedClock --------------------------------------------------------

def test_the_fixed_clock_returns_exactly_what_it_was_given() -> None:
    assert FixedClock(_INSTANT).now() == _INSTANT


def test_the_fixed_clock_does_not_advance() -> None:
    clock = FixedClock(_INSTANT)
    assert clock.now() == clock.now() == _INSTANT


def test_an_offset_instant_is_normalised_to_utc() -> None:
    # Normalised rather than refused: an offset-aware instant names a real moment unambiguously,
    # so converting it is lossless. Only a NAIVE one is ambiguous.
    plus_two = dt.timezone(dt.timedelta(hours=2))
    clock = FixedClock(dt.datetime(2026, 7, 1, 14, tzinfo=plus_two))
    assert clock.now() == _INSTANT
    assert clock.now().tzinfo is dt.UTC


def test_a_naive_instant_is_refused() -> None:
    with pytest.raises(ValueError) as caught:
        FixedClock(dt.datetime(2026, 7, 1, 12))
    assert "naive" in str(caught.value).lower() or "timezone" in str(caught.value).lower()


def test_the_refusal_explains_why_rather_than_just_failing() -> None:
    with pytest.raises(ValueError) as caught:
        FixedClock(dt.datetime(2026, 7, 1, 12))
    assert "utc" in str(caught.value).lower()
