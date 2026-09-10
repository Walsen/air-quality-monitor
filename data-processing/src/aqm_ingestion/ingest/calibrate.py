"""Resolving humidity and assembling a Calibrated_Reading.

This is the stage that turns a validated record into a corrected reading. It lives in
``ingest/`` rather than ``domain/`` for the same reason
:mod:`aqm_ingestion.ingest.quarantine` does: it reaches a PORT (the
MeteorologyProvider) and it LOGS. The correction arithmetic and the domain bounds stay
in :mod:`aqm_ingestion.domain.calibration`, which knows nothing of either.

Three conditions are deliberately kept distinct, because collapsing any pair would
misdescribe the reading:

- ``calibrated`` — a correction was applied with inputs inside the strategy's domain.
- ``calibrated_extrapolated`` — a correction was applied with an input outside it
  (Requirement 8.7). Still applied, because a refused reading serves nothing while an
  extrapolated one carries a value and a caveat.
- ``uncalibrated`` — no RH could be resolved at all, so the configured fallback ran
  (Requirement 8.6). This is NOT extrapolation: nothing was extrapolated, because no
  humidity correction was attempted.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, get_args

from aqm_ingestion.contract.records import SensorDataRecord, SpeciesName
from aqm_ingestion.domain.calibration import (
    DEFAULT_NO_HUMIDITY_STRATEGY,
    DEFAULT_SPECIES_STRATEGY,
    CalibrationRegistry,
)
from aqm_ingestion.domain.dedup import dedup_key_for
from aqm_ingestion.domain.models import (
    CalibratedReading,
    Confidence,
    HumiditySource,
    QualityFlag,
)
from aqm_ingestion.observability.logging import get_logger
from aqm_ingestion.ports.protocols import MetObservation

_logger = get_logger("ingest.calibrate")
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

MeteorologyChannel = Literal["rh", "temperature", "pressure"]
_CHANNELS: tuple[str, ...] = get_args(MeteorologyChannel)
_CONTRACT_SPECIES: frozenset[str] = frozenset(get_args(SpeciesName))


class ObservationSource(Protocol):
    """The one MeteorologyProvider method this stage needs (§1)."""

    def observation(self, site_code: str, at: dt.datetime) -> MetObservation | None:
        """Return the observation for a site at an instant, or None."""
        ...


@dataclass(frozen=True, slots=True)
class MeteorologyChannelMapping:
    """Maps a non-contract `Species` value to a meteorology channel (Req 8.5).

    Empty by default, so a deployment that publishes no meteorology channels behaves
    exactly as if the feature did not exist.
    """

    mapping: Mapping[str, MeteorologyChannel] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the configured mapping (§5 — fail at configuration time)."""
        for species, channel in self.mapping.items():
            if channel not in _CHANNELS:
                raise ValueError(
                    f"meteorology channel {channel!r} for Species {species!r} is not "
                    f"one of: {', '.join(_CHANNELS)}"
                )
            if species in _CONTRACT_SPECIES:
                raise ValueError(
                    f"Species {species!r} is a contract value, but Requirement 8.5 "
                    "maps only values outside the contract's permitted set; mapping a "
                    "contract Species would make it both a Reading and meteorology"
                )

    @property
    def is_empty(self) -> bool:
        """Whether any channel is configured."""
        return not self.mapping

    @property
    def species(self) -> frozenset[str]:
        """The mapped `Species` values, for the parser's Requirement 3.6 diversion."""
        return frozenset(self.mapping)

    def channel_for(self, species: str) -> MeteorologyChannel | None:
        """The channel a `Species` maps to, or None if it is not mapped."""
        return self.mapping.get(species)


@dataclass(frozen=True, slots=True)
class ChannelObservation:
    """One meteorology value delivered through the record channel (Req 8.5)."""

    site_code: str
    interval_start: dt.datetime
    channel: MeteorologyChannel
    value: float


@dataclass(frozen=True, slots=True)
class ResolvedHumidity:
    """The RH used for a correction, and where it came from (Requirement 8.11)."""

    value: float | None
    source: HumiditySource


def channel_observations(
    elements: Iterable[Mapping[str, object]], mapping: MeteorologyChannelMapping
) -> tuple[ChannelObservation, ...]:
    """Interpret parser-diverted elements as meteorology observations.

    The parser hands these over uninterpreted (Requirement 3.6); giving them meaning is
    this module's job, because the mapping that classified them lives here.

    An element whose `Species` is not mapped is skipped rather than raising: the parser
    diverts by the same mapping, so a mismatch means the caller mixed configurations,
    and silently ignoring an unmapped element is safer than aborting a whole batch (§5).
    """
    observations: list[ChannelObservation] = []
    for element in elements:
        species = element.get("Species")
        if not isinstance(species, str):
            continue
        channel = mapping.channel_for(species)
        if channel is None:
            continue
        site_code = element.get("SiteCode")
        moment = element.get("DateTime")
        value = element.get("ScaledValue")
        if (
            not isinstance(site_code, str)
            or not isinstance(moment, str)
            or not isinstance(value, (int, float))
            or isinstance(value, bool)
        ):
            continue
        observations.append(
            ChannelObservation(
                site_code=site_code,
                interval_start=dt.datetime.strptime(moment, _UTC_FORMAT).replace(
                    tzinfo=dt.UTC
                ),
                channel=channel,
                value=float(value),
            )
        )
    return tuple(observations)


