"""The time boundary.

Requirement 27.1 makes time an injected dependency: only the process edge holds a
:class:`SystemClock`, and every domain function takes the instant it needs or the
clock that supplies it. That is what lets the whole suite run deterministically
and lets backfill replay history without waiting for it.

Requirement 27.8 requires every instant to be timezone-aware UTC. The
implementations here NORMALISE rather than merely check: an instant supplied in
another offset is converted, and a naive one is refused outright, because a naive
datetime silently compares as local time and would corrupt every window boundary
and retention calculation downstream.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable


def _as_utc(instant: dt.datetime) -> dt.datetime:
    """Return ``instant`` as timezone-aware UTC, refusing a naive value."""
    if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
        raise ValueError(
            "instant must be timezone-aware; a naive datetime would be compared "
            f"as local time: {instant!r}"
        )
    return instant.astimezone(dt.UTC)


@runtime_checkable
class Clock(Protocol):
    """The single source of the current instant."""

    def now(self) -> dt.datetime:
        """Return the current instant as timezone-aware UTC."""
        ...


class SystemClock:
    """The real clock, used only at the process edge (Requirement 27.1)."""

    def now(self) -> dt.datetime:
        """Return the wall-clock instant in UTC."""
        return dt.datetime.now(dt.UTC)


class FixedClock:
    """A clock frozen at one instant, for tests that need a stable 'now'."""

    def __init__(self, instant: dt.datetime) -> None:
        """Freeze at ``instant``, normalised to UTC."""
        self._instant = _as_utc(instant)

    def now(self) -> dt.datetime:
        """Return the frozen instant."""
        return self._instant


class AdvanceableClock:
    """A clock a test or a backfill run moves forward explicitly.

    Time only ever moves forward: a backward step is refused, because "the most
    recent Reading" and the retention window both lose meaning if it can regress.
    """

    def __init__(self, instant: dt.datetime) -> None:
        """Start at ``instant``, normalised to UTC."""
        self._instant = _as_utc(instant)

    def now(self) -> dt.datetime:
        """Return the current instant."""
        return self._instant

    def advance(self, delta: dt.timedelta) -> None:
        """Move forward by ``delta``.

        Raises:
            ValueError: if ``delta`` is negative.
        """
        if delta < dt.timedelta(0):
            raise ValueError(f"a clock cannot move backward; got {delta!r}")
        self._instant = self._instant + delta

    def set(self, instant: dt.datetime) -> None:
        """Jump to ``instant``, normalised to UTC."""
        self._instant = _as_utc(instant)
