"""The template-method publish pipeline.

For each Virtual_Sensor and each completed Publish_Interval, the pipeline runs a
FIXED sequence of pluggable steps (engineering-practices §4, Template Method):

    tick  →  average Tick values over the interval  →  apply scenario modifier
    →  apply sensor artifacts (noise, drift)  →  humidity growth (PM2.5)
    →  clamp  →  derive index species  →  build records

A dropout fault omits every record for that sensor and interval, which also
drops the paired index records (Requirements 7.3, 6.8). Each concentration
record is paired with exactly one index record carrying its identity fields
(Requirements 6.1, 6.2). ``Duration`` matches the configured Publish_Interval.

Every source of time and randomness is injected (§2): the clock drives which
interval runs; the per-sensor streams drive signals and artifacts. The pipeline
computes an interval by averaging the per-minute Tick values, so real-time and
backfill differ only in what advances the clock.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from aqm_simulator.contract.ratification import derive_ratification_status
from aqm_simulator.contract.records import SensorDataRecord, SpeciesName
from aqm_simulator.geography.profiles import GeographyProfile
from aqm_simulator.rng.streams import Purpose, RandomStreamFactory
from aqm_simulator.scenarios.engine import ScenarioEngine
from aqm_simulator.signal.artifacts import SensorArtifacts
from aqm_simulator.signal.faults import FaultController
from aqm_simulator.signal.humidity import HumidityArtifact
from aqm_simulator.signal.index_bands import default_breakpoint_table, derive_index_band
from aqm_simulator.signal.meteorology import MeteorologyEngine, MeteorologyRanges
from aqm_simulator.signal.pollutants import NO2Signal, PM25Signal
from aqm_simulator.signal.regional_field import RegionalField
from aqm_simulator.signal.spatial_field import SpatialField
from aqm_simulator.swarm.factory import VirtualSensor

_EPOCH_START = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
_SPATIAL_AMP = 4.0
_RUSH_LOCAL_HOURS = frozenset({7, 8, 9, 17, 18, 19})


@dataclass(frozen=True, slots=True)
class _SensorBundle:
    """The per-sensor signal/artifact objects the pipeline drives (typed)."""

    sensor: VirtualSensor
    no2: NO2Signal
    pm25: PM25Signal
    met: MeteorologyEngine
    artifacts: SensorArtifacts


def _duration_iso(minutes: int) -> str:
    if minutes % 60 == 0:
        return f"PT{minutes // 60}H"
    return f"PT{minutes}M"


class PublishPipeline:
    """Runs the fixed publish sequence for a swarm over Publish_Intervals."""

    def __init__(
        self,
        swarm: list[VirtualSensor],
        profile: GeographyProfile,
        factory: RandomStreamFactory,
        scenario_engine: ScenarioEngine,
        fault_controller: FaultController,
        publish_minutes: int = 60,
        reference_start: dt.datetime = _EPOCH_START,
    ) -> None:
        self._swarm = swarm
        self._profile = profile
        self._publish_minutes = publish_minutes
        self._scenarios = scenario_engine
        self._faults = fault_controller
        self._regional = RegionalField.from_profile(profile, factory)
        self._spatial = SpatialField.build(factory, amplitude=_SPATIAL_AMP)
        self._met_ranges = MeteorologyRanges.from_profile(profile)
        self._humidity = HumidityArtifact()
        self._index_table = default_breakpoint_table()
        # per-sensor signal/artifact bundle, built once (typed, not dict[str, object])
        self._bundles: dict[str, _SensorBundle] = {}
        for s in swarm:
            self._bundles[s.site_code] = _SensorBundle(
                sensor=s,
                no2=NO2Signal(
                    classification=s.classification,
                    timezone=profile.timezone,
                    rng=factory.stream(s.site_code, Purpose.SIGNAL),
                ),
                pm25=PM25Signal(
                    regional=self._regional,
                    site_code=s.site_code,
                    factory=factory,
                    latitude=float(s.latitude),
                    longitude=float(s.longitude),
                    spatial=self._spatial,
                ),
                met=MeteorologyEngine(
                    ranges=self._met_ranges,
                    timezone=profile.timezone,
                    rng=factory.stream(s.site_code, Purpose.METEOROLOGY),
                ),
                artifacts=SensorArtifacts(
                    site_code=s.site_code,
                    rng=factory.stream(s.site_code, Purpose.ARTIFACTS),
                    start=reference_start,
                ),
            )

    def _interval_mean(self, site_code: str, start: dt.datetime, species: str) -> float:
        """Mean of the per-minute Tick values across the Publish_Interval."""
        bundle = self._bundles[site_code]
        total = 0.0
        n = self._publish_minutes
        for minute in range(n):
            tick = start + dt.timedelta(minutes=minute)
            if species == "NO2":
                total += bundle.no2.value(tick)
            else:  # PM25 dry
                total += bundle.pm25.dry_value(tick)
        return total / n

    def _record(
        self,
        sensor: VirtualSensor,
        species: SpeciesName,
        value: float,
        start: dt.datetime,
        reference_time: dt.datetime,
    ) -> SensorDataRecord:
        index = species.endswith("Index")
        return SensorDataRecord(
            Species=species,
            Source="Measurement",
            Units="index" if index else "ug.m-3",
            SiteCode=sensor.site_code,
            DateTime=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            Duration=_duration_iso(self._publish_minutes),
            ScaledValue=value,
            RatificationStatus=derive_ratification_status(start, reference_time),
            SensorContract=self._profile.sensor_contract,
        )

    @property
    def publish_minutes(self) -> int:
        """The configured Publish_Interval length in minutes."""
        return self._publish_minutes

    def run_interval(
        self, start: dt.datetime, reference_time: dt.datetime
    ) -> list[SensorDataRecord]:
        """Produce all records for one Publish_Interval across the swarm."""
        records: list[SensorDataRecord] = []
        local_hour = start.astimezone(ZoneInfo(self._profile.timezone)).hour
        in_rush = local_hour in _RUSH_LOCAL_HOURS

        for sensor in self._swarm:
            decision = self._faults.decide(sensor.site_code, start)
            if not decision.emit:
                continue  # dropout: omit all four records (Req 7.3, 6.8)

            mod = self._scenarios.resolve(
                sensor.site_code, start, sensor.classification, in_rush
            )
            bundle = self._bundles[sensor.site_code]

            # NO2: interval mean x scenario multiplier + drift, clamped >= 0
            no2 = self._interval_mean(sensor.site_code, start, "NO2") * mod.no2_multiplier
            no2 += bundle.artifacts.drift("NO2", start)
            no2 = bundle.pm25.clamp("NO2", max(0.0, no2), sensor.site_code, start)

            # PM2.5: dry interval mean x scenario mult + additive lift, then humidity growth
            pm_dry = self._interval_mean(sensor.site_code, start, "PM25")
            pm_dry = pm_dry * mod.pm25_multiplier + mod.pm25_add
            rh = bundle.met.reading(start).relative_humidity_pct
            pm = self._humidity.apply(dry=pm_dry, rh=rh, site_code=sensor.site_code, when=start)
            pm += bundle.artifacts.drift("PM25", start)
            pm = bundle.pm25.clamp("PM25", max(0.0, pm), sensor.site_code, start)

            pm_idx, _ = derive_index_band(self._index_table, "PM25", pm)
            no2_idx, _ = derive_index_band(self._index_table, "NO2", no2)

            records.append(self._record(sensor, "PM25", round(pm, 2), start, reference_time))
            records.append(self._record(sensor, "NO2", round(no2, 2), start, reference_time))
            records.append(self._record(sensor, "PM25Index", float(pm_idx), start, reference_time))
            records.append(self._record(sensor, "NO2Index", float(no2_idx), start, reference_time))
        return records
