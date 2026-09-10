"""Pipeline property test (task 15.2).

Feature: ingestion-and-serving-service
- Property 7: ingestion is idempotent (Requirements 7.8, 7.3)

This is the END-TO-END counterpart to Property 8. Property 8 asserts that the resolution
FUNCTION converges; this asserts that the assembled pipeline does — that re-ingesting a
payload leaves the ReadingsStore in the state one ingestion produced, and leaves the count
of stored Readings unchanged after the first.

The distinction matters because a correct resolution function can still be assembled into a
non-idempotent pipeline: a stage that appended rather than resolved, or a dedup check placed
after the store write, would pass Property 8 and fail here.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.adapters.memory import (
    InMemoryMeteorologyProvider,
    InMemoryRawArchive,
    InMemoryReadingsStore,
    InMemorySensorRegistryStore,
)
from aqm_ingestion.contract.records import SensorDataRecord
from aqm_ingestion.contract.serializer import serialize_data
from aqm_ingestion.ingest.pipeline import (
    IngestPipeline,
    PipelineDependencies,
    PipelineSettings,
)
from aqm_ingestion.ports.clock import FixedClock
from aqm_ingestion.ports.protocols import ArchiveMeta
from tests.unit.test_records import GOLDEN_DATA_PAYLOAD

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _record(
    species: str, value: float, hours_ago: int, status: str
) -> SensorDataRecord:
    moment = (_NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD)) | {
        "Species": species,
        "Units": "ug.m-3",
        "ScaledValue": value,
        "DateTime": moment,
        "RatificationStatus": status,
    }
    return SensorDataRecord(**fields)


def _payload(records: list[SensorDataRecord]) -> bytes:
    body = [json.loads(serialize_data(record)) for record in records]
    return json.dumps(body).encode("utf-8")


def _fresh() -> tuple[IngestPipeline, InMemoryReadingsStore]:
    clock = FixedClock(_NOW)
    store = InMemoryReadingsStore(clock=clock)
    pipeline = IngestPipeline(
        dependencies=PipelineDependencies(
            archive=InMemoryRawArchive(),
            readings=store,
            registry=InMemorySensorRegistryStore(),
            meteorology=InMemoryMeteorologyProvider(),
            clock=clock,
        ),
        settings=PipelineSettings(),
    )
    return pipeline, store


def _meta() -> ArchiveMeta:
    return ArchiveMeta(ingested_at=_NOW, transport="mqtt", source="aqm/london/data")


def _stored(store: InMemoryReadingsStore) -> list[Any]:
    result = store.query_window(
        site_code="CB0001",
        species=None,
        start=_NOW - dt.timedelta(days=30),
        end=_NOW + dt.timedelta(hours=1),
    )
    return list(result.readings)


# One record per (species, hour) so the payload holds no internal duplicates: this
# property is about RE-INGESTION, which a payload that already conflicted with itself would
# confound.
@st.composite
def _records(draw: st.DrawFn) -> list[SensorDataRecord]:
    slots = draw(
        st.lists(
            st.tuples(
                st.sampled_from(["PM25", "NO2"]),
                st.integers(min_value=1, max_value=20),
            ),
            min_size=1,
            max_size=6,
            unique=True,
        )
    )
    return [
        _record(
            species,
            draw(st.floats(min_value=0.0, max_value=200.0, allow_nan=False)),
            hours_ago,
            draw(st.sampled_from(["P", "R"])),
        )
        for species, hours_ago in slots
    ]


@given(records=_records())
def test_property_7_ingestion_is_idempotent(records: list[SensorDataRecord]) -> None:
    """Feature: ingestion-and-serving-service, Property 7."""
    payload = _payload(records)

    pipeline, store = _fresh()
    first = pipeline.ingest(payload, _meta())
    after_one = _stored(store)

    second = pipeline.ingest(payload, _meta())
    after_two = _stored(store)

    # Req 7.8: the store is in the state one ingestion produced
    assert after_two == after_one

    # Req 7.8: and the COUNT is unchanged after the first
    assert len(after_two) == len(after_one)

    # Req 7.3: the re-delivery is counted as duplicate, not accepted again
    assert second.accepted == 0
    assert second.deduplicated == first.accepted
    assert second.quarantined == 0


@given(records=_records())
def test_property_7_a_third_ingestion_changes_nothing_further(
    records: list[SensorDataRecord],
) -> None:
    """Feature: ingestion-and-serving-service, Property 7 (stability).

    Requirement 7.8 says "two or more times", so the state must be a fixed point rather
    than merely stable between the first and second pass.
    """
    payload = _payload(records)
    pipeline, store = _fresh()
    pipeline.ingest(payload, _meta())
    pipeline.ingest(payload, _meta())
    twice = _stored(store)
    pipeline.ingest(payload, _meta())
    assert _stored(store) == twice


@given(records=_records())
def test_property_7_re_ingestion_is_still_archived(
    records: list[SensorDataRecord],
) -> None:
    """Feature: ingestion-and-serving-service, Property 7 (Req 7.10 interaction).

    Idempotence applies to the ReadingsStore, NOT to the archive: Requirement 7.10 says
    every received payload is archived regardless of the deduplication outcome, so a
    re-delivery still produces an archive entry even though it stores no Reading.
    """
    payload = _payload(records)
    pipeline, _store = _fresh()
    first = pipeline.ingest(payload, _meta())
    second = pipeline.ingest(payload, _meta())
    # the same payload and metadata derive the same archive id, so re-archiving is
    # idempotent by derivation rather than by being skipped
    assert second.archive_id == first.archive_id
    assert second.archive_id


@given(
    values=st.lists(
        st.floats(min_value=0.0, max_value=200.0, allow_nan=False),
        min_size=2,
        max_size=5,
    ),
    statuses=st.lists(st.sampled_from(["P", "R"]), min_size=2, max_size=5),
)
def test_property_7_same_key_records_reach_the_same_state_in_any_order(
    values: list[float], statuses: list[str]
) -> None:
    """Feature: ingestion-and-serving-service, Property 7 (Req 7.7 through the pipeline).

    Requirement 7.7 scopes order independence to records SHARING ONE Dedup_Key, and that is
    what is asserted here: the same competing values for one interval converge on the same
    stored Reading whichever order the pipeline sees them in.

    It is deliberately NOT generalised to records with DIFFERENT keys, and an earlier draft
    of this test that did so was refuted — correctly. A NowCast is defined over the hours
    AVAILABLE when it is computed (Requirement 11.2), so a PM2.5 record processed before its
    neighbours genuinely has a smaller window than the same record processed after them. That
    is history dependence by design, not an ordering defect, and Requirement 7.7 does not
    claim otherwise. Asserting it would have demanded the pipeline behave in a way the spec
    never asks for.
    """
    pairs = list(zip(values, statuses, strict=False))
    records = [
        _record("PM25", value, hours_ago=3, status=status) for value, status in pairs
    ]

    forward_pipeline, forward_store = _fresh()
    for record in records:
        forward_pipeline.ingest(_payload([record]), _meta())

    reverse_pipeline, reverse_store = _fresh()
    for record in reversed(records):
        reverse_pipeline.ingest(_payload([record]), _meta())

    forward = _stored(forward_store)
    reverse = _stored(reverse_store)
    assert len(forward) == len(reverse) == 1
    assert forward[0].reported_value == reverse[0].reported_value
    assert forward[0].ratification_status == reverse[0].ratification_status
