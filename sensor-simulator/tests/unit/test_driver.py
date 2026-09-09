"""Unit tests for the real-time and backfill drivers (task 16.6).

Both drivers run the SAME PublishPipeline; they differ only in the injected
clock (engineering-practices §2).

- Backfill (Req 12.3): produce one batch of records per Publish_Interval whose
  start is in [start, end), in non-decreasing DateTime order, without waiting.
- Real-time (Req 12.1/12.7): emit each interval within 5s of its boundary,
  DateTime aligned to the interval start. Tested with a fake clock — no sleep.
"""

from __future__ import annotations

import datetime as dt

from aqm_simulator.geography.profiles import get_profile
from aqm_simulator.pipeline.driver import backfill, real_time
from aqm_simulator.pipeline.publish import PublishPipeline
from aqm_simulator.rng.streams import RandomStreamFactory
from aqm_simulator.scenarios.engine import ScenarioEngine
from aqm_simulator.signal.faults import FaultController
from aqm_simulator.swarm.factory import build_swarm


def _pipeline(seed: int = 3) -> PublishPipeline:
    profile = get_profile("cochabamba")
    factory = RandomStreamFactory(seed=seed)
    swarm = build_swarm(size=2, profile=profile, factory=factory)
    return PublishPipeline(
        swarm=swarm,
        profile=profile,
        factory=factory,
        scenario_engine=ScenarioEngine(entries=[], swarm_site_codes={s.site_code for s in swarm}),
        fault_controller=FaultController(windows=[]),
        publish_minutes=60,
    )


def test_backfill_covers_every_interval_in_range() -> None:
    start = dt.datetime(2026, 7, 1, 0, tzinfo=dt.UTC)
    end = dt.datetime(2026, 7, 1, 3, tzinfo=dt.UTC)  # 3 hourly intervals
    records = list(backfill(_pipeline(), start=start, end=end, reference_time=end))
    datetimes = sorted({r.DateTime for r in records})
    assert datetimes == [
        "2026-07-01T00:00:00Z",
        "2026-07-01T01:00:00Z",
        "2026-07-01T02:00:00Z",
    ]


def test_backfill_non_decreasing_order() -> None:
    start = dt.datetime(2026, 7, 1, 0, tzinfo=dt.UTC)
    end = dt.datetime(2026, 7, 1, 4, tzinfo=dt.UTC)
    records = list(backfill(_pipeline(), start=start, end=end, reference_time=end))
    times = [r.DateTime for r in records]
    assert times == sorted(times)  # non-decreasing DateTime order


def test_backfill_end_exclusive() -> None:
    start = dt.datetime(2026, 7, 1, 0, tzinfo=dt.UTC)
    end = dt.datetime(2026, 7, 1, 1, tzinfo=dt.UTC)  # only the 00:00 interval
    records = list(backfill(_pipeline(), start=start, end=end, reference_time=end))
    assert {r.DateTime for r in records} == {"2026-07-01T00:00:00Z"}


def test_real_time_emits_bounded_intervals() -> None:
    # a fake clock that yields two interval boundaries then stops
    start = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
    records = list(
        real_time(_pipeline(), start=start, max_intervals=2, reference_time=start)
    )
    assert {r.DateTime for r in records} == {
        "2026-07-01T12:00:00Z",
        "2026-07-01T13:00:00Z",
    }


def test_backfill_and_real_time_agree_on_first_interval() -> None:
    start = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
    bf = list(backfill(_pipeline(), start=start, end=start + dt.timedelta(hours=1),
                       reference_time=start))
    rt = list(real_time(_pipeline(), start=start, max_intervals=1, reference_time=start))
    bf_bytes = [r.model_dump_json() for r in bf]
    rt_bytes = [r.model_dump_json() for r in rt]
    assert bf_bytes == rt_bytes
