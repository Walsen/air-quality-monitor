"""Unit tests for the Swarm_Manager (tasks 12.6, 12.7).

Requirement 8.1: instantiate exactly the configured number of sensors (1-500)
before the first tick.
Requirement 17.4: if a tick raises for one sensor, log it at error with the
SiteCode, operation, and error type, continue with the remaining sensors within
the same interval, and leave the process running.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from aqm_simulator.geography.profiles import get_profile
from aqm_simulator.observability.logging import configure_logging
from aqm_simulator.rng.streams import RandomStreamFactory
from aqm_simulator.swarm.manager import SwarmManager


def _manager(size: int = 5, seed: int = 7) -> SwarmManager:
    profile = get_profile("cochabamba")
    return SwarmManager(size=size, profile=profile, factory=RandomStreamFactory(seed=seed))


def test_instantiates_all_sensors_in_order() -> None:
    mgr = _manager(size=10)
    codes = [s.site_code for s in mgr.sensors]
    assert len(codes) == 10
    assert codes == sorted(codes)


def test_tick_all_invokes_each_sensor() -> None:
    mgr = _manager(size=5)
    seen: list[str] = []
    ts = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
    mgr.tick_all(ts, tick_fn=lambda sensor, when: seen.append(sensor.site_code))
    assert sorted(seen) == sorted(s.site_code for s in mgr.sensors)


def test_one_sensor_failure_isolated_others_continue(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="error")
    mgr = _manager(size=5)
    failing = mgr.sensors[2].site_code
    succeeded: list[str] = []

    def tick_fn(sensor: object, when: dt.datetime) -> None:
        if sensor.site_code == failing:  # type: ignore[attr-defined]
            raise ValueError("boom")
        succeeded.append(sensor.site_code)  # type: ignore[attr-defined]

    ts = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
    # must NOT raise — the swarm carries on and the process survives
    mgr.tick_all(ts, tick_fn=tick_fn)

    assert len(succeeded) == 4  # the other four still ticked
    assert failing not in succeeded

    # the failure is logged once at error with SiteCode, operation, error type
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    error_lines = [json.loads(ln) for ln in lines]
    tick_errors = [e for e in error_lines if e.get("event") == "sensor_tick_failed"]
    assert len(tick_errors) == 1
    assert tick_errors[0]["site_code"] == failing
    assert tick_errors[0]["error_type"] == "ValueError"


def teardown_function() -> None:
    import logging

    logging.getLogger("aqm_simulator").handlers.clear()
