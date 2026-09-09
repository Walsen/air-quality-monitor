"""Unit tests for the feed poller entry point (task 16.2).

- 5.1: request /SensorData for the configured species over the derived window, through the
  SAME pipeline the push path uses.
- 5.2: the credential is resolved only from the environment or a supplied path and reaches no
  log. Asserted by searching ALL captured stdout for the value, not by inspecting one event.
- 5.3: the window runs from the most recent ingested DateTime MINUS the overlap (default 2 h)
  to the Clock's instant, or the initial backfill span (default 24 h) when nothing is stored.
- 5.5: one structured summary per invocation.
- 5.6: on failure, the high-water mark is UNCHANGED and the exit status is non-zero.
- 5.7: /ListSensors on the refresh cadence, upserted into the registry.
- 5.8: bounded by the maximum record count, resuming WITHOUT a gap.
- 5.9: disabled means NO outbound request.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, cast

import pytest

from aqm_ingestion.adapters.memory import (
    InMemoryMeteorologyProvider,
    InMemoryRawArchive,
    InMemoryReadingsStore,
    InMemorySensorRegistryStore,
    ScriptedFeedClient,
)
from aqm_ingestion.contract.records import SensorDataRecord
from aqm_ingestion.contract.serializer import serialize_data
from aqm_ingestion.ingest.feed_entry import (
    DEFAULT_BACKFILL_HOURS,
    DEFAULT_MAX_RECORDS,
    DEFAULT_OVERLAP_HOURS,
    DEFAULT_REGISTRY_REFRESH_HOURS,
    FeedPoller,
    FeedSettings,
    resolve_credential,
)
from aqm_ingestion.ingest.pipeline import (
    IngestPipeline,
    PipelineDependencies,
    PipelineSettings,
)
from aqm_ingestion.observability.logging import configure_logging
from aqm_ingestion.ports.clock import FixedClock
from aqm_ingestion.ports.protocols import FeedClient

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _record(**overrides: object) -> SensorDataRecord:
    from tests.unit.test_records import GOLDEN_DATA_PAYLOAD

    fields = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD)) | overrides
    fields.setdefault("DateTime", "2026-07-01T11:00:00Z")
    return SensorDataRecord(**fields)


def _payload(*records: SensorDataRecord) -> bytes:
    body = [json.loads(serialize_data(record)) for record in records]
    return json.dumps(body).encode("utf-8")


def _events(captured: str) -> list[dict[str, Any]]:
    return [
        cast("dict[str, Any]", json.loads(line))
        for line in captured.strip().splitlines()
        if line
    ]


def _poller(
    client: FeedClient,
    *,
    settings: FeedSettings | None = None,
    store: InMemoryReadingsStore | None = None,
    registry: InMemorySensorRegistryStore | None = None,
) -> FeedPoller:
    clock = FixedClock(_NOW)
    readings = store or InMemoryReadingsStore(clock=clock)
    pipeline = IngestPipeline(
        dependencies=PipelineDependencies(
            archive=InMemoryRawArchive(),
            readings=readings,
            registry=registry or InMemorySensorRegistryStore(),
            meteorology=InMemoryMeteorologyProvider(),
            clock=clock,
        ),
        settings=PipelineSettings(),
    )
    return FeedPoller(
        client=client,
        pipeline=pipeline,
        readings=readings,
        registry=registry or InMemorySensorRegistryStore(),
        clock=clock,
        settings=settings or FeedSettings(),
    )


# --- defaults ------------------------------------------------------------

def test_defaults_are_the_documented_values() -> None:
    assert DEFAULT_OVERLAP_HOURS == 2
    assert DEFAULT_BACKFILL_HOURS == 24
    assert DEFAULT_MAX_RECORDS == 500_000
    assert DEFAULT_REGISTRY_REFRESH_HOURS == 24


# --- Req 5.3 the derived window -----------------------------------------

def test_an_empty_store_uses_the_initial_backfill_span() -> None:
    client = ScriptedFeedClient()
    _poller(client).poll()
    since, until = client.calls[0]
    assert until == _NOW
    assert since == _NOW - dt.timedelta(hours=DEFAULT_BACKFILL_HOURS)


def test_a_populated_store_uses_the_high_water_mark_minus_the_overlap() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_NOW))
    latest = _NOW - dt.timedelta(hours=3)
    registry = _ingest(store, latest)

    client = ScriptedFeedClient()
    _poller(client, store=store, registry=registry).poll()
    since, _until = client.calls[0]
    assert since == latest - dt.timedelta(hours=DEFAULT_OVERLAP_HOURS)


def _ingest(
    store: InMemoryReadingsStore,
    moment: dt.datetime,
    registry: InMemorySensorRegistryStore | None = None,
) -> InMemorySensorRegistryStore:
    """Put one reading and register its site, to establish a high-water mark.

    The site must be registered because the mark is derived from stored readings across the
    sites the REGISTRY knows — which is what makes Req 5.6 hold without a separate cursor.
    """
    from aqm_ingestion.contract.records import SensorMetadataRecord
    from aqm_ingestion.domain.models import (
        CalibratedReading,
        Confidence,
        DedupKey,
        QualityFlag,
    )
    from tests.unit.test_records import GOLDEN_METADATA_PAYLOAD

    known = registry or InMemorySensorRegistryStore()
    known.upsert(
        SensorMetadataRecord(
            **cast("dict[str, Any]", json.loads(GOLDEN_METADATA_PAYLOAD))
        ),
        _NOW,
    )
    store.put(
        CalibratedReading(
            key=DedupKey(
                site_code="CB0001",
                species="PM25",
                interval_start=moment,
                duration="PT1H",
            ),
            reported_value=10.0,
            corrected_value=10.0,
            units="ug.m-3",
            quality_flag=QualityFlag.CALIBRATED,
            confidence=Confidence.HIGH,
            calibration_strategy="rh_linear",
            breakpoint_table="epa-2024-05-06",
            ratification_status="R",
            ingested_at=_NOW,
            archive_id="a1",
        )
    )
    return known


def test_the_overlap_is_configurable() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_NOW))
    latest = _NOW - dt.timedelta(hours=5)
    registry = _ingest(store, latest)
    client = ScriptedFeedClient()
    _poller(
        client, store=store, registry=registry, settings=FeedSettings(overlap_hours=6)
    ).poll()
    since, _until = client.calls[0]
    assert since == latest - dt.timedelta(hours=6)


def test_the_overlap_deliberately_re_requests_data() -> None:
    # Req 5.3's overlap exists so a record that arrived late at the feed is not missed;
    # deduplication (Req 7) is what makes re-requesting safe
    store = InMemoryReadingsStore(clock=FixedClock(_NOW))
    latest = _NOW - dt.timedelta(hours=1)
    registry = _ingest(store, latest)
    client = ScriptedFeedClient()
    _poller(client, store=store, registry=registry).poll()
    since, _until = client.calls[0]
    assert since < latest


def test_the_window_ends_at_the_clock_instant() -> None:
    client = ScriptedFeedClient()
    _poller(client).poll()
    _since, until = client.calls[0]
    assert until == _NOW  # §2: from the injected clock, never read directly


# --- Req 5.1 the same pipeline ------------------------------------------

def test_returned_records_are_ingested() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_NOW))
    window = (
        _NOW - dt.timedelta(hours=DEFAULT_BACKFILL_HOURS),
        _NOW,
    )
    client = ScriptedFeedClient({window: _payload(_record())})
    report = _poller(client, store=store).poll()
    assert report.accepted == 1
    assert report.exit_code == 0


def test_the_configured_species_are_requested() -> None:
    class RecordingClient(ScriptedFeedClient):
        def __init__(self) -> None:
            super().__init__()
            self.species: list[frozenset[str]] = []

        def fetch_data(
            self, since: dt.datetime, until: dt.datetime, species: frozenset[str]
        ) -> bytes:
            self.species.append(species)
            return super().fetch_data(since, until, species)

    client = RecordingClient()
    _poller(client, settings=FeedSettings(species=frozenset({"NO2"}))).poll()
    assert client.species == [frozenset({"NO2"})]


# --- Req 5.9 disabled ---------------------------------------------------

def test_a_disabled_interface_makes_no_request() -> None:
    client = ScriptedFeedClient()
    report = _poller(client, settings=FeedSettings(enabled=False)).poll()
    assert client.calls == []
    assert client.sensor_calls == 0
    assert report.exit_code == 0


# --- Req 5.2 the credential ---------------------------------------------

def test_the_credential_resolves_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AQM_FEED_API_KEY", "s3cret-value")
    assert resolve_credential(env_var="AQM_FEED_API_KEY") == "s3cret-value"


def test_the_credential_resolves_from_a_supplied_path(tmp_path: Path) -> None:
    path = tmp_path / "key"
    path.write_text("from-file\n", encoding="utf-8")
    # trailing whitespace stripped: a file written by an operator usually ends in a newline,
    # and sending it would produce a puzzling 401
    assert resolve_credential(path=str(path)) == "from-file"


def test_an_absent_credential_is_refused_naming_the_source() -> None:
    with pytest.raises(ValueError, match="AQM_FEED_API_KEY"):
        resolve_credential(env_var="AQM_FEED_API_KEY")


def test_the_credential_error_does_not_contain_a_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # §7: even a failure path must not echo material
    path = tmp_path / "key"
    path.write_text("do-not-echo", encoding="utf-8")
    monkeypatch.setenv("AQM_FEED_API_KEY", "do-not-echo")
    try:
        resolve_credential(env_var="MISSING_VAR")
    except ValueError as error:
        assert "do-not-echo" not in str(error)


def test_the_poller_never_receives_a_credential() -> None:
    # the structural guarantee: the FeedClient port carries no credential, so the poller
    # cannot log one however carelessly it is written
    import inspect

    from aqm_ingestion.ports.protocols import FeedClient

    signature = inspect.signature(FeedClient.fetch_data)
    assert "credential" not in signature.parameters
    assert "api_key" not in signature.parameters
    assert "credential" not in inspect.signature(FeedPoller.__init__).parameters


def test_no_log_line_contains_the_credential(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # asserted over ALL captured stdout rather than one event, so a credential leaking into
    # any field of any line fails
    monkeypatch.setenv("AQM_FEED_API_KEY", "super-secret-key")
    configure_logging("debug")
    window = (_NOW - dt.timedelta(hours=DEFAULT_BACKFILL_HOURS), _NOW)
    _poller(ScriptedFeedClient({window: _payload(_record())})).poll()
    assert "super-secret-key" not in capsys.readouterr().out


# --- Req 5.5 the poll summary -------------------------------------------

def test_one_summary_is_logged(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    _poller(ScriptedFeedClient()).poll()
    summaries = [
        e for e in _events(capsys.readouterr().out) if e.get("event") == "feed_poll"
    ]
    assert len(summaries) == 1
    event = summaries[0]
    for field in ("window_start", "window_end", "received", "accepted", "elapsed_seconds"):
        assert field in event


# --- Req 5.6 failure leaves state unchanged -----------------------------

def test_a_transport_failure_exits_non_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingClient(ScriptedFeedClient):
        def fetch_data(
            self, since: dt.datetime, until: dt.datetime, species: frozenset[str]
        ) -> bytes:
            raise OSError("feed unreachable")

    configure_logging("info")
    report = _poller(FailingClient()).poll()
    assert report.exit_code != 0
    errors = [e for e in _events(capsys.readouterr().out) if e["level"] == "error"]
    assert errors


def test_a_failure_leaves_the_high_water_mark_unchanged() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_NOW))
    latest = _NOW - dt.timedelta(hours=3)
    registry = _ingest(store, latest)

    class FailingClient(ScriptedFeedClient):
        def fetch_data(
            self, since: dt.datetime, until: dt.datetime, species: frozenset[str]
        ) -> bytes:
            raise OSError("feed unreachable")

    _poller(FailingClient(), store=store, registry=registry).poll()

    # the next invocation must retry the SAME window, so the mark cannot have moved
    client = ScriptedFeedClient()
    _poller(client, store=store, registry=registry).poll()
    since, _until = client.calls[0]
    assert since == latest - dt.timedelta(hours=DEFAULT_OVERLAP_HOURS)


def test_an_unparseable_body_is_a_failure_not_an_empty_poll() -> None:
    # Req 5.6 lists "a body the Parser rejects wholesale" alongside a transport error: an
    # empty result and a broken feed must not look the same, or a broken feed would silently
    # advance the window past data it never delivered
    window = (_NOW - dt.timedelta(hours=DEFAULT_BACKFILL_HOURS), _NOW)
    client = ScriptedFeedClient({window: b"not json"})
    report = _poller(client).poll()
    assert report.exit_code != 0


def test_an_empty_window_is_a_success() -> None:
    report = _poller(ScriptedFeedClient()).poll()
    assert report.exit_code == 0
    assert report.received == 0


# --- Req 5.7 the registry refresh ---------------------------------------

def test_the_registry_is_refreshed_on_the_first_poll() -> None:
    client = ScriptedFeedClient()
    _poller(client).poll()
    assert client.sensor_calls == 1


def test_the_registry_is_not_refreshed_again_within_the_cadence() -> None:
    client = ScriptedFeedClient()
    poller = _poller(client)
    poller.poll()
    poller.poll()
    assert client.sensor_calls == 1


def test_sensor_records_are_upserted() -> None:
    from tests.unit.test_records import GOLDEN_METADATA_PAYLOAD

    registry = InMemorySensorRegistryStore()
    sensors = json.dumps([json.loads(GOLDEN_METADATA_PAYLOAD)]).encode("utf-8")
    client = ScriptedFeedClient(sensors=sensors)
    _poller(client, registry=registry).poll()
    assert registry.get("CB0001") is not None


def test_a_registry_refresh_failure_does_not_fail_the_poll(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # readings are the point of the invocation; a stale registry costs geographic results
    # (Req 6.7 already tolerates an unknown site) but losing readings is worse
    class SensorFailingClient(ScriptedFeedClient):
        def fetch_sensors(self) -> bytes:
            raise OSError("list unavailable")

    configure_logging("info")
    report = _poller(SensorFailingClient()).poll()
    assert report.exit_code == 0


# --- Req 5.8 the record bound -------------------------------------------

def test_a_bounded_invocation_records_the_remaining_window() -> None:
    window = (_NOW - dt.timedelta(hours=DEFAULT_BACKFILL_HOURS), _NOW)
    records = [
        _record(DateTime=f"2026-07-01T{hour:02d}:00:00Z") for hour in range(3)
    ]
    client = ScriptedFeedClient({window: _payload(*records)})
    report = _poller(client, settings=FeedSettings(max_records=2)).poll()
    assert report.received == 2  # the oldest portion first
    assert report.remaining_window is not None


def test_an_unbounded_invocation_records_no_remaining_window() -> None:
    window = (_NOW - dt.timedelta(hours=DEFAULT_BACKFILL_HOURS), _NOW)
    client = ScriptedFeedClient({window: _payload(_record())})
    report = _poller(client, settings=FeedSettings(max_records=10)).poll()
    assert report.remaining_window is None


def test_the_bound_takes_the_oldest_portion_first() -> None:
    # Req 5.8: oldest first, so the next invocation resumes forward without a gap
    window = (_NOW - dt.timedelta(hours=DEFAULT_BACKFILL_HOURS), _NOW)
    records = [
        _record(DateTime="2026-07-01T09:00:00Z"),
        _record(DateTime="2026-07-01T10:00:00Z"),
        _record(DateTime="2026-07-01T11:00:00Z"),
    ]
    store = InMemoryReadingsStore(clock=FixedClock(_NOW))
    client = ScriptedFeedClient({window: _payload(*records)})
    _poller(client, store=store, settings=FeedSettings(max_records=1)).poll()
    result = store.query_window(
        site_code="CB0001",
        species=None,
        start=_NOW - dt.timedelta(days=1),
        end=_NOW,
    )
    assert [r.key.interval_start.hour for r in result.readings] == [9]
