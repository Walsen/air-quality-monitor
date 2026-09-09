"""Scenario engine package.

Importing this package registers all built-in scenario strategies with the
registry, so ``get_scenario(name)`` resolves the real implementations.
"""

from __future__ import annotations

# Side-effect import: registers pollution_episode, wildfire_smoke, rush_hour_no2.
from aqm_simulator.scenarios import atmospheric as _atmospheric  # noqa: F401
