"""Stuck-value and dropout fault windows.

A :class:`FaultWindow` marks a half-open ``[start, end)`` simulated interval
during which a Virtual_Sensor misbehaves. The :class:`FaultController` answers,
for a given SiteCode and Publish_Interval start, what the publish pipeline
should do:

- **dropout** (Requirement 7.3): omit every record for intervals starting in the
  window, for all four Species, with no placeholder. Housekeeping continues
  (handled by the telemetry path, not here).
- **stuck value** (Requirement 7.4): emit, but ``hold`` the ScaledValue the
  Species held in the last interval completed before the window — the pipeline
  repeats that value and derives the index from it.

When a window ends the controller resumes normal emission (Requirement 7.5), and
because a dropout suppresses the concentration record it also suppresses the
paired index record (Requirement 6.8), which the pipeline enforces by pairing.
A window may target one SiteCode or (site_code=None) apply swarm-wide.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass


class FaultKind(enum.Enum):
    STUCK_VALUE = "stuck value"
    DROPOUT = "dropout"


@dataclass(frozen=True, slots=True)
class FaultWindow:
    """A half-open [start, end) fault window, optionally targeting one sensor."""

    kind: FaultKind
    start: dt.datetime
    end: dt.datetime
    site_code: str | None = None

    def covers(self, site_code: str, when: dt.datetime) -> bool:
        if self.site_code is not None and self.site_code != site_code:
            return False
        return self.start <= when < self.end


@dataclass(frozen=True, slots=True)
class FaultDecision:
    """What the pipeline should do for one sensor at one Publish_Interval."""

    emit: bool  # False -> omit the record entirely (dropout)
    hold: bool  # True -> repeat the last pre-window ScaledValue (stuck value)


class FaultController:
    """Resolves fault windows into a per-interval emit/hold decision."""

    def __init__(self, windows: list[FaultWindow]) -> None:
        self._windows = list(windows)

    def decide(self, site_code: str, when: dt.datetime) -> FaultDecision:
        for window in self._windows:
            if window.covers(site_code, when):
                if window.kind is FaultKind.DROPOUT:
                    return FaultDecision(emit=False, hold=False)
                return FaultDecision(emit=True, hold=True)  # stuck value
        return FaultDecision(emit=True, hold=False)

    def active_faults(self, when: dt.datetime, site_code: str | None = None) -> list[str]:
        """Active fault names at ``when`` for the housekeeping active-fault list."""
        names: list[str] = []
        for window in self._windows:
            covers = window.start <= when < window.end and (
                site_code is None or window.site_code in (None, site_code)
            )
            if covers:
                names.append(window.kind.value)
        return names
