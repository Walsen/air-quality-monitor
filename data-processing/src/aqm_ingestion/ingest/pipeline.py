"""The Ingest_Pipeline: the fixed sequence every payload passes through.

A TEMPLATE METHOD (§4), and the structural problem it solves is real rather than
decorative: the ORDER of these stages is itself a set of requirements, several of which
are unobservable from the result.

- The archive write must precede the first validation rule (Requirement 6.11), so a
  payload that fails every rule stays recoverable.
- Deduplication must precede calibration (Requirement 7.9), so a re-delivered record
  consumes no correction or meteorology work.
- Calibration must precede every index computation (Requirement 8.1), so nothing
  downstream sees the uncorrected value.

A quarantined record that was calibrated and then discarded looks identical from outside,
which is why the sequence is fixed here in one place, with the steps as overridable
methods, rather than left to each entry point to assemble correctly.

TRANSPORT-AGNOSTIC by construction: :meth:`IngestPipeline.ingest` takes bytes and an
:class:`ArchiveMeta`, never an MQTT message or an HTTP response, so the MQTT subscriber and
the feed poller share it unchanged.

WHY THE OVERALL AQI IS NOT STORED. Requirement 12.1 defines it as the maximum sub-index
across the species available at a site and instant — which changes as more records arrive
for that instant. A stored overall would therefore be correct when written and stale a
minute later, so it is computed here into the batch summary for observability and derived
authoritatively at serving time from the stored sub-indices.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from aqm_ingestion.contract.parser import ParseError, parse_data_records
from aqm_ingestion.contract.records import SensorDataRecord
from aqm_ingestion.domain.aqi.breakpoints import (
    DEFAULT_TABLE_ID,
    BreakpointTableRegistry,
)
from aqm_ingestion.domain.aqi.nowcast import (
    DEFAULT_NOWCAST_WINDOW_HOURS,
    compute_nowcast,
    supports_nowcast,
)
from aqm_ingestion.domain.aqi.overall import (
    DEFAULT_SPECIES_PRECEDENCE,
    OverallAqi,
    SpeciesContribution,
    compute_overall_aqi,
)
from aqm_ingestion.domain.aqi.subindex import compute_sub_index
from aqm_ingestion.domain.calibration import CalibrationRegistry
from aqm_ingestion.domain.conversion import ConversionUnavailableError, ppb_from_ug_m3
from aqm_ingestion.domain.dedup import dedup_key_for
from aqm_ingestion.domain.models import (
    CalibratedReading,
    QualityFlag,
)
from aqm_ingestion.domain.quality import (
    CalibrationOutcome,
    NowCastCoverage,
    QualityInputs,
    assess_quality,
)
from aqm_ingestion.domain.validation import ValidationLimits
from aqm_ingestion.ingest.archive import ArchiveOutcome, archive_payload
from aqm_ingestion.ingest.calibrate import (
    ChannelObservation,
    MeteorologyChannelMapping,
    calibrate_reading,
    channel_observations,
    resolve_humidity,
)
from aqm_ingestion.ingest.conditions import resolve_site_conditions
from aqm_ingestion.ingest.quarantine import screen_reading
from aqm_ingestion.ingest.registry_check import check_site_known
from aqm_ingestion.observability.logging import get_logger, log_handled_error
from aqm_ingestion.observability.metrics import MetricsRegistry
from aqm_ingestion.ports.clock import Clock
from aqm_ingestion.ports.protocols import (
    ArchiveMeta,
    MeteorologyProvider,
    RawArchive,
    ReadingsStore,
    SensorRegistryStore,
)

_logger = get_logger("ingest.pipeline")


@dataclass(frozen=True, slots=True)
class PipelineDependencies:
    """The ports the pipeline drives.

    Typed as the PROTOCOLS rather than as concrete adapters, so the pipeline depends on
    abstractions at every boundary (§1 Dependency Inversion) and the in-memory and cloud
    adapters are interchangeable without touching this module.
    """

    archive: RawArchive
    readings: ReadingsStore
    registry: SensorRegistryStore
    meteorology: MeteorologyProvider
    clock: Clock
    metrics: MetricsRegistry | None = None
    calibration: CalibrationRegistry = field(
        default_factory=CalibrationRegistry.with_defaults
    )
    breakpoints: BreakpointTableRegistry = field(
        default_factory=BreakpointTableRegistry.with_defaults
    )


@dataclass(frozen=True, slots=True)
class PipelineSettings:
    """The configured values the stages need — narrow, not a whole config object (§1)."""

    validation: ValidationLimits = field(default_factory=ValidationLimits)
    channel_mapping: MeteorologyChannelMapping = field(
        default_factory=MeteorologyChannelMapping
    )
    table_id: str = DEFAULT_TABLE_ID
    nowcast_window_hours: int = DEFAULT_NOWCAST_WINDOW_HOURS
    species_precedence: tuple[str, ...] = DEFAULT_SPECIES_PRECEDENCE


@dataclass(frozen=True, slots=True)
class BatchSummary:
    """What one payload produced (Requirement 29.5)."""

    received: int
    accepted: int
    deduplicated: int
    quarantined: int
    rejected: int
    per_quality_flag: Mapping[str, int]
    elapsed_seconds: float
    archive_id: str
    overall: Mapping[str, OverallAqi] = field(default_factory=dict)


class IngestPipeline:
    """Runs the fixed ingest sequence over one payload."""

    def __init__(
        self, dependencies: PipelineDependencies, settings: PipelineSettings
    ) -> None:
        """Hold the ports and the configured values."""
        self._deps = dependencies
        self._settings = settings

    @property
    def archive(self) -> RawArchive:
        """The archive port, for an entry point that must archive before rejecting.

        Requirement 4.3's topic/SiteCode mismatch is detected before the pipeline runs, and
        Requirement 6.11 still requires the payload to be archived — so the entry point needs
        the same archive this pipeline writes through, not a second one that could diverge.
        """
        return self._deps.archive

    def ingest(self, payload: bytes, meta: ArchiveMeta) -> BatchSummary:
        """Run the sequence.

        Args:
            payload: the bytes exactly as received.
            meta: the ingestion instant, transport, and source.

        Returns:
            The batch summary of Requirement 29.5.

        Raises:
            ArchiveWriteFailedError: if the archive write failed, in which case NOTHING is
                processed (Requirement 16.7) — the caller leaves the message unacknowledged
                or fails the poll.
        """
        started = self._now()

        # 1. Archive FIRST (Requirements 16.1, 6.11).
        outcome = archive_payload(self._deps.archive, payload, meta)

        if self._deps.metrics is not None:
            self._deps.metrics.record_ingested(transport=meta.transport)

        # 2. Parse. A whole-payload failure is not a per-record failure: there is nothing
        # to isolate, so it is reported as a batch that produced no records.
        try:
            parsed = parse_data_records(
                payload.decode("utf-8", errors="replace"),
                meteorology_species=self._settings.channel_mapping.species,
            )
        except ParseError as error:
            log_handled_error(
                _logger, "payload_unparseable", error, transport=meta.transport
            )
            return self._summarise(
                started, outcome.archive_id, received=0, rejected=1, results=[]
            )

        observations = channel_observations(
            parsed.meteorology, self._settings.channel_mapping
        )

        results: list[_RecordResult] = []
        for record in parsed.records:
            results.append(self._process_record(record, meta, outcome, observations))

        return self._summarise(
            started,
            outcome.archive_id,
            received=len(parsed.records),
            rejected=len(parsed.rejections),
            results=results,
        )

    def _process_record(
        self,
        record: SensorDataRecord,
        meta: ArchiveMeta,
        outcome: ArchiveOutcome,
        observations: Sequence[ChannelObservation],
    ) -> _RecordResult:
        """Run one record through the sequence, isolating its failures (§5).

        A per-record failure is caught and logged rather than propagated, so one bad record
        cannot cost a batch its accepted siblings — the same isolation the parser applies at
        Requirement 3.5 and the swarm applies per sensor.
        """
        try:
            return self._process(record, meta, outcome, observations)
        except (ValueError, KeyError, OSError, RuntimeError, ArithmeticError) as error:
            log_handled_error(
                _logger,
                "record_processing_failed",
                error,
                SiteCode=record.SiteCode,
                Species=record.Species,
                DateTime=record.DateTime,
            )
            return _RecordResult(kind="failed")

    def _process(
        self,
        record: SensorDataRecord,
        meta: ArchiveMeta,
        outcome: ArchiveOutcome,
        observations: Sequence[ChannelObservation],
    ) -> _RecordResult:
        """The fixed sequence for one record."""
        archive_id = outcome.archive_id
        now = self._now()

        # 3. Validate (Requirement 6). Archiving already happened, so a record failing
        # every rule is still recoverable.
        screened = screen_reading(
            record,
            now=now,
            limits=self._settings.validation,
            transport=meta.transport,
            archive_id=archive_id,
            metrics=self._deps.metrics,
        )
        if screened.accepted is None:
            return _RecordResult(kind="quarantined")

        # Requirement 6.7: an unknown site is kept, with one warning.
        check_site_known(record.SiteCode, self._deps.registry)

        # 4. Deduplicate BEFORE calibration (Requirement 7.9), so a re-delivery costs no
        # correction or meteorology work.
        if self._is_duplicate(record):
            return _RecordResult(kind="duplicate")

        # 5. Calibrate (Requirement 8.1) — before any index is computed.
        humidity = resolve_humidity(
            site_code=record.SiteCode,
            interval_start=dedup_key_for(record).interval_start,
            channel_observations=observations,
            provider=self._deps.meteorology,
        )
        reading = calibrate_reading(
            record=record,
            humidity=humidity.value,
            humidity_source=humidity.source,
            registry=self._deps.calibration,
            strategy_name=None,
            ingested_at=meta.ingested_at,
            archive_id=archive_id,
        )

        # 6-8. Convert, NowCast, sub-index.
        reading = self._index(reading, record, observations)

        # 9. Quality assessment, then store.
        reading = self._flag(reading, humidity_absent=humidity.value is None)
        self._deps.readings.put(reading)
        if self._deps.metrics is not None:
            self._deps.metrics.record_reading(quality_flag=reading.quality_flag.value)
        return _RecordResult(kind="accepted", reading=reading)

    def _is_duplicate(self, record: SensorDataRecord) -> bool:
        """Whether this record is already represented in the store.

        Compares what a ``CalibratedReading`` retains — the reported value and the
        ratification status — rather than every contract field, because the store does not
        keep the originating record. That is the expressible half of Requirement 7.3; the
        store's own write path applies the rest of Requirement 7's precedence, so a record
        that IS different still resolves correctly rather than being dropped here.
        """
        stored = self._deps.readings.get(dedup_key_for(record))
        if stored is None:
            return False
        return (
            stored.reported_value == record.ScaledValue
            and stored.ratification_status == record.RatificationStatus
        )

    def _index(
        self,
        reading: CalibratedReading,
        record: SensorDataRecord,
        observations: Sequence[ChannelObservation],
    ) -> CalibratedReading:
        """Convert if needed, apply NowCast if applicable, then compute the sub-index."""
        table = self._deps.breakpoints.get(self._settings.table_id, record.Species)
        if table is None:
            # Requirement 10.12: omit rather than approximate.
            return reading

        concentration = reading.corrected_value


        if table.unit == "ppb":
            # Requirement 9.1: the NO2 table is in ppb, so convert before applying it.
            conditions = resolve_site_conditions(
                site_code=record.SiteCode,
                interval_start=reading.key.interval_start,
                observations=observations,
                provider=self._deps.meteorology,
            )
            if conditions.conditions is None:
                # Requirement 9.9: no sub-index for this reading.
                return reading

            try:
                concentration = ppb_from_ug_m3(
                    concentration,
                    temperature_k=conditions.conditions.temperature_k,
                    pressure_pa=conditions.conditions.pressure_pa,
                )
            except ConversionUnavailableError:
                return reading
            reading = replace(
                reading,
                mixing_ratio_ppb=concentration,
                conversion_source=conditions.source,
                conversion_temperature_k=conditions.conditions.temperature_k,
                conversion_pressure_pa=conditions.conditions.pressure_pa,
            )
        elif supports_nowcast(record.Species):
            # Requirement 11.1: place the hourly value on the 24-hour scale.
            nowcast = compute_nowcast(
                self._hourly_series(reading),
                hourly_value=concentration,
                window_hours=self._settings.nowcast_window_hours,
            )
            concentration = nowcast.value

            reading = replace(
                reading,
                method="nowcast" if nowcast.used_nowcast else "hourly",
                nowcast_window_hours=nowcast.window_hours,
                nowcast_hours_available=nowcast.hours_available,
                nowcast_weight_factor=nowcast.weight_factor,
            )

        result = compute_sub_index(concentration, table)
        return replace(
            reading,
            sub_index=result.sub_index,
            band=result.band,
            breakpoint_table=result.table_id,
            method=reading.method or "hourly",
        )

    def _hourly_series(self, reading: CalibratedReading) -> list[float | None]:
        """The corrected hourly values ending at this reading's interval, newest first."""
        window_hours = self._settings.nowcast_window_hours
        end = reading.key.interval_start + dt.timedelta(hours=1)
        start = end - dt.timedelta(hours=window_hours)
        stored = self._deps.readings.query_window(
            site_code=reading.key.site_code,
            species=frozenset({reading.key.species}),
            start=start,
            end=end,
        )
        by_hour = {r.key.interval_start: r.corrected_value for r in stored.readings}
        by_hour[reading.key.interval_start] = reading.corrected_value
        return [
            by_hour.get(reading.key.interval_start - dt.timedelta(hours=age))
            for age in range(window_hours)
        ]

    def _flag(
        self, reading: CalibratedReading, *, humidity_absent: bool
    ) -> CalibratedReading:
        """Derive the one Quality_Flag and Confidence (Requirement 13)."""
        assessment = assess_quality(
            QualityInputs(
                calibration=_outcome_of(reading, humidity_absent=humidity_absent),
                nowcast=_coverage_from_reading(reading),
                conversion_source=reading.conversion_source,
                dedup_conflict=reading.quality_flag is QualityFlag.SUSPECT_CONFLICT,
                fault_flagged=False,
            )
        )
        return replace(
            reading,
            quality_flag=assessment.flag,
            confidence=assessment.confidence,
        )

    def _now(self) -> dt.datetime:
        """The current instant from the injected Clock (§2 — never read directly)."""
        return self._deps.clock.now()

    def _summarise(
        self,
        started: dt.datetime,
        archive_id: str,
        *,
        received: int,
        rejected: int,
        results: Sequence[_RecordResult],
    ) -> BatchSummary:
        """Build and log the Requirement 29.5 summary."""
        per_flag: dict[str, int] = {}
        accepted_readings: list[CalibratedReading] = []
        for result in results:
            if result.kind == "accepted" and result.reading is not None:
                accepted_readings.append(result.reading)
                key = result.reading.quality_flag.value
                per_flag[key] = per_flag.get(key, 0) + 1

        summary = BatchSummary(
            received=received,
            accepted=sum(1 for r in results if r.kind == "accepted"),
            deduplicated=sum(1 for r in results if r.kind == "duplicate"),
            quarantined=sum(1 for r in results if r.kind == "quarantined"),
            rejected=rejected + sum(1 for r in results if r.kind == "failed"),
            per_quality_flag=per_flag,
            elapsed_seconds=(self._now() - started).total_seconds(),
            archive_id=archive_id,
            overall=self._overall(accepted_readings),
        )
        _logger.info(
            "ingest_batch",
            received=summary.received,
            accepted=summary.accepted,
            deduplicated=summary.deduplicated,
            quarantined=summary.quarantined,
            rejected=summary.rejected,
            per_quality_flag=dict(summary.per_quality_flag),
            elapsed_seconds=summary.elapsed_seconds,
        )
        return summary

    def _overall(
        self, readings: Sequence[CalibratedReading]
    ) -> Mapping[str, OverallAqi]:
        """Overall AQI per site and interval, for observability only.

        Not stored — see the module docstring for why a stored overall would go stale.
        """
        grouped: dict[tuple[str, dt.datetime], list[SpeciesContribution]] = {}
        for reading in readings:
            if reading.sub_index is None:
                continue
            grouped.setdefault((reading.key.site_code, reading.key.interval_start), []).append(
                SpeciesContribution(
                    species=reading.key.species,
                    sub_index=reading.sub_index,
                    method=reading.method or "hourly",
                    confidence=reading.confidence,
                )
            )
        overall: dict[str, OverallAqi] = {}
        for (site, interval), contributions in sorted(grouped.items()):
            computed = compute_overall_aqi(
                contributions, precedence=self._settings.species_precedence
            )
            if computed is not None:
                overall[f"{site}@{interval.isoformat()}"] = computed
        return overall


