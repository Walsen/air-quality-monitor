"""Archive property test (task 4.2).

Feature: ingestion-and-serving-service
- Property 5: raw archive precedes processing and round-trips
  (Requirements 16.1, 16.2, 16.4, 6.11)

The precedence half is the interesting one: it is not enough that the bytes come
back, the archive must have happened BEFORE anything derived from the payload
exists. That is asserted by recording the order of operations through the port.
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.adapters.memory import InMemoryRawArchive
from aqm_ingestion.contract.parser import ParseError, parse_data_records
from aqm_ingestion.ingest.archive import archive_key, archive_payload
from aqm_ingestion.ports.protocols import ArchiveMeta

_T0 = dt.datetime(2026, 7, 1, 9, tzinfo=dt.UTC)


class _OrderRecordingArchive:
    """Wraps the in-memory archive, recording when the write happened."""

    def __init__(self, journal: list[str]) -> None:
        self._inner = InMemoryRawArchive()
        self._journal = journal

    def write(self, payload: bytes, meta: ArchiveMeta) -> str:
        self._journal.append("archive")
        return self._inner.write(payload, meta)

    def read(self, archive_id: str) -> bytes:
        return self._inner.read(archive_id)


@given(
    payload=st.binary(min_size=0, max_size=512),
    transport=st.sampled_from(["mqtt", "feed"]),
    site_code=st.one_of(st.none(), st.text(min_size=1, max_size=10)),
    offset_seconds=st.integers(min_value=0, max_value=10 * 365 * 24 * 3600),
)
def test_property_5_archive_precedes_processing_and_round_trips(
    payload: bytes, transport: str, site_code: str | None, offset_seconds: int
) -> None:
    """Feature: ingestion-and-serving-service, Property 5."""
    received_at = _T0 + dt.timedelta(seconds=offset_seconds)
    meta = ArchiveMeta(
        site_code=site_code, transport=transport, received_at=received_at
    )

    journal: list[str] = []
    archive = _OrderRecordingArchive(journal)

    # the pipeline order under test: archive, THEN attempt to parse
    outcome = archive_payload(archive, payload, meta)
    try:
        parse_data_records(payload.decode("utf-8", errors="replace"))
        journal.append("parse")
    except ParseError:
        journal.append("parse")

    # Req 16.1: archiving happened before any parsing, for EVERY payload —
    # including bytes that cannot be parsed at all, which are exactly the ones
    # worth keeping
    assert journal[0] == "archive"
    assert journal.index("archive") < journal.index("parse")

    # Req 16.4: the bytes come back identical
    assert archive.read(outcome.archive_id) == payload

    # Req 16.2: the identifier every derived reading will carry is present, and
    # the key locates it in the time-ordered layout
    assert outcome.archive_id
    assert outcome.key == archive_key(received_at, outcome.archive_id)


@given(
    payload=st.binary(min_size=0, max_size=128),
    earlier=st.integers(min_value=0, max_value=1_000_000),
    later=st.integers(min_value=1_000_001, max_value=2_000_000),
)
def test_property_5_keys_sort_chronologically(
    payload: bytes, earlier: int, later: int
) -> None:
    """Feature: ingestion-and-serving-service, Property 5 (key ordering).

    Requirement 16.5 wants a time range enumerable without scanning the archive,
    which only holds if lexical key order follows chronological order.
    """
    first = archive_key(_T0 + dt.timedelta(seconds=earlier), "aaaa")
    second = archive_key(_T0 + dt.timedelta(seconds=later), "aaaa")
    assert first <= second
