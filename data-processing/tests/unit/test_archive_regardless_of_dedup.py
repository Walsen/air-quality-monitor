"""Unit test for archive-regardless-of-dedup (task 6.4, Requirement 7.10).

The archive records what ARRIVED, not what was stored. A discarded duplicate is
exactly the case where the two diverge, so it is the case worth pinning: a payload
that changes nothing in the ReadingsStore must still leave an archive entry, or the
archive silently under-reports what the transport delivered.

Requirement 7.9 pairs with this — deduplication runs BEFORE calibration, so a
duplicate consumes no correction work. Both are ordering claims about the pipeline,
so they are asserted the same way: by recording the order of operations.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

from aqm_ingestion.adapters.memory import InMemoryRawArchive
from aqm_ingestion.contract.records import SensorDataRecord
from aqm_ingestion.contract.serializer import serialize_data
from aqm_ingestion.domain.dedup import DedupAction, resolve_duplicate
from aqm_ingestion.ingest.archive import archive_payload
from aqm_ingestion.ports.protocols import ArchiveMeta
from tests.unit.test_records import GOLDEN_DATA_PAYLOAD

_T0 = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_MOMENT = "2026-07-01T11:00:00Z"


def _record(**overrides: object) -> SensorDataRecord:
    fields = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD)) | overrides
    fields.setdefault("DateTime", _MOMENT)
    return SensorDataRecord(**fields)


def _meta(instant: dt.datetime) -> ArchiveMeta:
    return ArchiveMeta(ingested_at=instant, transport="mqtt", source="aqm/london/data")


def test_a_discarded_duplicate_is_still_archived() -> None:
    archive = InMemoryRawArchive()
    record = _record()
    payload = serialize_data(record).encode("utf-8")

    # first delivery: archived and stored
    first = archive_payload(archive, payload, _meta(_T0))
    assert resolve_duplicate(stored=None, incoming=record).action is DedupAction.STORE

    # re-delivery of the SAME record: dedup writes nothing to the readings store...
    resolution = resolve_duplicate(stored=record, incoming=record)
    assert resolution.action is DedupAction.NO_WRITE

    # ...but the payload is archived all the same, under its own entry, because the
    # archive records what arrived (Req 7.10)
    second = archive_payload(archive, payload, _meta(_T0 + dt.timedelta(minutes=5)))
    assert archive.read(second.archive_id) == payload
    assert second.archive_id != first.archive_id  # a distinct delivery, distinct entry


def test_a_rejected_provisional_is_still_archived() -> None:
    # Req 7.5 discards the incoming record entirely; the archive must not follow suit
    archive = InMemoryRawArchive()
    stored = _record(RatificationStatus="R", ScaledValue=3.0)
    incoming = _record(RatificationStatus="P", ScaledValue=99.0)
    payload = serialize_data(incoming).encode("utf-8")

    outcome = archive_payload(archive, payload, _meta(_T0))
    resolution = resolve_duplicate(stored=stored, incoming=incoming)

    assert resolution.action is DedupAction.NO_WRITE  # nothing stored
    assert archive.read(outcome.archive_id) == payload  # yet fully archived


def test_archiving_precedes_the_dedup_decision() -> None:
    # Req 16.1 puts the archive first and Req 7.10 keeps it unconditional; together
    # they mean the archive write cannot be skipped by an early dedup exit
    journal: list[str] = []

    class RecordingArchive:
        def __init__(self) -> None:
            self._inner = InMemoryRawArchive()

        def write(self, payload: bytes, meta: ArchiveMeta) -> str:
            journal.append("archive")
            return self._inner.write(payload, meta)

    record = _record()
    payload = serialize_data(record).encode("utf-8")
    archive_payload(RecordingArchive(), payload, _meta(_T0))
    resolve_duplicate(stored=record, incoming=record)
    journal.append("dedup")

    assert journal == ["archive", "dedup"]


def test_every_delivery_of_a_duplicate_leaves_its_own_entry() -> None:
    # an at-least-once transport may deliver the same payload many times; each
    # delivery is a fact about the transport and is archived separately
    archive = InMemoryRawArchive()
    record = _record()
    payload = serialize_data(record).encode("utf-8")

    ids = {
        archive_payload(
            archive, payload, _meta(_T0 + dt.timedelta(minutes=minute))
        ).archive_id
        for minute in range(4)
    }
    assert len(ids) == 4
    for archive_id in ids:
        assert archive.read(archive_id) == payload
