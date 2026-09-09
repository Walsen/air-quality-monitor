"""Scenario evaluation context.

The narrow, purpose-built input a scenario needs to compute its modifier for one
sensor at one Publish_Interval (§1 interface segregation): how long the scenario
window has been active (for ramp/onset shaping), the sensor's classification
(rush_hour_no2 targets Roadside), and whether the interval sits in a rush-hour
window. Scenarios receive this, never the whole simulator state.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScenarioContext:
    """Per-evaluation inputs for a scenario's modifier."""

    elapsed_hours: float
    classification: str
    in_rush_window: bool
