"""The Scenario registry (Strategy pattern).

Scenarios are genuine strategies (engineering-practices §4): each is an
independently testable object selected by name from this registry, never an arm
in a long if/elif chain — adding one is one registry entry. Exactly five names
are supported (Requirement 10.1); any other is unrecognized.

A :class:`Scenario` produces a :class:`ScenarioModifier` describing how it bends
the signal (multiply/shift the regional PM2.5 field, multiply NO2, or drive a
sensor fault). ``clean`` is the identity modifier, and an uncovered timestamp
applies no modifier (Requirement 10.9) — the engine (task 14.4) resolves which
scenarios cover a given instant before composing their modifiers.

This module defines the protocol, the registry, and the ``clean`` identity;
the three atmospheric scenarios (14.2) and ``sensor_fault`` (14.3) register
their own strategies here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from aqm_simulator.scenarios.context import ScenarioContext

SUPPORTED_SCENARIOS = (
    "clean",
    "pollution_episode",
    "rush_hour_no2",
    "wildfire_smoke",
    "sensor_fault",
)


class UnknownScenarioError(ValueError):
    """Raised for a scenario name outside the supported set (Requirement 10.1)."""


@dataclass(frozen=True, slots=True)
class ScenarioModifier:
    """How a scenario bends the signal for one sensor at one interval.

    Multipliers default to 1.0 and additive terms to 0.0, so an empty modifier
    is the identity (the ``clean`` baseline).
    """

    pm25_multiplier: float = 1.0
    pm25_add: float = 0.0
    no2_multiplier: float = 1.0
    # Fault effect a scenario may drive on a targeted sensor; None = none.
    fault: str | None = None


@runtime_checkable
class Scenario(Protocol):
    """A named air-quality scenario strategy."""

    @property
    def name(self) -> str: ...

    def modifier(self, context: ScenarioContext) -> ScenarioModifier:
        """The modifier this scenario applies for the given evaluation context."""
        ...


class CleanScenario:
    """The identity scenario: no modification (Requirement 10.9)."""

    name = "clean"

    def modifier(self, context: ScenarioContext) -> ScenarioModifier:
        return ScenarioModifier()


class _IdentityPlaceholder:
    """A name-correct identity scenario, replaced by the real strategy later.

    Keeps the registry resolving all five supported names with the correct
    ``name`` before tasks 14.2/14.3 register the atmospheric and fault
    strategies; its modifier is the clean identity until then.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def modifier(self, context: ScenarioContext) -> ScenarioModifier:
        return ScenarioModifier()


# The registry. clean is the real identity; the other four start as name-correct
# placeholders that tasks 14.2/14.3 replace with real strategies via register().
_REGISTRY: dict[str, Scenario] = {"clean": CleanScenario()}
for _name in SUPPORTED_SCENARIOS:
    if _name != "clean":
        _REGISTRY[_name] = _IdentityPlaceholder(_name)


def register(scenario: Scenario) -> None:
    """Register (or replace) a scenario strategy by its name."""
    if scenario.name not in SUPPORTED_SCENARIOS:
        raise UnknownScenarioError(
            f"cannot register unsupported scenario {scenario.name!r}; "
            f"supported: {', '.join(SUPPORTED_SCENARIOS)}"
        )
    _REGISTRY[scenario.name] = scenario


def is_supported(name: str) -> bool:
    return name in SUPPORTED_SCENARIOS


def get_scenario(name: str) -> Scenario:
    """Return the scenario strategy for ``name`` (Requirement 10.1)."""
    if name not in SUPPORTED_SCENARIOS:
        raise UnknownScenarioError(
            f"unknown scenario {name!r}; supported: {', '.join(SUPPORTED_SCENARIOS)}"
        )
    return _REGISTRY[name]
