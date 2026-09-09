"""Unit tests for scenario schedule validation (task 14.8).

Requirement 10.11: reject an entry with an unrecognized scenario name, an
unrecognized fault type, a SiteCode absent from the Swarm, an end at or before
its start, or more entries/targets than the configured maximum — one message
per offending entry naming the entry and the violated constraint; no signals.
"""

from __future__ import annotations

import datetime as dt

from aqm_simulator.scenarios.engine import ScheduleEntry
from aqm_simulator.scenarios.validation import validate_schedule

_SWARM = {"CB0001", "CB0002"}


def _entry(**over: object) -> ScheduleEntry:
    base: dict[str, object] = {
        "scenario": "wildfire_smoke",
        "start": dt.datetime(2026, 7, 1, 3, tzinfo=dt.UTC),
        "end": dt.datetime(2026, 7, 1, 6, tzinfo=dt.UTC),
    }
    base.update(over)
    return ScheduleEntry(**base)  # type: ignore[arg-type]


def test_valid_schedule_passes() -> None:
    assert validate_schedule([_entry()], _SWARM) == []


def test_unknown_scenario_name_reported() -> None:
    problems = validate_schedule([_entry(scenario="dust_storm")], _SWARM)
    assert any("dust_storm" in p for p in problems)


def test_unknown_fault_type_reported() -> None:
    entry = _entry(scenario="sensor_fault", targets={"CB0001"}, fault_kind="explode")
    problems = validate_schedule([entry], _SWARM)
    assert any("explode" in p for p in problems)


def test_unknown_sitecode_reported() -> None:
    problems = validate_schedule([_entry(targets={"CB9999"})], _SWARM)
    assert any("CB9999" in p for p in problems)


def test_end_at_or_before_start_reported() -> None:
    bad = _entry(
        start=dt.datetime(2026, 7, 1, 6, tzinfo=dt.UTC),
        end=dt.datetime(2026, 7, 1, 3, tzinfo=dt.UTC),
    )
    problems = validate_schedule([bad], _SWARM)
    assert any("start" in p.lower() or "end" in p.lower() for p in problems)


def test_over_max_entries_reported() -> None:
    many = [_entry() for _ in range(101)]
    problems = validate_schedule(many, _SWARM)
    assert any("100" in p for p in problems)


def test_over_max_targets_reported() -> None:
    big = _entry(targets={f"CB{i:04d}" for i in range(1, 502)})
    problems = validate_schedule([big], _SWARM | {f"CB{i:04d}" for i in range(1, 502)})
    assert any("500" in p for p in problems)


def test_all_offending_entries_reported() -> None:
    problems = validate_schedule(
        [_entry(scenario="dust_storm"), _entry(targets={"CB9999"})], _SWARM
    )
    assert len(problems) >= 2
