"""The sensor_fault scenario (Strategy implementation).

Unlike the atmospheric scenarios, ``sensor_fault`` is inherently per-sensor: it
drives a fault (stuck value, accelerated drift, or dropout) on the NAMED target
sensors only for the window, leaving every other sensor at the clean baseline
(Requirement 10.5). So it exposes ``modifier_for(site_code, context)`` — the
engine (task 14.4) calls that for this scenario — while ``modifier(context)``
returns identity because a fault cannot be decided without a SiteCode.

Accelerated drift defaults to 5x the Requirement 7 drift rate; the drift engine
applies that multiplier when the returned modifier's fault is "accelerated drift".
"""

from __future__ import annotations

from aqm_simulator.scenarios.context import ScenarioContext
from aqm_simulator.scenarios.registry import ScenarioModifier

_ACCEL_DRIFT_MULTIPLIER = 5.0  # Requirement 10.5 default

# Fault kinds this scenario can drive.
_FAULT_KINDS = frozenset({"stuck value", "accelerated drift", "dropout"})


class SensorFaultScenario:
    """Applies a fault to named sensors only; identity for the rest (Req 10.5)."""

    name = "sensor_fault"

    def __init__(
        self,
        fault_kind: str,
        targets: set[str],
        drift_multiplier: float = _ACCEL_DRIFT_MULTIPLIER,
    ) -> None:
        if fault_kind not in _FAULT_KINDS:
            raise ValueError(
                f"unknown sensor_fault kind {fault_kind!r}; "
                f"one of {', '.join(sorted(_FAULT_KINDS))}"
            )
        self._fault_kind = fault_kind
        self._targets = set(targets)
        self.drift_multiplier = drift_multiplier

    def modifier(self, context: ScenarioContext) -> ScenarioModifier:
        # Without a SiteCode the fault cannot be targeted — identity.
        return ScenarioModifier()

    def modifier_for(self, site_code: str, context: ScenarioContext) -> ScenarioModifier:
        """The modifier for a specific sensor: fault iff it is a target."""
        if site_code in self._targets:
            return ScenarioModifier(fault=self._fault_kind)
        return ScenarioModifier()