@dataclass(frozen=True, slots=True)
class _RecordResult:
    """What happened to one record, for the summary to tally."""

    kind: str
    reading: CalibratedReading | None = None


def _outcome_of(
    reading: CalibratedReading, *, humidity_absent: bool
) -> CalibrationOutcome:
    """Map the calibration stage's flag back onto its outcome."""
    if humidity_absent:
        return CalibrationOutcome.UNCALIBRATED
    if reading.quality_flag is QualityFlag.CALIBRATED_EXTRAPOLATED:
        return CalibrationOutcome.EXTRAPOLATED
    if reading.quality_flag is QualityFlag.UNCALIBRATED:
        return CalibrationOutcome.UNCALIBRATED
    return CalibrationOutcome.CALIBRATED


def _coverage_from_reading(reading: CalibratedReading) -> NowCastCoverage:
    """Derive the NowCast coverage state from what the reading recorded."""
    if reading.nowcast_window_hours is None:
        return NowCastCoverage.NOT_APPLICABLE
    if reading.method == "hourly":
        return NowCastCoverage.INSUFFICIENT
    available = reading.nowcast_hours_available or 0
    return (
        NowCastCoverage.COMPLETE
        if available >= reading.nowcast_window_hours
        else NowCastCoverage.INCOMPLETE
    )



__all__ = [
    "BatchSummary",
    "IngestPipeline",
    "PipelineDependencies",
    "PipelineSettings",
]
