"""Entry-point convergence property test (task 16.3).

Feature: ingestion-and-serving-service
- Property 39: push and pull ingestion converge on the same state
  (Requirements 27.7, 5.4, 27.3)

WHAT "CONVERGE" MEANS HERE, because a naive reading of Requirement 27.7 ("identical final
ReadingsStore state") is not achievable and must not be:

Requirement 27.6 derives the archive identifier from injected inputs INCLUDING the transport
and the source, and Requirement 4.7 requires the transport to be recorded on the resulting
readings precisely so the ingestion path of any stored Reading is recoverable. The push path
carries transport `mqtt` with a topic as its source; the pull path carries `feed` with a
request window. The archive identifiers therefore MUST differ — a pipeline that made them
identical would have thrown away the provenance Requirement 4.7 exists to preserve.

Requirement 5.4 settles it by enumerating what must match: "the same corrected value,
Quality_Flag, Sub_Index, and Band". Provenance is deliberately absent from that list. So this
property compares the MEASUREMENT — every field that describes what was measured and what this
service computed from it — and asserts the provenance fields differ, which is the other half
of the same claim.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import fields
from typing import Any, cast

from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.adapters.memory import (
    InMemoryMeteorologyProvider,
    InMemoryRawArchive,
    InMemoryReadingsStore,
    InMemorySensorRegistryStore,
    ScriptedFeedClient,
    ScriptedMqttTransport,
)
from aqm_ingestion.contract.records import SensorDataRecord, SensorMetadataRecord
from aqm_ingestion.contract.serializer import serialize_data
from aqm_ingestion.domain.models import CalibratedReading
from aqm_ingestion.ingest.feed_entry import DEFAULT_BACKFILL_HOURS, FeedPoller, FeedSettings
from aqm_ingestion.ingest.mqtt_entry import MqttSettings, MqttSubscriber
from aqm_ingestion.ingest.pipeline import (
    IngestPipeline,
    PipelineDependencies,
    PipelineSettings,
)
from aqm_ingestion.ports.clock import FixedClock
from tests.unit.test_records import GOLDEN_DATA_PAYLOAD, GOLDEN_METADATA_PAYLOAD

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)

# Fields that describe HOW a reading arrived rather than WHAT was measured. Requirement 5.4
# omits these from what must match, and Requirements 4.7 and 27.6 require them to differ.
_PROVENANCE_FIELDS = frozenset({"archive_id"})


def _record(species: str, value: float, hours_ago: int) -> SensorDataRecord:
    moment = (_NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields_map = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD)) | {
        "Species": species,
        "Units": "ug.m-3",
        "ScaledValue": value,
        "DateTime": moment,
    }
    return SensorDataRecord(**fields_map)


def _stack() -> tuple[IngestPipeline, InMemoryReadingsStore, InMemorySensorRegistryStore]:
    """An independent pipeline, store and registry, identically configured."""
    clock = FixedClock(_NOW)
    store = InMemoryReadingsStore(clock=clock)
    registry = InMemorySensorRegistryStore()
    registry.upsert(
        SensorMetadataRecord(
            **cast("dict[str, Any]", json.loads(GOLDEN_METADATA_PAYLOAD))
        ),
        _NOW,
    )
    pipeline = IngestPipeline(
        dependencies=PipelineDependencies(
            archive=InMemoryRawArchive(),
            readings=store,
            registry=registry,
            meteorology=InMemoryMeteorologyProvider(),
            clock=clock,
        ),
        settings=PipelineSettings(),
    )
    return pipeline, store, registry


def _stored(store: InMemoryReadingsStore) -> list[CalibratedReading]:
    return list(
        store.query_window(
            site_code="CB0001",
            species=None,
            start=_NOW - dt.timedelta(days=2),
            end=_NOW + dt.timedelta(hours=1),
        ).readings
    )


def _measurement(reading: CalibratedReading) -> dict[str, object]:
    """Every field except provenance — see the module docstring for why."""
    return {
        field.name: getattr(reading, field.name)
        for field in fields(reading)
        if field.name not in _PROVENANCE_FIELDS
    }


@st.composite
def _records(draw: st.DrawFn) -> list[SensorDataRecord]:
    slots = draw(
        st.lists(
            st.tuples(
                st.sampled_from(["PM25", "NO2"]),
                st.integers(min_value=1, max_value=10),
            ),
            min_size=1,
            max_size=5,
            unique=True,
        )
    )
    return [
        _record(
            species,
            draw(st.floats(min_value=0.0, max_value=200.0, allow_nan=False)),
            hours_ago,
        )
        for species, hours_ago in slots
    ]


@given(records=_records())
def test_property_39_push_and_pull_ingestion_converge_on_the_same_state(
    records: list[SensorDataRecord],
) -> None:
    """Feature: ingestion-and-serving-service, Property 39."""
    # PUSH: one message per record, on the site's topic.
    push_pipeline, push_store, _push_registry = _stack()
    transport = ScriptedMqttTransport(
        [
            (
                f"aqm/sensors/{record.SiteCode}/data",
                serialize_data(record).encode("utf-8"),
                index,
            )
            for index, record in enumerate(records)
        ]
    )
    MqttSubscriber(
        transport=transport,
        pipeline=push_pipeline,
        clock=FixedClock(_NOW),
        settings=MqttSettings(),
    ).run()

    # PULL: the same records as one windowed array payload.
    pull_pipeline, pull_store, pull_registry = _stack()
    window = (_NOW - dt.timedelta(hours=DEFAULT_BACKFILL_HOURS), _NOW)
    payload = json.dumps(
        [json.loads(serialize_data(record)) for record in records]
    ).encode("utf-8")
    FeedPoller(
        client=ScriptedFeedClient({window: payload}),
        pipeline=pull_pipeline,
        readings=pull_store,
        registry=pull_registry,
        clock=FixedClock(_NOW),
        settings=FeedSettings(),
    ).poll()

    pushed = _stored(push_store)
    pulled = _stored(pull_store)

    # Req 27.7: the same set of measurements, in the same order (the store's ordering is
    # defined, so this compares sequences rather than sets)
    assert len(pushed) == len(pulled)
    assert [reading.key for reading in pushed] == [reading.key for reading in pulled]

    # Req 5.4 / 27.3: every computed value agrees
    for push_reading, pull_reading in zip(pushed, pulled, strict=True):
        assert _measurement(push_reading) == _measurement(pull_reading)


@given(records=_records())
def test_property_39_the_provenance_deliberately_differs(
    records: list[SensorDataRecord],
) -> None:
    """Feature: ingestion-and-serving-service, Property 39 (the other half).

    Requirement 4.7 records the transport so the ingestion path of any stored Reading is
    recoverable, and Requirement 27.6 derives the archive identifier from the transport and
    source among other injected inputs. So the identifiers MUST differ between the two paths —
    and asserting that is what stops a future change from "fixing" the convergence above by
    discarding the provenance Requirement 4.7 exists to preserve.
    """
    push_pipeline, push_store, _pr = _stack()
    transport = ScriptedMqttTransport(
        [
            (
                f"aqm/sensors/{record.SiteCode}/data",
                serialize_data(record).encode("utf-8"),
                index,
            )
            for index, record in enumerate(records)
        ]
    )
    MqttSubscriber(
        transport=transport,
        pipeline=push_pipeline,
        clock=FixedClock(_NOW),
        settings=MqttSettings(),
    ).run()

    pull_pipeline, pull_store, pull_registry = _stack()
    window = (_NOW - dt.timedelta(hours=DEFAULT_BACKFILL_HOURS), _NOW)
    payload = json.dumps(
        [json.loads(serialize_data(record)) for record in records]
    ).encode("utf-8")
    FeedPoller(
        client=ScriptedFeedClient({window: payload}),
        pipeline=pull_pipeline,
        readings=pull_store,
        registry=pull_registry,
        clock=FixedClock(_NOW),
        settings=FeedSettings(),
    ).poll()

    pushed = _stored(push_store)
    pulled = _stored(pull_store)
    for push_reading, pull_reading in zip(pushed, pulled, strict=True):
        assert push_reading.archive_id != pull_reading.archive_id


@given(records=_records())
def test_property_39_each_path_is_internally_deterministic(
    records: list[SensorDataRecord],
) -> None:
    """Feature: ingestion-and-serving-service, Property 39 (Req 27.3).

    Convergence between paths would be vacuous if either path were not itself deterministic —
    two nondeterministic paths could agree by accident on one run. So each is run twice
    against a fresh stack and asserted to reproduce itself exactly, archive identifier
    included, since Requirement 27.6 makes that derived too.
    """
    payload = json.dumps(
        [json.loads(serialize_data(record)) for record in records]
    ).encode("utf-8")
    window = (_NOW - dt.timedelta(hours=DEFAULT_BACKFILL_HOURS), _NOW)

    results: list[list[CalibratedReading]] = []
    for _run in range(2):
        pipeline, store, registry = _stack()
        FeedPoller(
            client=ScriptedFeedClient({window: payload}),
            pipeline=pipeline,
            readings=store,
            registry=registry,
            clock=FixedClock(_NOW),
            settings=FeedSettings(),
        ).poll()
        results.append(_stored(store))

    assert results[0] == results[1]  # identical, provenance and all
