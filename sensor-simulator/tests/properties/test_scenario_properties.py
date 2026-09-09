"""Scenario property tests (tasks 14.5, 14.6, 14.7).

Feature: sensor-simulator-service
- Property 38: observable effect of scenarios (Req 10.8, 10.9)
- Property 39: scenario magnitude bounds (Req 10.2, 10.3, 10.4, 10.10)
- Property 40: scenario precedence composition (Req 10.7)
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given, settings
from hypothesis import strategies as st

from aqm_simulator.scenarios.engine import ScenarioEngine, ScheduleEntry

_START = dt.datetime(2026, 7, 1, 3, tzinfo=dt.UTC)
_END = dt.datetime(2026, 7, 1, 9, tzinfo=dt.UTC)
_NON_CLEAN = ["pollution_episode", "rush_hour_no2", "wildfire_smoke"]


def _engine(entries: list[ScheduleEntry]) -> ScenarioEngine:
    return ScenarioEngine(entries=entries, swarm_site_codes={"CB0001"})


@given(scenario=st.sampled_from(_NON_CLEAN))
@settings(max_examples=100)
def test_property_38_observable_effect(scenario: str) -> None:
    """Feature: sensor-simulator-service, Property 38 — a non-clean scenario differs
    from the clean baseline at some point over its window (Req 10.8)."""
    eng = _engine([ScheduleEntry(scenario, _START, _END)])
    # probe every hour across the window; the effect must appear somewhere
    differs_somewhere = False
    for h in range(3, 9):
        when = dt.datetime(2026, 7, 1, h, tzinfo=dt.UTC)
        mod = eng.resolve("CB0001", when, "Roadside", in_rush_window=True)
        if (
            abs(mod.pm25_multiplier - 1.0) > 1e-9
            or abs(mod.pm25_add) > 1e-9
            or abs(mod.no2_multiplier - 1.0) > 1e-9
        ):
            differs_somewhere = True
            break
    assert differs_somewhere


@given(probe_h=st.integers(min_value=3, max_value=8))
@settings(max_examples=100)
def test_property_39_magnitude_bounds(probe_h: int) -> None:
    """Feature: sensor-simulator-service, Property 39 — effects within permitted magnitude."""
    when = dt.datetime(2026, 7, 1, probe_h, tzinfo=dt.UTC)
    # wildfire multiplier never exceeds its 10.0 permitted max (default 3.0)
    wf = _engine([ScheduleEntry("wildfire_smoke", _START, _END)])
    assert 1.0 <= wf.resolve("CB0001", when, "Roadside", True).pm25_multiplier <= 10.0
    # rush multiplier within 1.0..5.0
    rh = _engine([ScheduleEntry("rush_hour_no2", _START, _END)])
    assert 1.0 <= rh.resolve("CB0001", when, "Roadside", True).no2_multiplier <= 5.0


@given(probe_h=st.integers(min_value=5, max_value=8))
@settings(max_examples=100)
def test_property_40_precedence_composition(probe_h: int) -> None:
    """Feature: sensor-simulator-service, Property 40 — deterministic composition."""
    # probe_h >= 5 is past both ramps (start 03:00, ramp/onset <= 3h) so both
    # effects are at full strength.
    when = dt.datetime(2026, 7, 1, probe_h, tzinfo=dt.UTC)
    entries = [
        ScheduleEntry("wildfire_smoke", _START, _END),
        ScheduleEntry("rush_hour_no2", _START, _END),
    ]
    a = _engine(entries).resolve("CB0001", when, "Roadside", in_rush_window=True)
    b = _engine(list(reversed(entries))).resolve("CB0001", when, "Roadside", in_rush_window=True)
    # composition is order-independent of schedule listing (fixed precedence)
    assert a == b
    # both effects present at full strength
    assert a.pm25_multiplier > 1.0 and a.no2_multiplier >= 1.8
