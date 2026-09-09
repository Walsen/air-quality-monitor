"""The Swarm_Manager.

Instantiates and ticks the fleet. All Virtual_Sensors are built up front, before
the first tick, in ascending ``SiteCode`` order (Requirement 8.1). ``tick_all``
ticks each sensor via an injected per-sensor tick function; a failure in one
sensor is caught, logged at ``error`` with its ``SiteCode``, the operation, and
the error type, and the swarm carries on with the rest — one sensor raising
never stops the others and never brings the process down (Requirement 17.4,
engineering-practices §5).

The tick function is injected so the manager depends on an abstraction, not on a
concrete signal pipeline (§1 dependency inversion) — the pipeline wires the real
one in later.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from aqm_simulator.geography.profiles import GeographyProfile
from aqm_simulator.observability.logging import get_logger
from aqm_simulator.rng.streams import RandomStreamFactory
from aqm_simulator.swarm.factory import VirtualSensor, build_swarm

_logger = get_logger("swarm.manager")

TickFn = Callable[[VirtualSensor, dt.datetime], None]


class SwarmManager:
    """Owns the fleet: builds it once, then ticks it with per-sensor isolation."""

    def __init__(
        self, size: int, profile: GeographyProfile, factory: RandomStreamFactory
    ) -> None:
        # Instantiate the whole swarm before the first tick (Requirement 8.1).
        self._sensors = build_swarm(size=size, profile=profile, factory=factory)

    @property
    def sensors(self) -> list[VirtualSensor]:
        return self._sensors

    def tick_all(self, when: dt.datetime, tick_fn: TickFn) -> None:
        """Tick every sensor, isolating per-sensor failures (Requirement 17.4)."""
        for sensor in self._sensors:
            try:
                tick_fn(sensor, when)
            # True per-sensor boundary: catch broadly, log, and continue so one
            # sensor never stops the swarm or the process (§5, Requirement 17.4).
            except Exception:
                _logger.exception(
                    "sensor_tick_failed",
                    site_code=sensor.site_code,
                    operation="tick",
                    timestamp=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
