"""Driver property tests (tasks 16.7-16.8).

Feature: sensor-simulator-service
- Property 34: backfill coverage (Req 12.3)
- Property 35: mode equivalence (Req 12.4)
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given, settings
from hypothesis import strategies as st

from aqm_simulator.geography.profiles import get_profile
from aqm_simulator.pipeline.driver import backfill, real_time
from aqm_simulator.pipeline.publish import PublishPipeline
from aqm_simulator.rng.streams import RandomStreamFactory
from aqm_simulator.scenarios.engine import ScenarioEngine
from aqm_simulator.signal.faults import FaultController
from aqm_simulator.swarm.factory import build_swarm

_BASE = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)


def _pipeline(seed: int) -> PublishPipeline:
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


@given(
    seed=st.integers(min_value=0, max_value=5_000),
    start_h=st.integers(min_value=0, max_value=20),
    span=st.integers(min_value=1, max_value=8),
)
@settings(max_examples=100)
def test_property_34_backfill_coverage(seed: int, start_h: int, span: int) -> None:
    """Feature: sensor-simulator-service, Property 34."""
    start = _BASE + dt.timedelta(hours=start_h)
    end = start + dt.timedelta(hours=span)
    records = list(backfill(_pipeline(seed), start=start, end=end, reference_time=end))
    datetimes = sorted({r.DateTime for r in records})
    expected = [
        (start + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for h in range(span)
    ]
    assert datetimes == expected  # exactly one interval per hour-start in [start, end)
    times = [r.DateTime for r in records]
    assert times == sorted(times)  # non-decreasing


@given(
    seed=st.integers(min_value=0, max_value=5_000),
    hour=st.integers(min_value=0, max_value=23),
)
@settings(max_examples=100)
def test_property_35_mode_equivalence(seed: int, hour: int) -> None:
    """Feature: sensor-simulator-service, Property 35."""
    t = _BASE + dt.timedelta(hours=hour)
    ref = _BASE + dt.timedelta(days=400)  # SAME reference in both modes
    bf = list(backfill(_pipeline(seed), start=t, end=t + dt.timedelta(hours=1),
                       reference_time=ref))
    rt = list(real_time(_pipeline(seed), start=t, max_intervals=1, reference_time=ref))
    # every contract field equal → identical serialized bytes, in the same order
    assert [r.model_dump_json() for r in bf] == [r.model_dump_json() for r in rt]
