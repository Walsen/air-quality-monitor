"""Scenario schedule validation (Requirement 10.11).

Validates a loaded schedule fail-fast: every entry is checked and every
violation reported (one message per offending entry naming the entry and the
constraint), so the Config_Loader can reject with a non-zero status and the
Simulator begins no signal generation. Mirrors the accumulate-all-problems
approach of the Config_Loader (§5).
"""

from __future__ import annotations

from aqm_simulator.scenarios.engine import ScheduleEntry
from aqm_simulator.scenarios.registry import SUPPORTED_SCENARIOS

_MAX_ENTRIES = 100  # Requirement 10.6 / 15.4
_MAX_TARGETS = 500
_FAULT_KINDS = frozenset({"stuck value", "accelerated drift", "dropout"})


def validate_schedule(entries: list[ScheduleEntry], swarm_site_codes: set[str]) -> list[str]:
    """Return a list of problems; empty means the schedule is valid."""
    problems: list[str] = []

    if len(entries) > _MAX_ENTRIES:
        problems.append(
            f"schedule has {len(entries)} entries, exceeding the {_MAX_ENTRIES} maximum"
        )

    for i, entry in enumerate(entries):
        if entry.scenario not in SUPPORTED_SCENARIOS:
            problems.append(
                f"entry {i}: unknown scenario {entry.scenario!r}; "
                f"supported: {', '.join(SUPPORTED_SCENARIOS)}"
            )
        if entry.end <= entry.start:
            problems.append(
                f"entry {i} ({entry.scenario}): end {entry.end.isoformat()} is at or "
                f"before start {entry.start.isoformat()}"
            )
        if entry.scenario == "sensor_fault" and entry.fault_kind not in _FAULT_KINDS:
            problems.append(
                f"entry {i}: unknown fault type {entry.fault_kind!r}; "
                f"one of {', '.join(sorted(_FAULT_KINDS))}"
            )
        if entry.targets is not None:
            if len(entry.targets) > _MAX_TARGETS:
                problems.append(
                    f"entry {i} ({entry.scenario}): {len(entry.targets)} targets exceed "
                    f"the {_MAX_TARGETS} maximum"
                )
            unknown = sorted(entry.targets - swarm_site_codes)
            for code in unknown:
                problems.append(
                    f"entry {i} ({entry.scenario}): target SiteCode {code!r} is not in the Swarm"
                )

    return problems
