"""Unit tests for the per-interval publish reporter (task 16.13).

Requirement 17.2/17.3: one INFO JSON event per Publish_Interval carrying the
generated / published / buffered / dropped counts, plus the interval DateTime.
Requirement 17.6: a WARNING event when records were generated but none published.
Requirement 17.7 / §6: every event is a single-line JSON object; nothing
non-JSON reaches stdout.
"""

from __future__ import annotations

import json
from typing import Any

from aqm_simulator.observability.logging import configure_logging, get_logger
from aqm_simulator.pipeline.reporter import IntervalReport, report_interval


def _lines(capsys) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    out = capsys.readouterr().out.strip().splitlines()
    return [json.loads(line) for line in out if line]


def test_publish_interval_summary_is_info_json(capsys) -> None:  # type: ignore[no-untyped-def]
    configure_logging("info")
    logger = get_logger("pipeline")
    report_interval(
        logger,
        IntervalReport(
            date_time="2026-07-01T12:00:00Z",
            generated=8, published=8, buffered=0, dropped=0,
        ),
    )
    events = _lines(capsys)
    assert len(events) == 1
    e = events[0]
    assert e["level"] == "info"
    assert e["event"] == "publish_interval"
    assert e["DateTime"] == "2026-07-01T12:00:00Z"
    assert e["generated"] == 8
    assert e["published"] == 8
    assert e["buffered"] == 0
    assert e["dropped"] == 0


def test_stall_emits_warning(capsys) -> None:  # type: ignore[no-untyped-def]
    configure_logging("info")
    logger = get_logger("pipeline")
    report_interval(
        logger,
        IntervalReport(
            date_time="2026-07-01T13:00:00Z",
            generated=8, published=0, buffered=0, dropped=8,
        ),
    )
    events = _lines(capsys)
    # a summary plus a stall warning
    warn = [e for e in events if e["level"] == "warn"]
    assert len(warn) == 1
    assert warn[0]["event"] == "publish_stalled"
    assert warn[0]["generated"] == 8
    assert warn[0]["published"] == 0


def test_no_stall_when_published(capsys) -> None:  # type: ignore[no-untyped-def]
    configure_logging("info")
    logger = get_logger("pipeline")
    report_interval(
        logger,
        IntervalReport(
            date_time="2026-07-01T14:00:00Z",
            generated=8, published=8, buffered=0, dropped=0,
        ),
    )
    assert not [e for e in _lines(capsys) if e["level"] == "warn"]


def test_every_line_is_valid_json(capsys) -> None:  # type: ignore[no-untyped-def]
    configure_logging("info")
    logger = get_logger("pipeline")
    report_interval(
        logger,
        IntervalReport("2026-07-01T15:00:00Z", generated=4, published=0, buffered=0, dropped=4),
    )
    raw = capsys.readouterr().out.strip().splitlines()
    for line in raw:
        json.loads(line)  # raises if any line is not JSON


def test_backfill_reports_one_summary_per_interval(capsys) -> None:  # type: ignore[no-untyped-def]
    import datetime as dt

    from aqm_simulator.geography.profiles import get_profile
    from aqm_simulator.pipeline.driver import backfill
    from aqm_simulator.pipeline.publish import PublishPipeline
    from aqm_simulator.rng.streams import RandomStreamFactory
    from aqm_simulator.scenarios.engine import ScenarioEngine
    from aqm_simulator.signal.faults import FaultController

    configure_logging("info")
    logger = get_logger("pipeline")
    profile = get_profile("cochabamba")
    factory = RandomStreamFactory(seed=1)
    from aqm_simulator.swarm.factory import build_swarm

    swarm = build_swarm(size=2, profile=profile, factory=factory)
    pipe = PublishPipeline(
        swarm=swarm, profile=profile, factory=factory,
        scenario_engine=ScenarioEngine(entries=[], swarm_site_codes={s.site_code for s in swarm}),
        fault_controller=FaultController(windows=[]), publish_minutes=60,
    )
    start = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    list(backfill(pipe, start=start, end=start + dt.timedelta(hours=3),
                  reference_time=start, logger=logger))
    summaries = [e for e in _lines(capsys)
                 if e.get("event") == "publish_interval"]
    assert len(summaries) == 3  # one per hourly interval
    assert all(e["generated"] == 8 for e in summaries)  # 2 sensors x 4 species
