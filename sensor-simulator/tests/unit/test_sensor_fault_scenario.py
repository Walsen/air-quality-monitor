"""Unit tests for the sensor_fault scenario (task 14.3).

Requirement 10.5: sensor_fault applies the configured fault type (stuck value |
accelerated drift | dropout) to the NAMED sensors only for the window; every
other sensor stays at the clean baseline. Accelerated drift default is 5x the
Requirement 7 drift rate.
"""

from __future__ import annotations

from aqm_simulator.scenarios.context import ScenarioContext
from aqm_simulator.scenarios.sensor_fault import SensorFaultScenario


def _ctx(**over: object) -> ScenarioContext:
    base: dict[str, object] = {
        "elapsed_hours": 2.0,
        "classification": "Roadside",
        "in_rush_window": False,
    }
    base.update(over)
    return ScenarioContext(**base)  # type: ignore[arg-type]


def test_targeted_sensor_gets_the_fault() -> None:
    scn = SensorFaultScenario(fault_kind="dropout", targets={"CB0001"})
    mod = scn.modifier_for("CB0001", _ctx())
    assert mod.fault == "dropout"


def test_untargeted_sensor_is_identity() -> None:
    scn = SensorFaultScenario(fault_kind="dropout", targets={"CB0001"})
    mod = scn.modifier_for("CB0002", _ctx())
    assert mod.fault is None
    assert mod.pm25_multiplier == 1.0
    assert mod.no2_multiplier == 1.0


def test_stuck_value_fault_kind() -> None:
    scn = SensorFaultScenario(fault_kind="stuck value", targets={"CB0003"})
    assert scn.modifier_for("CB0003", _ctx()).fault == "stuck value"


def test_accelerated_drift_default_5x() -> None:
    scn = SensorFaultScenario(fault_kind="accelerated drift", targets={"CB0001"})
    assert scn.drift_multiplier == 5.0
    assert scn.modifier_for("CB0001", _ctx()).fault == "accelerated drift"


def test_name_is_sensor_fault() -> None:
    scn = SensorFaultScenario(fault_kind="dropout", targets={"CB0001"})
    assert scn.name == "sensor_fault"
