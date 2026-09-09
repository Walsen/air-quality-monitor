"""Unit tests for the ScenarioEngine (task 14.4).

Requirement 10.6: schedule of entries (name, ISO start/end, optional targets).
Requirement 10.7: overlapping windows compose in precedence order; applied names
and affected SiteCodes recorded in diagnostics.
Requirement 10.9: uncovered timestamp -> identity (clean baseline).
Requirement 10.10: after a window ends, the contribution tapers to zero over the
recovery duration (default 2h).
"""

from __future__ import annotations

import datetime as dt

from aqm_simulator.scenarios.engine import ScenarioEngine, ScheduleEntry


def _ts(hour: int) -> dt.datetime:
    return dt.datetime(2026, 7, 1, hour, tzinfo=dt.UTC)


def _engine(entries: list[ScheduleEntry]) -> ScenarioEngine:
    return ScenarioEngine(entries=entries, swarm_site_codes={"CB0001", "CB0002"})


def test_uncovered_timestamp_is_identity() -> None:
    eng = _engine([ScheduleEntry("wildfire_smoke", _ts(3), _ts(6))])
    mod = eng.resolve("CB0001", _ts(10), "Roadside", in_rush_window=False)
    assert mod.pm25_multiplier == 1.0 and mod.no2_multiplier == 1.0 and mod.pm25_add == 0.0


def test_covered_window_applies_scenario() -> None:
    eng = _engine([ScheduleEntry("wildfire_smoke", _ts(3), _ts(6))])
    mod = eng.resolve("CB0001", _ts(5), "Roadside", in_rush_window=False)
    assert mod.pm25_multiplier > 1.0  # wildfire lifts PM2.5


def test_targeted_entry_only_affects_targets() -> None:
    eng = _engine(
        [ScheduleEntry("wildfire_smoke", _ts(3), _ts(6), targets={"CB0001"})]
    )
    assert eng.resolve("CB0001", _ts(5), "Roadside", in_rush_window=False).pm25_multiplier > 1.0
    assert eng.resolve("CB0002", _ts(5), "Roadside", in_rush_window=False).pm25_multiplier == 1.0


def test_recovery_taper_after_window() -> None:
    eng = _engine([ScheduleEntry("wildfire_smoke", _ts(3), _ts(6))])
    at_end = eng.resolve("CB0001", _ts(6), "Roadside", in_rush_window=False).pm25_multiplier
    mid_recovery = eng.resolve(
        "CB0001",
        _ts(6) + dt.timedelta(hours=1),
        "Roadside",
        in_rush_window=False,
    ).pm25_multiplier
    after_recovery = eng.resolve(
        "CB0001",
        _ts(6) + dt.timedelta(hours=3),
        "Roadside",
        in_rush_window=False,
    ).pm25_multiplier
    # multiplier decays back toward 1.0 over the 2h recovery
    assert at_end >= mid_recovery >= after_recovery
    assert after_recovery == 1.0  # fully recovered past 2h


def test_precedence_composition_records_applied_names() -> None:
    eng = _engine(
        [
            ScheduleEntry("wildfire_smoke", _ts(3), _ts(9)),
            ScheduleEntry("rush_hour_no2", _ts(3), _ts(9)),
        ]
    )
    mod = eng.resolve("CB0001", _ts(5), "Roadside", in_rush_window=True)
    # both compose: PM2.5 lifted (wildfire) AND NO2 lifted (rush)
    assert mod.pm25_multiplier > 1.0
    assert mod.no2_multiplier >= 1.8
    applied = eng.applied_scenarios("CB0001", _ts(5), "Roadside", in_rush_window=True)
    assert set(applied) == {"wildfire_smoke", "rush_hour_no2"}


def test_sensor_fault_entry_targets_named_sensor() -> None:
    eng = _engine(
        [ScheduleEntry("sensor_fault", _ts(3), _ts(6), targets={"CB0001"}, fault_kind="dropout")]
    )
    mod = eng.resolve("CB0001", _ts(4), "Roadside", in_rush_window=False)
    assert mod.fault == "dropout"
    assert eng.resolve("CB0002", _ts(4), "Roadside", in_rush_window=False).fault is None
