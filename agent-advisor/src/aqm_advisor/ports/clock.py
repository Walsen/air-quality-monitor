"""The time boundary (Requirements 1.3, 25.2, 25.5).

Every instant in an Advisory_Response comes from here, which is what makes Requirement 25.1's
byte-identical claim provable: a wall-clock read inside the pipeline would make two runs over
identical inputs differ, and there would be nothing to compare.

A NAIVE INSTANT IS REFUSED rather than assumed to be UTC. A naive ``datetime`` compares as local
time, so accepting one would corrupt every window and expiry comparison downstream — including
the
Requirement 32.8a check on whether a forwarded credential is near expiry — while looking
entirely
fine in a test that happened to run in UTC.

An OFFSET-AWARE instant is normalised instead of refused, because it names a real moment
unambiguously and converting it loses nothing. Only the ambiguous case is an error.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Supplies the current instant.

    Deliberately exposes ONLY ``now``. A clock that could also sleep or be advanced would invite
    a
    caller to drive time from inside the turn, which is the thing this port exists to prevent
    (§1).
    """

    def now(self) -> dt.datetime:
        """Return the current instant as an aware UTC datetime."""
        ...


def _as_utc(instant: dt.datetime) -> dt.datetime:
    """Normalise to UTC, refusing a naive instant.

    Raises:
        ValueError: for a naive datetime, naming why rather than only that it failed — a caller
            handed one has usually built it with ``datetime.now()`` and needs to know that is
            the
            problem.
    """
    if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
        raise ValueError(
            "a naive datetime is refused: it compares as local time, so it would corrupt every "
            "window and expiry comparison. Supply an aware instant in UTC."
        )
    return instant.astimezone(dt.UTC)


class SystemClock:
    """The real clock. Constructed at the PROCESS EDGE only.

    Never inside the domain, the pipeline or a tool — the task-2.5 architecture check enforces
    that by refusing any wall-clock read under ``domain/``.
    """

    def now(self) -> dt.datetime:
        """The current instant, in UTC."""
        return dt.datetime.now(tz=dt.UTC)


class FixedClock:
    """A clock frozen at one instant, for tests and for a reproducible replay."""

    def __init__(self, instant: dt.datetime) -> None:
        """Freeze at ``instant``.

        Raises:
            ValueError: if the instant is naive.
        """
        self._instant = _as_utc(instant)

    def now(self) -> dt.datetime:
        """The frozen instant."""
        return self._instant


__all__ = ["Clock", "FixedClock", "SystemClock"]