def channel_value(
    observations: Sequence[ChannelObservation],
    channel: MeteorologyChannel,
    site_code: str,
    interval_start: dt.datetime,
) -> float | None:
    """Find one channel's value for a site and interval, or None.

    Shared by the humidity path (Requirement 8.4) and the temperature/pressure path
    (Requirement 9.3) rather than written once per channel (§1).

    BOTH the site and the interval must match. Borrowing another site's or another
    hour's meteorology would silently corrupt the result while still reporting it as
    measurement-backed, which is worse than having no measurement at all.
    """
    for observation in observations:
        if (
            observation.channel == channel
            and observation.site_code == site_code
            and observation.interval_start == interval_start
        ):
            return observation.value
    return None


def resolve_humidity(
    *,
    site_code: str,
    interval_start: dt.datetime,
    channel_observations: Sequence[ChannelObservation],
    provider: ObservationSource | None,
) -> ResolvedHumidity:
    """Resolve RH from the first available of three sources (Requirement 8.4).

    Order: a channel record for the SAME site and interval, then the
    MeteorologyProvider, then nothing.
    """
    from_channel = channel_value(channel_observations, "rh", site_code, interval_start)
    if from_channel is not None:
        return ResolvedHumidity(value=from_channel, source="channel")

    if provider is not None:
        observed = provider.observation(site_code, interval_start)
        # A provider may know temperature and not humidity, so an observation is not
        # itself an RH value (MetObservation's field is optional for that reason).
        if observed is not None and observed.relative_humidity_pct is not None:
            return ResolvedHumidity(
                value=observed.relative_humidity_pct, source="provider"
            )

    return ResolvedHumidity(value=None, source="none")


def calibrate_reading(
    *,
    record: SensorDataRecord,
    humidity: float | None,
    humidity_source: HumiditySource,
    registry: CalibrationRegistry,
    strategy_name: str | None,
    ingested_at: dt.datetime,
    archive_id: str,
    no_humidity_strategy: str = DEFAULT_NO_HUMIDITY_STRATEGY,
) -> CalibratedReading:
    """Apply calibration and assemble the reading.

    Args:
        record: the validated record.
        humidity: the resolved RH, or None if none could be resolved.
        humidity_source: where it came from (Requirement 8.11).
        registry: the strategy registry.
        strategy_name: the configured strategy, or None to take the per-species
            default of Requirement 8.12.
        ingested_at: the ingestion instant.
        archive_id: the archived payload this reading derives from.
        no_humidity_strategy: the Requirement 8.6 fallback.

    Returns:
        A reading retaining BOTH the reported and corrected values (Requirement 8.10)
        and recording the strategy and RH source (Requirement 8.11).
    """
    configured = strategy_name or DEFAULT_SPECIES_STRATEGY.get(
        record.Species, no_humidity_strategy
    )

    if humidity is None:
        # Requirement 8.6: no RH at all — run the fallback and say so. The reading is
        # uncalibrated rather than extrapolated: no correction was attempted, so there
        # is nothing to have extrapolated.
        strategy = registry.resolve(no_humidity_strategy)
        corrected = strategy.correct(reported=record.ScaledValue, rh=None)
        return _assemble(
            record=record,
            corrected=corrected,
            strategy_name=strategy.name,
            humidity_source=humidity_source,
            quality_flag=QualityFlag.UNCALIBRATED,
            confidence=Confidence.LOW,
            ingested_at=ingested_at,
            archive_id=archive_id,
        )

    strategy = registry.resolve(configured)
    breaches = strategy.domain.breaches(reported=record.ScaledValue, rh=humidity)
    corrected = strategy.correct(reported=record.ScaledValue, rh=humidity)

    if breaches:
        # ONE warning per reading even when both inputs are out of domain: a reading is
        # one operational fact, and the breaches travel together in the event (§6).
        _logger.warning(
            "calibration_out_of_domain",
            SiteCode=record.SiteCode,
            Species=record.Species,
            DateTime=record.DateTime,
            strategy=strategy.name,
            breaches=list(breaches),
        )

    return _assemble(
        record=record,
        corrected=corrected,
        strategy_name=strategy.name,
        humidity_source=humidity_source,
        quality_flag=(
            QualityFlag.CALIBRATED_EXTRAPOLATED if breaches else QualityFlag.CALIBRATED
        ),
        confidence=Confidence.MEDIUM if breaches else Confidence.HIGH,
        ingested_at=ingested_at,
        archive_id=archive_id,
    )


def _assemble(
    *,
    record: SensorDataRecord,
    corrected: float,
    strategy_name: str,
    humidity_source: HumiditySource,
    quality_flag: QualityFlag,
    confidence: Confidence,
    ingested_at: dt.datetime,
    archive_id: str,
) -> CalibratedReading:
    """Build the reading, retaining the reported value (Requirement 8.10)."""
    return CalibratedReading(
        key=dedup_key_for(record),
        reported_value=record.ScaledValue,
        corrected_value=corrected,
        units=record.Units,
        quality_flag=quality_flag,
        confidence=confidence,
        calibration_strategy=strategy_name,
        breakpoint_table="",  # set by the sub-index stage (task 9)
        ratification_status=record.RatificationStatus,
        ingested_at=ingested_at,
        archive_id=archive_id,
        humidity_source=humidity_source,
    )
