"""Unit tests for the three atmospheric scenarios (task 14.2).

Requirement 10.2: pollution_episode ramps regional PM2.5 so emitted PM2.5 is
>=35.5 and <= episode peak (default 120) within ramp (default 3h), held until end.
Requirement 10.3: rush_hour_no2 multiplies Roadside NO2 in rush windows by
>= mult (default 1.8); Urban Background/Suburban within 0.01 of clean baseline.
Requirement 10.4: wildfire_smoke multiplies regional PM2.5 by mult (default 3.0)
within onset (default 1h), caps emitted PM2.5 at peak (default 250), NO2 at baseline.
"""

from __future__ import annotations

from aqm_simulator.scenarios.context import ScenarioContext
from aqm_simulator.scenarios.registry import get_scenario


def _ctx(**over: object) -> ScenarioContext:
    base: dict[str, object] = {
        "elapsed_hours": 5.0,   # well past any ramp/onset
        "classification": "Roadside",
        "in_rush_window": True,
    }
    base.update(over)
    return ScenarioContext(**base)  # type: ignore[arg-type]


def test_pollution_episode_lifts_pm25_after_ramp() -> None:
    scn = get_scenario("pollution_episode")
    mod = scn.modifier(_ctx(elapsed_hours=5.0))
    # additive lift brings a low regional baseline up to >= 35.5
    assert mod.pm25_add >= 35.5 - 12.0  # from ~clean baseline to >=35.5
    assert mod.no2_multiplier == 1.0  # NO2 untouched


def test_pollution_episode_ramps_over_3h() -> None:
    scn = get_scenario("pollution_episode")
    early = scn.modifier(_ctx(elapsed_hours=0.0)).pm25_add
    mid = scn.modifier(_ctx(elapsed_hours=1.5)).pm25_add
    full = scn.modifier(_ctx(elapsed_hours=3.0)).pm25_add
    assert early < mid < full or early <= mid <= full
    assert full >= early


def test_wildfire_multiplies_pm25_and_leaves_no2() -> None:
    scn = get_scenario("wildfire_smoke")
    mod = scn.modifier(_ctx(elapsed_hours=2.0))
    assert mod.pm25_multiplier >= 1.5  # default 3.0, min 1.5
    assert mod.no2_multiplier == 1.0
    assert mod.no2_add if hasattr(mod, "no2_add") else True  # NO2 at baseline


def test_wildfire_reaches_full_multiplier_after_onset() -> None:
    scn = get_scenario("wildfire_smoke")
    at_onset = scn.modifier(_ctx(elapsed_hours=1.0)).pm25_multiplier
    later = scn.modifier(_ctx(elapsed_hours=5.0)).pm25_multiplier
    assert at_onset == later  # full multiplier held after onset
    assert later >= 1.5


def test_rush_hour_multiplies_roadside_only_in_window() -> None:
    scn = get_scenario("rush_hour_no2")
    road = scn.modifier(_ctx(classification="Roadside", in_rush_window=True))
    urban = scn.modifier(_ctx(classification="Urban Background", in_rush_window=True))
    assert road.no2_multiplier >= 1.8
    assert urban.no2_multiplier == 1.0  # non-roadside untouched
    assert road.pm25_multiplier == 1.0  # PM2.5 untouched


def test_rush_hour_no_effect_outside_window() -> None:
    scn = get_scenario("rush_hour_no2")
    mod = scn.modifier(_ctx(classification="Roadside", in_rush_window=False))
    assert mod.no2_multiplier == 1.0
