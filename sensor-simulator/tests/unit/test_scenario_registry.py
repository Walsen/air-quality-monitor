"""Unit tests for the Scenario protocol and registry (task 14.1).

Requirement 10.1: exactly the names clean, pollution_episode, rush_hour_no2,
wildfire_smoke, sensor_fault are supported; any other is unrecognized.
Requirement 10.9: an uncovered timestamp applies no modifier (clean baseline).
"""

from __future__ import annotations

from aqm_simulator.scenarios.registry import (
    SUPPORTED_SCENARIOS,
    UnknownScenarioError,
    get_scenario,
    is_supported,
)


def test_exactly_the_five_supported_names() -> None:
    assert set(SUPPORTED_SCENARIOS) == {
        "clean",
        "pollution_episode",
        "rush_hour_no2",
        "wildfire_smoke",
        "sensor_fault",
    }


def test_is_supported() -> None:
    assert is_supported("clean")
    assert is_supported("wildfire_smoke")
    assert not is_supported("dust_storm")


def test_get_each_supported_scenario() -> None:
    for name in SUPPORTED_SCENARIOS:
        scenario = get_scenario(name)
        assert scenario.name == name


def test_unknown_scenario_rejected_listing_supported() -> None:
    import pytest

    with pytest.raises(UnknownScenarioError) as exc:
        get_scenario("dust_storm")
    msg = str(exc.value)
    assert "dust_storm" in msg
    assert "clean" in msg  # lists the supported names


def test_clean_is_identity_modifier() -> None:
    clean = get_scenario("clean")
    mod = clean.modifier()
    # identity: PM2.5 and NO2 multipliers 1.0, no additive shift
    assert mod.pm25_multiplier == 1.0
    assert mod.no2_multiplier == 1.0
    assert mod.pm25_add == 0.0
