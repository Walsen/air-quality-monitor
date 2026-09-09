"""The Scenario engine: schedule, ramp, recovery, and precedence composition.

Loads a schedule of :class:`ScheduleEntry` (a scenario name, an ISO start/end,
and an optional target SiteCode list — no targets means swarm-wide) and, for a
given sensor and simulated instant, composes the modifiers of every covering
entry into one :class:`ScenarioModifier`:

- while a window is active the scenario's own ramp shapes it (via elapsed hours);
- for the recovery duration AFTER a window ends (default 2h), the contribution
  tapers linearly back to the clean baseline (Requirement 10.10);
- overlapping windows compose in precedence order — the default is the order the
  scenarios are listed in Requirement 10.1 — multiplying multipliers, summing
  additive lifts, and letting the highest-precedence fault win (Requirement 10.7);
- an uncovered instant yields the identity modifier (Requirement 10.9).

Applied scenario names and affected SiteCodes are exposed for the diagnostic
output, and window open/close is logged at info by the pipeline that drives this.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from aqm_simulator.scenarios.atmospheric import (
    PollutionEpisodeScenario,
    RushHourNo2Scenario,
    WildfireSmokeScenario,
)
from aqm_simulator.scenarios.context import ScenarioContext
from aqm_simulator.scenarios.registry import (
    SUPPORTED_SCENARIOS,
    CleanScenario,
    Scenario,
    ScenarioModifier,
)
from aqm_simulator.scenarios.sensor_fault import SensorFaultScenario

_RECOVERY_H = 2.0  # Requirement 10.10 default recovery duration

# Default precedence = the order scenarios are listed in Requirement 10.1.
_PRECEDENCE = {name: i for i, name in enumerate(SUPPORTED_SCENARIOS)}


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    """One scheduled scenario window (Requirement 10.6)."""

    scenario: str
    start: dt.datetime
    end: dt.datetime
    targets: set[str] | None = None  # None = swarm-wide
    fault_kind: str | None = None  # for sensor_fault entries

    def applies_to(self, site_code: str) -> bool:
        return self.targets is None or site_code in self.targets


def _taper(when: dt.datetime, end: dt.datetime, recovery_h: float) -> float:
    """Recovery factor 1.0 during the window, tapering to 0.0 over recovery_h."""
    if when < end:
        return 1.0
    elapsed_recovery = (when - end).total_seconds() / 3600.0
    if elapsed_recovery >= recovery_h:
        return 0.0
    return 1.0 - elapsed_recovery / recovery_h


class ScenarioEngine:
    """Composes scheduled scenarios into a per-sensor, per-instant modifier."""

    def __init__(
        self,
        entries: list[ScheduleEntry],
        swarm_site_codes: set[str],
        recovery_h: float = _RECOVERY_H,
    ) -> None:
        self._entries = list(entries)
        self._swarm = set(swarm_site_codes)
        self._recovery_h = recovery_h

    def _covering(self, site_code: str, when: dt.datetime) -> list[ScheduleEntry]:
        # An entry covers up to recovery_h past its end so the taper can apply.
        covering = []
        for e in self._entries:
            if not e.applies_to(site_code):
                continue
            recovery_end = e.end + dt.timedelta(hours=self._recovery_h)
            if e.start <= when < recovery_end:
                covering.append(e)
        # compose in precedence order (Requirement 10.7)
        covering.sort(key=lambda e: _PRECEDENCE[e.scenario])
        return covering

    def _build(self, entry: ScheduleEntry) -> Scenario:
        if entry.scenario == "pollution_episode":
            return PollutionEpisodeScenario()
        if entry.scenario == "wildfire_smoke":
            return WildfireSmokeScenario()
        if entry.scenario == "rush_hour_no2":
            return RushHourNo2Scenario()
        if entry.scenario == "sensor_fault":
            return SensorFaultScenario(
                fault_kind=entry.fault_kind or "dropout", targets=entry.targets or set()
            )
        return CleanScenario()

    def _entry_modifier(
        self, entry: ScheduleEntry, site_code: str, when: dt.datetime, ctx: ScenarioContext
    ) -> ScenarioModifier:
        scenario = self._build(entry)
        if isinstance(scenario, SensorFaultScenario):
            raw = scenario.modifier_for(site_code, ctx)
        else:
            raw = scenario.modifier(ctx)
        # apply the recovery taper toward the identity
        factor = _taper(when, entry.end, self._recovery_h)
        if factor >= 1.0:
            return raw
        return ScenarioModifier(
            pm25_multiplier=1.0 + (raw.pm25_multiplier - 1.0) * factor,
            pm25_add=raw.pm25_add * factor,
            no2_multiplier=1.0 + (raw.no2_multiplier - 1.0) * factor,
            fault=raw.fault if factor > 0 else None,
        )

    def resolve(
        self, site_code: str, when: dt.datetime, classification: str, in_rush_window: bool
    ) -> ScenarioModifier:
        """The composed modifier for one sensor at one instant."""
        pm_mult, pm_add, no2_mult, fault = 1.0, 0.0, 1.0, None
        for entry in self._covering(site_code, when):
            elapsed = (when - entry.start).total_seconds() / 3600.0
            ctx = ScenarioContext(
                elapsed_hours=elapsed,
                classification=classification,
                in_rush_window=in_rush_window,
            )
            mod = self._entry_modifier(entry, site_code, when, ctx)
            pm_mult *= mod.pm25_multiplier
            pm_add += mod.pm25_add
            no2_mult *= mod.no2_multiplier
            if mod.fault is not None:
                fault = mod.fault  # highest-precedence covering fault wins
        return ScenarioModifier(
            pm25_multiplier=pm_mult, pm25_add=pm_add, no2_multiplier=no2_mult, fault=fault
        )

    def applied_scenarios(
        self, site_code: str, when: dt.datetime, classification: str, in_rush_window: bool
    ) -> list[str]:
        """Names of scenarios materially covering this sensor/instant (diagnostics)."""
        return [e.scenario for e in self._covering(site_code, when)]
