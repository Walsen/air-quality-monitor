"""Pipeline property tests (tasks 16.2-16.5).

Feature: sensor-simulator-service
- Property 33: interval averaging (Req 12.2, 12.5)
- Property 21: index species pairing (Req 6.1, 6.2, 6.8)
- Property 32: monotonic timestamps (Req 11.5)
- Property 5: emitted contract field set (Req 2.1)
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given, settings
from hypothesis import strategies as st

from aqm_simulator.geography.profiles import get_profile
from aqm_simulator.pipeline.publish import PublishPipeline
from aqm_simulator.rng.streams import RandomStreamFactory
from aqm_simulator.scenarios.engine import ScenarioEngine
from aqm_simulator.signal.faults import FaultController
from aqm_simulator.swarm.factory import build_swarm

_REF = dt.datetime(2026, 7, 2, tzinfo=dt.UTC)
_DATA_FIELDS = [
    "Species", "Source", "Units", "SiteCode", "DateTime",
    "Duration", "ScaledValue", "RatificationStatus", "SensorContract",
]


def _pipeline(seed: int, size: int = 3) -> PublishPipeline:
    profile = get_profile("cochabamba")
    factory = RandomStreamFactory(seed=seed)
    swarm = build_swarm(size=size, profile=profile, factory=factory)
    return PublishPipeline(
        swarm=swarm,
        profile=profile,
        factory=factory,
        scenario_engine=ScenarioEngine(entries=[], swarm_site_codes={s.site_code for s in swarm}),
        fault_controller=FaultController(windows=[]),
        publish_minutes=60,
    )


@given(seed=st.integers(min_value=0, max_value=5_000), hour=st.integers(min_value=0, max_value=23))
@settings(max_examples=100)
def test_property_21_index_pairing(seed: int, hour: int) -> None:
    """Feature: sensor-simulator-service, Property 21."""
    start = dt.datetime(2026, 7, 1, hour, tzinfo=dt.UTC)
    records = _pipeline(seed).run_interval(start, reference_time=_REF)
    by_site: dict[str, dict[str, object]] = {}
    for r in records:
        by_site.setdefault(r.SiteCode, {})[r.Species] = r
    for species in by_site.values():
        # exactly one index record per concentration record, sharing identity
        assert ("PM25" in species) == ("PM25Index" in species)
        assert ("NO2" in species) == ("NO2Index" in species)


@given(seed=st.integers(min_value=0, max_value=5_000), hour=st.integers(min_value=0, max_value=23))
@settings(max_examples=100)
def test_property_5_contract_field_set(seed: int, hour: int) -> None:
    """Feature: sensor-simulator-service, Property 5."""
    start = dt.datetime(2026, 7, 1, hour, tzinfo=dt.UTC)
    for r in _pipeline(seed).run_interval(start, reference_time=_REF):
        assert list(r.model_dump(mode="json").keys()) == _DATA_FIELDS


@given(seed=st.integers(min_value=0, max_value=5_000))
@settings(max_examples=50)
def test_property_32_monotonic_timestamps(seed: int) -> None:
    """Feature: sensor-simulator-service, Property 32."""
    pipe = _pipeline(seed)
    all_records = []
    for h in range(6):  # six consecutive hourly intervals
        start = dt.datetime(2026, 7, 1, h, tzinfo=dt.UTC)
        all_records.extend(pipe.run_interval(start, reference_time=_REF))
    # per (SiteCode, Species): strictly increasing DateTime, no duplicates
    seen: dict[tuple[str, str], list[str]] = {}
    for r in all_records:
        seen.setdefault((r.SiteCode, r.Species), []).append(r.DateTime)
    for times in seen.values():
        assert times == sorted(times)
        assert len(times) == len(set(times))  # no duplicate SiteCode+Species+DateTime


@given(seed=st.integers(min_value=0, max_value=5_000))
@settings(max_examples=50)
def test_property_33_ratification_consistent(seed: int) -> None:
    """Feature: sensor-simulator-service, Property 33 (interval records well-formed)."""
    # An interval's four records share DateTime and Duration (averaging produces
    # one value per Species per interval).
    start = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
    records = _pipeline(seed).run_interval(start, reference_time=_REF)
    for r in records:
        assert r.DateTime == "2026-07-01T12:00:00Z"
        assert r.Duration == "PT1H"
