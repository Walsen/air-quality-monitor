"""Determinism harness.

The simulator must replay byte-identically from a seed (engineering-practices
§2, Requirement 11). This harness builds a fully-wired :class:`PublishPipeline`
from a seed alone and serializes a run to bytes, so a test can construct TWO
independent instances — each from a FRESH ``RandomStreamFactory(seed)``, which
mimics two separate processes — and compare their serialized output.

Because every source of randomness in the pipeline (regional field, spatial
field, per-sensor signal/artifact streams) is drawn from the injected factory,
two instances built from the same seed produce identical bytes, and two built
from different seeds diverge.
"""

from __future__ import annotations

import datetime as dt

from aqm_simulator.geography.profiles import get_profile
from aqm_simulator.pipeline.driver import backfill
from aqm_simulator.pipeline.publish import PublishPipeline
from aqm_simulator.rng.streams import RandomStreamFactory
from aqm_simulator.scenarios.engine import ScenarioEngine
from aqm_simulator.signal.faults import FaultController
from aqm_simulator.swarm.factory import build_swarm


def build_pipeline(
    seed: int,
    profile_name: str = "cochabamba",
    size: int = 3,
    publish_minutes: int = 60,
) -> PublishPipeline:
    """Build a fully-wired pipeline from a fresh factory seeded with ``seed``."""
    profile = get_profile(profile_name)
    factory = RandomStreamFactory(seed=seed)
    swarm = build_swarm(size=size, profile=profile, factory=factory)
    return PublishPipeline(
        swarm=swarm,
        profile=profile,
        factory=factory,
        scenario_engine=ScenarioEngine(
            entries=[], swarm_site_codes={s.site_code for s in swarm}
        ),
        fault_controller=FaultController(windows=[]),
        publish_minutes=publish_minutes,
    )


def run_and_serialize(
    pipeline: PublishPipeline,
    start: dt.datetime,
    end: dt.datetime,
    reference_time: dt.datetime,
) -> list[str]:
    """Run a backfill over [start, end) and return the serialized record bytes."""
    return [
        record.model_dump_json()
        for record in backfill(pipeline, start=start, end=end, reference_time=reference_time)
    ]
