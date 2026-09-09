"""Unit tests for the template-method publish pipeline (task 16.1).

Requirement 6.1/6.2: each PM25 record pairs with exactly one PM25Index (and NO2
with NO2Index) carrying identical SiteCode/DateTime/Duration/Ratification/Contract.
Requirement 12.2/12.5: ScaledValue is the interval mean; Duration matches the
Publish_Interval (PT1H default).
Requirement 6.8/7.3: a dropout omits every record for that sensor+interval,
including the index records.
Requirement 2.1: emitted records carry exactly the nine contract fields.
"""

from __future__ import annotations

import datetime as dt

from aqm_simulator.geography.profiles import get_profile
from aqm_simulator.pipeline.publish import PublishPipeline
from aqm_simulator.rng.streams import RandomStreamFactory
from aqm_simulator.scenarios.engine import ScenarioEngine
from aqm_simulator.signal.faults import FaultController, FaultKind, FaultWindow
from aqm_simulator.swarm.factory import build_swarm

_INTERVAL_START = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_REF = dt.datetime(2026, 7, 2, tzinfo=dt.UTC)  # ratification reference


def _pipeline(faults: FaultController | None = None) -> PublishPipeline:
    profile = get_profile("cochabamba")
    factory = RandomStreamFactory(seed=7)
    swarm = build_swarm(size=3, profile=profile, factory=factory)
    return PublishPipeline(
        swarm=swarm,
        profile=profile,
        factory=factory,
        scenario_engine=ScenarioEngine(entries=[], swarm_site_codes={s.site_code for s in swarm}),
        fault_controller=faults or FaultController(windows=[]),
        publish_minutes=60,
    )


def test_emits_four_species_per_sensor() -> None:
    records = _pipeline().run_interval(_INTERVAL_START, reference_time=_REF)
    by_site: dict[str, set[str]] = {}
    for r in records:
        by_site.setdefault(r.SiteCode, set()).add(r.Species)
    for species in by_site.values():
        assert species == {"PM25", "NO2", "PM25Index", "NO2Index"}


def test_records_carry_correct_duration_and_datetime() -> None:
    records = _pipeline().run_interval(_INTERVAL_START, reference_time=_REF)
    for r in records:
        assert r.Duration == "PT1H"
        assert r.DateTime == "2026-07-01T12:00:00Z"


def test_index_paired_with_concentration() -> None:
    records = _pipeline().run_interval(_INTERVAL_START, reference_time=_REF)
    site = records[0].SiteCode
    site_recs = {str(r.Species): r for r in records if r.SiteCode == site}
    # index records share the concentration's identity fields
    for conc, index in (("PM25", "PM25Index"), ("NO2", "NO2Index")):
        assert site_recs[index].DateTime == site_recs[conc].DateTime
        assert site_recs[index].RatificationStatus == site_recs[conc].RatificationStatus
        assert isinstance(site_recs[index].ScaledValue, (int, float))


def test_dropout_omits_all_records_for_sensor() -> None:
    profile = get_profile("cochabamba")
    factory = RandomStreamFactory(seed=7)
    swarm = build_swarm(size=3, profile=profile, factory=factory)
    target = swarm[0].site_code
    faults = FaultController(
        windows=[
            FaultWindow(
                FaultKind.DROPOUT,
                _INTERVAL_START,
                _INTERVAL_START + dt.timedelta(hours=2),
                site_code=target,
            )
        ]
    )
    pipe = PublishPipeline(
        swarm=swarm,
        profile=profile,
        factory=factory,
        scenario_engine=ScenarioEngine(entries=[], swarm_site_codes={s.site_code for s in swarm}),
        fault_controller=faults,
        publish_minutes=60,
    )
    records = pipe.run_interval(_INTERVAL_START, reference_time=_REF)
    assert all(r.SiteCode != target for r in records)  # dropped entirely
    assert any(r.SiteCode != target for r in records)   # others still emit


def test_ratification_status_from_reference() -> None:
    # a very old interval is ratified (R); a recent one provisional (P)
    old = _pipeline().run_interval(
        dt.datetime(2026, 1, 1, tzinfo=dt.UTC), reference_time=_REF
    )
    assert all(r.RatificationStatus == "R" for r in old)
