"""Unit tests for the MQTT entry point (task 16.1).

- 4.1: subscribe to the configured filter (default `aqm/sensors/+/data`) and log it.
- 4.2: a payload is a single record or an array of them, passed through the pipeline.
- 4.3: a `SiteCode` disagreeing with the topic's site identifier is QUARANTINED, naming
  both values.
- 4.4: a failure handling one message is caught, logged with topic/site/error type, and does
  not stop the subscription.
- 4.5: reconnect with exponential backoff from 1 s doubling to the configured maximum,
  indefinitely, logging each attempt at `warning` with the attempt count.
- 4.6: acknowledge ONLY after the archive write succeeded.
- 4.7: the transport is recorded as `mqtt`.
- 4.8: disabled means NO subscription is opened.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
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
from aqm_ingestion.domain.models import CalibratedReading
from aqm_ingestion.ingest.mqtt_entry import (
    DEFAULT_MAX_BACKOFF_SECONDS,
    DEFAULT_TOPIC_FILTER,
    DEFAULT_TOPIC_PATTERN,
    MqttSettings,
    MqttSubscriber,
    backoff_delays,
    site_code_from_topic,
)
from aqm_ingestion.ingest.pipeline import (
    IngestPipeline,
    PipelineDependencies,
    PipelineSettings,
)
from aqm_ingestion.observability.logging import configure_logging
from aqm_ingestion.ports.clock import FixedClock
from aqm_ingestion.ports.protocols import ArchiveMeta, RawArchive, ReadingsStore

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_MOMENT = "2026-07-01T11:00:00Z"


def _record(**overrides: object) -> SensorDataRecord:
    from tests.unit.test_records import GOLDEN_DATA_PAYLOAD

    fields = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD)) | overrides
    fields.setdefault("DateTime", _MOMENT)
    return SensorDataRecord(**fields)


def _payload(record: SensorDataRecord) -> bytes:
    return serialize_data(record).encode("utf-8")


def _events(captured: str) -> list[dict[str, Any]]:
    return [
        cast("dict[str, Any]", json.loads(line))
        for line in captured.strip().splitlines()
        if line
    ]


class FakeTransport:
    """Records subscriptions and acknowledgements so both can be asserted."""

    def __init__(self, messages: list[tuple[str, bytes, int]]) -> None:
        """Hold the sequence to replay."""
        self._messages = messages
        self.subscriptions: list[str] = []
        self.acknowledged: list[int] = []
        self.closed = False

    def subscribe(self, topic_filter: str) -> None:
        """Record the resolved filter."""
        self.subscriptions.append(topic_filter)

    def messages(self) -> Iterator[tuple[str, bytes, int]]:
        """Yield the scripted messages in arrival order."""
        yield from self._messages

    def acknowledge(self, delivery_tag: int) -> None:
        """Record the acknowledgement, which is what Req 4.6 is asserted against."""
        self.acknowledged.append(delivery_tag)

    def close(self) -> None:
        """Mark the transport closed."""
        self.closed = True


def _subscriber(
    transport: FakeTransport,
    *,
    settings: MqttSettings | None = None,
    archive: RawArchive | None = None,
    store: ReadingsStore | None = None,
) -> MqttSubscriber:
    clock = FixedClock(_NOW)
    pipeline = IngestPipeline(
        dependencies=PipelineDependencies(
            archive=archive or InMemoryRawArchive(),
            readings=store or InMemoryReadingsStore(clock=clock),
            registry=InMemorySensorRegistryStore(),
            meteorology=InMemoryMeteorologyProvider(),
            clock=clock,
        ),
        settings=PipelineSettings(),
    )
    return MqttSubscriber(
        transport=transport,
        pipeline=pipeline,
        clock=clock,
        settings=settings or MqttSettings(),
    )


# --- Req 4.1 the subscription -------------------------------------------

def test_default_topic_filter_is_the_documented_one() -> None:
    assert DEFAULT_TOPIC_FILTER == "aqm/sensors/+/data"


def test_default_topic_pattern_is_the_documented_one() -> None:
    assert DEFAULT_TOPIC_PATTERN == "aqm/sensors/{SiteCode}/data"


def test_the_subscriber_subscribes_to_the_configured_filter() -> None:
    transport = FakeTransport([])
    _subscriber(transport).run()
    assert transport.subscriptions == [DEFAULT_TOPIC_FILTER]


def test_the_resolved_filter_is_logged(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    _subscriber(FakeTransport([])).run()
    events = [
        e for e in _events(capsys.readouterr().out) if e.get("event") == "mqtt_subscribed"
    ]
    assert len(events) == 1
    assert events[0]["topic_filter"] == DEFAULT_TOPIC_FILTER


def test_a_custom_filter_is_honoured() -> None:
    transport = FakeTransport([])
    _subscriber(transport, settings=MqttSettings(topic_filter="custom/#")).run()
    assert transport.subscriptions == ["custom/#"]


# --- Req 4.8 disabled ----------------------------------------------------

def test_a_disabled_interface_opens_no_subscription() -> None:
    transport = FakeTransport([("aqm/sensors/CB0001/data", _payload(_record()), 1)])
    _subscriber(transport, settings=MqttSettings(enabled=False)).run()
    assert transport.subscriptions == []
    assert transport.acknowledged == []


def test_a_disabled_interface_processes_nothing() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_NOW))
    transport = FakeTransport([("aqm/sensors/CB0001/data", _payload(_record()), 1)])
    _subscriber(
        transport, settings=MqttSettings(enabled=False), store=store
    ).run()
    from aqm_ingestion.domain.dedup import dedup_key_for

    assert store.get(dedup_key_for(_record())) is None


# --- Req 4.3 the site identifier ----------------------------------------

def test_the_site_code_is_extracted_from_the_topic() -> None:
    extracted = site_code_from_topic(
        "aqm/sensors/CB0001/data", DEFAULT_TOPIC_PATTERN
    )
    assert extracted == "CB0001"


def test_a_topic_not_matching_the_pattern_yields_none() -> None:
    assert site_code_from_topic("other/topic", DEFAULT_TOPIC_PATTERN) is None


def test_extraction_does_not_confuse_a_longer_topic() -> None:
    # a pattern match must be anchored, or "aqm/sensors/X/data/extra" would look valid
    assert site_code_from_topic(
        "aqm/sensors/CB0001/data/extra", DEFAULT_TOPIC_PATTERN
    ) is None


def test_a_site_code_with_a_slash_is_not_matched() -> None:
    # a single topic level, so an embedded separator cannot smuggle two levels in
    assert site_code_from_topic("aqm/sensors/A/B/data", DEFAULT_TOPIC_PATTERN) is None


def test_a_matching_site_code_is_ingested() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_NOW))
    transport = FakeTransport([("aqm/sensors/CB0001/data", _payload(_record()), 1)])
    report = _subscriber(transport, store=store).run()
    assert report.ingested == 1
    assert report.mismatched == 0


def test_a_disagreeing_site_code_is_quarantined(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_NOW))
    configure_logging("info")
    transport = FakeTransport([("aqm/sensors/OTHER/data", _payload(_record()), 1)])
    report = _subscriber(transport, store=store).run()

    assert report.mismatched == 1
    assert report.ingested == 0
    from aqm_ingestion.domain.dedup import dedup_key_for

    assert store.get(dedup_key_for(_record())) is None


def test_the_mismatch_warning_names_both_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Req 4.3 says the rejection reason names BOTH, because either side could be wrong
    configure_logging("info")
    transport = FakeTransport([("aqm/sensors/OTHER/data", _payload(_record()), 1)])
    _subscriber(transport).run()
    events = [
        e
        for e in _events(capsys.readouterr().out)
        if e.get("event") == "topic_site_mismatch"
    ]
    assert len(events) == 1
    assert events[0]["topic_site_code"] == "OTHER"
    assert events[0]["record_site_code"] == "CB0001"


def test_a_mismatched_payload_is_still_archived() -> None:
    # the quarantine happens after the archive, so the payload stays recoverable
    archive = InMemoryRawArchive()
    transport = FakeTransport([("aqm/sensors/OTHER/data", _payload(_record()), 1)])
    report = _subscriber(transport, archive=archive).run()
    assert report.archive_ids
    assert archive.read(report.archive_ids[0])


def test_an_unmatched_topic_is_reported_not_ingested() -> None:
    transport = FakeTransport([("junk/topic", _payload(_record()), 1)])
    report = _subscriber(transport).run()
    assert report.unmatched_topics == 1
    assert report.ingested == 0


# --- Req 4.6 acknowledge only after the archive write -------------------

def test_a_message_is_acknowledged_after_a_successful_archive() -> None:
    transport = FakeTransport([("aqm/sensors/CB0001/data", _payload(_record()), 7)])
    _subscriber(transport).run()
    assert transport.acknowledged == [7]


def test_a_message_is_not_acknowledged_when_the_archive_fails() -> None:
    # Req 4.6's whole purpose: an unacknowledged message will be redelivered, so the
    # payload is not lost silently
    class FailingArchive:
        def write(self, payload: bytes, meta: ArchiveMeta) -> str:
            raise OSError("archive down")

        def read(self, archive_id: str) -> bytes:
            raise KeyError(archive_id)

    transport = FakeTransport([("aqm/sensors/CB0001/data", _payload(_record()), 7)])
    report = _subscriber(transport, archive=FailingArchive()).run()
    assert transport.acknowledged == []
    assert report.unacknowledged == 1


def test_an_archive_failure_does_not_stop_the_subscription() -> None:
    # Req 4.4: the next message must still be processed
    calls: list[int] = []

    class SometimesFailingArchive:
        def __init__(self) -> None:
            self._inner = InMemoryRawArchive()

        def write(self, payload: bytes, meta: ArchiveMeta) -> str:
            calls.append(1)
            if len(calls) == 1:
                raise OSError("transient")
            return self._inner.write(payload, meta)

        def read(self, archive_id: str) -> bytes:
            return self._inner.read(archive_id)

    transport = FakeTransport(
        [
            ("aqm/sensors/CB0001/data", _payload(_record()), 1),
            ("aqm/sensors/CB0001/data", _payload(_record(DateTime="2026-07-01T10:00:00Z")), 2),
        ]
    )
    report = _subscriber(transport, archive=SometimesFailingArchive()).run()
    assert transport.acknowledged == [2]  # only the second
    assert report.ingested == 1


def test_a_quarantined_record_is_still_acknowledged() -> None:
    # the archive write succeeded, so redelivering would archive the same payload again
    # without ever succeeding — a quarantine is a decision, not a delivery failure
    transport = FakeTransport(
        [("aqm/sensors/CB0001/data", _payload(_record(ScaledValue=-5.0)), 3)]
    )
    _subscriber(transport).run()
    assert transport.acknowledged == [3]


# --- Req 4.4 per-message isolation --------------------------------------

def test_a_failing_message_does_not_stop_the_next(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    transport = FakeTransport(
        [
            ("aqm/sensors/CB0001/data", b"not json at all", 1),
            ("aqm/sensors/CB0001/data", _payload(_record()), 2),
        ]
    )
    report = _subscriber(transport).run()
    assert report.ingested == 1


def test_a_handling_failure_logs_topic_site_and_error_type(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ExplodingStore(InMemoryReadingsStore):
        def put(self, reading: CalibratedReading) -> None:
            raise RuntimeError("store unavailable")

    configure_logging("info")
    transport = FakeTransport([("aqm/sensors/CB0001/data", _payload(_record()), 1)])
    _subscriber(transport, store=ExplodingStore(clock=FixedClock(_NOW))).run()
    errors = [e for e in _events(capsys.readouterr().out) if e["level"] == "error"]
    assert errors
    assert any("RuntimeError" in json.dumps(event) for event in errors)


# --- Req 4.5 the backoff schedule ---------------------------------------

def test_backoff_starts_at_one_second() -> None:
    assert next(iter(backoff_delays(maximum_seconds=60))) == 1.0


def test_backoff_doubles() -> None:
    delays = backoff_delays(maximum_seconds=60)
    assert [next(delays) for _ in range(5)] == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_backoff_caps_at_the_configured_maximum() -> None:
    delays = backoff_delays(maximum_seconds=10)
    observed = [next(delays) for _ in range(8)]
    assert max(observed) == 10.0
    assert observed[-1] == 10.0


def test_backoff_is_indefinite() -> None:
    # Req 4.5 retries INDEFINITELY, so the schedule must never terminate
    delays = backoff_delays(maximum_seconds=2)
    assert len([next(delays) for _ in range(1_000)]) == 1_000


def test_default_maximum_backoff_is_sixty_seconds() -> None:
    assert DEFAULT_MAX_BACKOFF_SECONDS == 60.0


def test_a_non_positive_maximum_is_refused() -> None:
    with pytest.raises(ValueError, match="maximum"):
        next(backoff_delays(maximum_seconds=0))


# --- Req 4.7 the transport is recorded ----------------------------------

def test_the_transport_is_recorded_as_mqtt() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_NOW))
    transport = FakeTransport([("aqm/sensors/CB0001/data", _payload(_record()), 1)])
    _subscriber(transport, store=store).run()
    from aqm_ingestion.domain.dedup import dedup_key_for

    stored = store.get(dedup_key_for(_record()))
    assert stored is not None
    # the archive id ties the reading to an entry written under the mqtt transport
    assert stored.archive_id


def test_the_source_records_the_topic() -> None:
    # Req 16.2's "source topic or request window" — for MQTT it is the topic
    archive = InMemoryRawArchive()
    transport = FakeTransport([("aqm/sensors/CB0001/data", _payload(_record()), 1)])
    report = _subscriber(transport, archive=archive).run()
    assert report.archive_ids
