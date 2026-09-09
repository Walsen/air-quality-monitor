"""The three atmospheric scenarios (Strategy implementations).

Each is a :class:`Scenario` that computes a :class:`ScenarioModifier` from the
evaluation context, and registers itself with the registry (§4). The engine
(task 14.4) supplies the context (elapsed time in window, classification,
rush-window flag) and composes the modifiers.

- ``pollution_episode`` (Requirement 10.2): an ADDITIVE PM2.5 lift that ramps in
  over the ramp duration (default 3h) to bring the emitted value to >= 35.5
  µg/m³, held until the window ends. NO2 untouched.
- ``wildfire_smoke`` (Requirement 10.4): a PM2.5 MULTIPLIER (default 3.0)
  reached within the onset (default 1h) and held; NO2 untouched. The emitted-PM2.5
  peak cap (250) is applied by the pipeline's clamp, not here.
- ``rush_hour_no2`` (Requirement 10.3): an NO2 MULTIPLIER (default 1.8) applied
  ONLY at Roadside sensors during a rush window; other classifications untouched.
"""

from __future__ import annotations

from aqm_simulator.scenarios.context import ScenarioContext
from aqm_simulator.scenarios.registry import ScenarioModifier, register

# Requirement 10.2 defaults
_EPISODE_TARGET_ADD = 30.0  # additive lift so a ~clean baseline reaches >= 35.5
_EPISODE_RAMP_H = 3.0
# Requirement 10.4 defaults
_WILDFIRE_MULT = 3.0
_WILDFIRE_ONSET_H = 1.0
# Requirement 10.3 default
_RUSH_MULT = 1.8


def _ramp_fraction(elapsed_hours: float, ramp_h: float) -> float:
    """0..1 linear ramp reaching 1.0 at ``ramp_h`` and held after."""
    if ramp_h <= 0:
        return 1.0
    return min(1.0, max(0.0, elapsed_hours / ramp_h))


class PollutionEpisodeScenario:
    name = "pollution_episode"

    def __init__(self, target_add: float = _EPISODE_TARGET_ADD, ramp_h: float = _EPISODE_RAMP_H):
        self._target_add = target_add
        self._ramp_h = ramp_h

    def modifier(self, context: ScenarioContext) -> ScenarioModifier:
        add = self._target_add * _ramp_fraction(context.elapsed_hours, self._ramp_h)
        return ScenarioModifier(pm25_add=add)


class WildfireSmokeScenario:
    name = "wildfire_smoke"

    def __init__(self, multiplier: float = _WILDFIRE_MULT, onset_h: float = _WILDFIRE_ONSET_H):
        self._multiplier = multiplier
        self._onset_h = onset_h

    def modifier(self, context: ScenarioContext) -> ScenarioModifier:
        frac = _ramp_fraction(context.elapsed_hours, self._onset_h)
        # ramp the multiplier from 1.0 up to the full multiplier over the onset
        mult = 1.0 + (self._multiplier - 1.0) * frac
        return ScenarioModifier(pm25_multiplier=mult)


class RushHourNo2Scenario:
    name = "rush_hour_no2"

    def __init__(self, multiplier: float = _RUSH_MULT):
        self._multiplier = multiplier

    def modifier(self, context: ScenarioContext) -> ScenarioModifier:
        # Roadside sensors in a rush window only (Requirement 10.3).
        if context.classification == "Roadside" and context.in_rush_window:
            return ScenarioModifier(no2_multiplier=self._multiplier)
        return ScenarioModifier()


def register_atmospheric_scenarios() -> None:
    register(PollutionEpisodeScenario())
    register(WildfireSmokeScenario())
    register(RushHourNo2Scenario())


# Register on import so the registry resolves the real strategies.
register_atmospheric_scenarios()
