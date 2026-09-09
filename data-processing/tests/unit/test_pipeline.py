"""Unit tests for the Ingest_Pipeline (tasks 15.1, 15.3).

Task 15.3 names three ORDERING guarantees, which are the whole point of assembling the
stages into a fixed sequence:

- the archive write precedes the first validation rule (Requirement 6.11);
- deduplication precedes calibration, so a duplicate consumes no correction work
  (Requirement 7.9);
- calibration never runs on a record the Validator quarantined (Requirement 8.1).

Each is asserted by RECORDING THE ORDER OF OPERATIONS through the injected ports rather
than by inspecting the result, because a correct-looking result can be reached by a wrong
order — a quarantined record calibrated and then discarded looks identical from outside.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest

from aqm_ingestion.adapters.memory import (
    InMemoryMeteorologyProvider,
    InMemoryRawArchive,
    InMemoryReadingsStore,
    InMemorySensorRegistryStore,
)
from aqm_ingestion.contract.records import SensorDataRecord
from aqm_ingestion.contract.serializer import serialize_data
from aqm_ingestion.domain.dedup import dedup_key_for
from aqm_ingestion.domain.models import CalibratedReading, DedupKey, QualityFlag
from aqm_ingestion.ingest.pipeline import (
    IngestPipeline,
    PipelineDependencies,
    PipelineSettings,
)
from aqm_ingestion.observability.logging import configure_logging
from aqm_ingestion.observability.metrics import MetricsRegistry
from aqm_ingestion.ports.clock import FixedClock
from aqm_ingestion.ports.protocols import (
    ArchiveMeta,
    MetObservation,
    RawArchive,
    ReadingsStore,
)
from tests.unit.test_records import GOLDEN_DATA_PAYLOAD

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_MOMENT = "2026-07-01T11:00:00Z"


def _record(**overrides: object) -> SensorDataRecord:
    fields = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD)) | overrides
    fields.setdefault("DateTime", _MOMENT)
    return SensorDataRecord(**fields)


def _payload(*records: SensorDataRecord) -> bytes:
    if len(records) == 1:
        return serialize_data(records[0]).encode("utf-8")
    body = [json.loads(serialize_data(record)) for record in records]
    return json.dumps(body).encode("utf-8")


def _meta() -> ArchiveMeta:
    return ArchiveMeta(ingested_at=_NOW, transport="mqtt", source="aqm/london/data")


def _pipeline(
    *,
    archive: RawArchive | None = None,
    store: ReadingsStore | None = None,
    metrics: MetricsRegistry | None = None,
) -> IngestPipeline:
    clock = FixedClock(_NOW)
    return IngestPipeline(
        dependencies=PipelineDependencies(
            archive=archive or InMemoryRawArchive(),
            readings=store or InMemoryReadingsStore(clock=clock),
            registry=InMemorySensorRegistryStore(),
            meteorology=InMemoryMeteorologyProvider(),
            clock=clock,
            metrics=metrics,
        ),
        settings=PipelineSettings(),
    )


def _events(captured: str) -> list[dict[str, Any]]:
    return [
        cast("dict[str, Any]", json.loads(line))
        for line in captured.strip().splitlines()
        if line
    ]


# --- the happy path ------------------------------------------------------

def test_a_valid_record_is_accepted_and_stored() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_NOW))
    pipeline = _pipeline(store=store)
    summary = pipeline.ingest(_payload(_record()), _meta())
    assert summary.received == 1
    assert summary.accepted == 1
    assert summary.quarantined == 0
    assert store.get(_record_key()) is not None


def _record_key() -> DedupKey:
    return dedup_key_for(_record())


def test_the_stored_reading_carries_its_provenance() -> None:
    # task 15.1: every provenance field the Basis needs
    store = InMemoryReadingsStore(clock=FixedClock(_NOW))
    _pipeline(store=store).ingest(_payload(_record()), _meta())
    stored = store.get(_record_key())
    assert stored is not None
    assert stored.archive_id  # ties back to the archived payload (Req 16.2)
    assert stored.calibration_strategy
    assert stored.breakpoint_table
    assert stored.ingested_at == _NOW
    assert stored.reported_value == _record().ScaledValue  # Req 8.10


def test_a_sub_index_and_band_are_computed() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_NOW))
    _pipeline(store=store).ingest(_payload(_record()), _meta())
    stored = store.get(_record_key())
    assert stored is not None
    assert stored.sub_index is not None
    assert stored.band


# --- Req 6.11 archive precedes validation -------------------------------

def test_the_archive_write_precedes_the_first_validation_rule() -> None:
    journal: list[str] = []

    class RecordingArchive:
        def __init__(self) -> None:
            self._inner = InMemoryRawArchive()

        def write(self, payload: bytes, meta: ArchiveMeta) -> str:
            journal.append("archive")
            return self._inner.write(payload, meta)

        def read(self, archive_id: str) -> bytes:
            return self._inner.read(archive_id)

    class RecordingStore(InMemoryReadingsStore):
        def put(self, reading: CalibratedReading) -> None:
            journal.append("store")
            super().put(reading)

    pipeline = _pipeline(
        archive=RecordingArchive(), store=RecordingStore(clock=FixedClock(_NOW))
    )
    pipeline.ingest(_payload(_record()), _meta())
    assert journal[0] == "archive"


def test_a_payload_that_fails_every_rule_is_still_archived() -> None:
    # Req 6.11's purpose: a payload failing validation entirely must remain recoverable
    archive = InMemoryRawArchive()
    pipeline = _pipeline(archive=archive)
    bad = _record(Species="PM25", ScaledValue=-5.0, Units="ppb")
    summary = pipeline.ingest(_payload(bad), _meta())
    assert summary.quarantined == 1
    assert summary.archive_id
    assert archive.read(summary.archive_id)


def test_an_unparseable_payload_is_still_archived() -> None:
    archive = InMemoryRawArchive()
    summary = _pipeline(archive=archive).ingest(b"not json", _meta())
    assert summary.archive_id
    assert archive.read(summary.archive_id) == b"not json"
    assert summary.accepted == 0


# --- Req 8.1 calibration never runs on a quarantined record -------------

def test_calibration_does_not_run_on_a_quarantined_record() -> None:
    # asserted through the METEOROLOGY port: calibration is the only stage that resolves
    # humidity, so a provider never consulted proves calibration never ran
    consulted: list[str] = []

    class RecordingProvider(InMemoryMeteorologyProvider):
        def observation(
            self, site_code: str, at: dt.datetime
        ) -> MetObservation | None:
            consulted.append(site_code)
            return super().observation(site_code, at)

    clock = FixedClock(_NOW)
    pipeline = IngestPipeline(
        dependencies=PipelineDependencies(
            archive=InMemoryRawArchive(),
            readings=InMemoryReadingsStore(clock=clock),
            registry=InMemorySensorRegistryStore(),
            meteorology=RecordingProvider(),
            clock=clock,
        ),
        settings=PipelineSettings(),
    )
    pipeline.ingest(_payload(_record(ScaledValue=-5.0)), _meta())
    assert consulted == []


def test_calibration_does_run_on_an_accepted_record() -> None:
    # the counterpart, so the test above cannot pass by calibration never running at all
    consulted: list[str] = []

    class RecordingProvider(InMemoryMeteorologyProvider):
        def observation(
            self, site_code: str, at: dt.datetime
        ) -> MetObservation | None:
            consulted.append(site_code)
            return super().observation(site_code, at)

    clock = FixedClock(_NOW)
    pipeline = IngestPipeline(
        dependencies=PipelineDependencies(
            archive=InMemoryRawArchive(),
            readings=InMemoryReadingsStore(clock=clock),
            registry=InMemorySensorRegistryStore(),
            meteorology=RecordingProvider(),
            clock=clock,
        ),
        settings=PipelineSettings(),
    )
    pipeline.ingest(_payload(_record()), _meta())
    assert consulted == ["CB0001"]


def test_a_quarantined_record_is_never_stored() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_NOW))
    _pipeline(store=store).ingest(_payload(_record(ScaledValue=-5.0)), _meta())
    assert store.get(_record_key()) is None


# --- Req 7.9 deduplication precedes calibration -------------------------

def test_a_duplicate_consumes_no_calibration_work() -> None:
    consulted: list[str] = []

    class RecordingProvider(InMemoryMeteorologyProvider):
        def observation(
            self, site_code: str, at: dt.datetime
        ) -> MetObservation | None:
            consulted.append(site_code)
            return super().observation(site_code, at)

    clock = FixedClock(_NOW)
    pipeline = IngestPipeline(
        dependencies=PipelineDependencies(
            archive=InMemoryRawArchive(),
            readings=InMemoryReadingsStore(clock=clock),
            registry=InMemorySensorRegistryStore(),
            meteorology=RecordingProvider(),
            clock=clock,
        ),
        settings=PipelineSettings(),
    )
    pipeline.ingest(_payload(_record()), _meta())
    first_calls = len(consulted)
    summary = pipeline.ingest(_payload(_record()), _meta())
    assert summary.deduplicated == 1
    assert len(consulted) == first_calls  # no further correction work


def test_a_duplicate_is_counted_not_quarantined() -> None:
    pipeline = _pipeline()
    pipeline.ingest(_payload(_record()), _meta())
    summary = pipeline.ingest(_payload(_record()), _meta())
    assert summary.deduplicated == 1
    assert summary.quarantined == 0
    assert summary.accepted == 0


# --- Req 29.5 the batch summary -----------------------------------------

def test_the_summary_reports_every_documented_count() -> None:
    assert set(type(_pipeline().ingest(_payload(_record()), _meta())).__dataclass_fields__) == {
        "received",
        "accepted",
        "deduplicated",
        "quarantined",
        "rejected",
        "per_quality_flag",
        "elapsed_seconds",
        "archive_id",
        "overall",
    }


def test_the_summary_counts_per_quality_flag() -> None:
    summary = _pipeline().ingest(_payload(_record()), _meta())
    assert sum(summary.per_quality_flag.values()) == summary.accepted
    assert set(summary.per_quality_flag) <= {flag.value for flag in QualityFlag}


def test_one_summary_event_is_logged(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    _pipeline().ingest(_payload(_record()), _meta())
    summaries = [
        e for e in _events(capsys.readouterr().out) if e.get("event") == "ingest_batch"
    ]
    assert len(summaries) == 1
    assert summaries[0]["received"] == 1
    assert "elapsed_seconds" in summaries[0]


def test_a_batch_of_several_records_is_counted() -> None:
    summary = _pipeline().ingest(
        _payload(
            _record(Species="PM25"),
            _record(Species="NO2", Units="ug.m-3", ScaledValue=40.0),
        ),
        _meta(),
    )
    assert summary.received == 2
    assert summary.accepted == 2


def test_a_rejected_element_is_counted_separately_from_a_quarantine() -> None:
    # a parse rejection and a validation quarantine are different failures: one never
    # became a record at all
    payload = json.dumps(
        [json.loads(serialize_data(_record())), {"Species": "PM25"}]
    ).encode("utf-8")
    summary = _pipeline().ingest(payload, _meta())
    assert summary.accepted == 1
    assert summary.rejected == 1
    assert summary.quarantined == 0


# --- §5 per-record isolation --------------------------------------------

def test_one_failing_record_does_not_stop_the_batch() -> None:
    summary = _pipeline().ingest(
        _payload(
            _record(ScaledValue=-5.0),  # quarantined
            _record(DateTime="2026-07-01T10:00:00Z"),  # fine
        ),
        _meta(),
    )
    assert summary.accepted == 1
    assert summary.quarantined == 1


def test_an_unexpected_per_record_failure_is_isolated_and_logged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # §5: a per-record failure must not abort the batch, and must still be logged
    class ExplodingStore(InMemoryReadingsStore):
        def put(self, reading: CalibratedReading) -> None:
            if reading.key.species == "PM25":
                raise RuntimeError("store unavailable for PM25")
            super().put(reading)

    configure_logging("info")
    pipeline = _pipeline(store=ExplodingStore(clock=FixedClock(_NOW)))
    summary = pipeline.ingest(
        _payload(
            _record(Species="PM25"),
            _record(Species="NO2", Units="ug.m-3", ScaledValue=40.0),
        ),
        _meta(),
    )
    errors = [e for e in _events(capsys.readouterr().out) if e["level"] == "error"]
    assert len(errors) == 1
    assert summary.accepted == 1  # the NO2 record survived its sibling's failure


# --- transport agnosticism ----------------------------------------------

def test_the_pipeline_takes_bytes_and_metadata_only() -> None:
    # it must be usable from MQTT and from a poll with no change, so its entry point
    # carries no transport object
    import inspect

    parameters = set(inspect.signature(IngestPipeline.ingest).parameters) - {"self"}
    assert parameters == {"payload", "meta"}


def test_the_transport_is_carried_in_the_metadata() -> None:
    for transport in ("mqtt", "feed"):
        summary = _pipeline().ingest(
            _payload(_record()),
            ArchiveMeta(ingested_at=_NOW, transport=transport, source="s"),
        )
        assert summary.received == 1


# --- Req 16.7 an archive failure processes nothing ----------------------

def test_an_archive_failure_aborts_before_any_processing() -> None:
    class FailingArchive:
        def write(self, payload: bytes, meta: ArchiveMeta) -> str:
            raise OSError("archive down")

        def read(self, archive_id: str) -> bytes:
            raise KeyError(archive_id)

    from aqm_ingestion.ingest.archive import ArchiveWriteFailedError

    store = InMemoryReadingsStore(clock=FixedClock(_NOW))
    pipeline = _pipeline(archive=FailingArchive(), store=store)
    with pytest.raises(ArchiveWriteFailedError):
        pipeline.ingest(_payload(_record()), _meta())
    assert store.get(_record_key()) is None


# --- metrics ------------------------------------------------------------

def test_metrics_are_recorded_when_supplied() -> None:
    metrics = MetricsRegistry()
    _pipeline(metrics=metrics).ingest(_payload(_record()), _meta())
    counters = metrics.snapshot().counters
    assert counters["records_ingested"]["mqtt"] == 1


def test_metrics_are_optional() -> None:
    assert _pipeline(metrics=None).ingest(_payload(_record()), _meta()).accepted == 1
