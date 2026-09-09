"""Unit tests for the raw archive and archive-first ordering (tasks 4.1, 4.3).

- Req 16.1: the payload is archived exactly as received, BEFORE parsing,
  validation, deduplication, or calibration.
- Req 16.2: the entry records the ingestion instant, transport, source topic or
  request window, and an archive identifier that every derived reading carries.
- Req 16.3: append-only — ingestion and serving never update or delete an entry.
- Req 16.4: reading an entry back yields identical bytes.
- Req 16.5: the key is derived from the instant and identifier, time-ordered, with
  the default layout raw/{yyyy}/{MM}/{dd}/{HH}/{archive_id}.json.
- Req 16.6: no feed credential, bearer credential, or profile field is archived.
- Req 16.7 / 4.6: a failed write processes NOTHING, logs one error naming the
  failure kind, and does not acknowledge the message.
- Req 27.6: the identifier is derived from injected inputs so a replay reproduces it.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from aqm_ingestion.adapters.memory import InMemoryRawArchive
from aqm_ingestion.ingest.archive import (
    ArchiveWriteFailedError,
    archive_key,
    archive_payload,
    derive_archive_id,
)
from aqm_ingestion.ports.protocols import ArchiveMeta

_T0 = dt.datetime(2026, 7, 1, 9, 5, 30, tzinfo=dt.UTC)
_PAYLOAD = b'{"Species":"PM25","ScaledValue":14.27}'


def _meta(**overrides: object) -> ArchiveMeta:
    base = {
        "site_code": "CB0001",
        "transport": "mqtt",
        "received_at": _T0,
    }
    return ArchiveMeta(**(base | overrides))  # type: ignore[arg-type]


# --- Req 16.5 the derived key --------------------------------------------

def test_key_uses_the_documented_layout() -> None:
    key = archive_key(_T0, "abc123")
    assert key == "raw/2026/07/01/09/abc123.json"


def test_key_zero_pads_every_component() -> None:
    key = archive_key(dt.datetime(2026, 1, 2, 3, tzinfo=dt.UTC), "id")
    assert key == "raw/2026/01/02/03/id.json"


def test_key_is_time_ordered_lexically() -> None:
    # Req 16.5 wants a layout a time range can be enumerated from, which needs
    # lexical order to follow chronological order
    earlier = archive_key(dt.datetime(2026, 7, 1, 9, tzinfo=dt.UTC), "a")
    later = archive_key(dt.datetime(2026, 7, 1, 10, tzinfo=dt.UTC), "a")
    assert earlier < later


def test_key_uses_utc_regardless_of_supplied_offset() -> None:
    offset = dt.timezone(dt.timedelta(hours=5))
    key = archive_key(dt.datetime(2026, 7, 1, 14, tzinfo=offset), "id")
    assert key == "raw/2026/07/01/09/id.json"  # same instant in UTC


def test_naive_instant_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        archive_key(dt.datetime(2026, 7, 1, 9), "id")


# --- Req 27.6 derived, reproducible identifier ---------------------------

def test_identifier_is_reproducible_for_the_same_inputs() -> None:
    assert derive_archive_id(_PAYLOAD, _meta()) == derive_archive_id(_PAYLOAD, _meta())


def test_identifier_changes_with_the_payload() -> None:
    assert derive_archive_id(_PAYLOAD, _meta()) != derive_archive_id(b"other", _meta())


def test_identifier_changes_with_the_instant() -> None:
    other = _meta(received_at=_T0 + dt.timedelta(seconds=1))
    assert derive_archive_id(_PAYLOAD, _meta()) != derive_archive_id(_PAYLOAD, other)


def test_identifier_changes_with_the_transport() -> None:
    assert derive_archive_id(_PAYLOAD, _meta()) != derive_archive_id(
        _PAYLOAD, _meta(transport="feed")
    )


def test_identifier_is_filename_safe() -> None:
    identifier = derive_archive_id(_PAYLOAD, _meta())
    assert identifier.isalnum()  # goes straight into a key path


# --- Req 16.1 / 16.4 archive-first and round-trip ------------------------

def test_payload_is_archived_verbatim() -> None:
    archive = InMemoryRawArchive()
    result = archive_payload(archive, _PAYLOAD, _meta())
    assert archive.read(result.archive_id) == _PAYLOAD


def test_result_reports_the_key_and_identifier() -> None:
    result = archive_payload(InMemoryRawArchive(), _PAYLOAD, _meta())
    assert result.archive_id
    assert result.key.startswith("raw/2026/07/01/09/")
    assert result.key.endswith(".json")


def test_archiving_does_not_parse_the_payload() -> None:
    # Req 16.1: archiving precedes parsing, so even unparseable bytes archive
    archive = InMemoryRawArchive()
    result = archive_payload(archive, b"not json at all", _meta())
    assert archive.read(result.archive_id) == b"not json at all"


def test_bytes_are_unchanged_by_a_round_trip() -> None:
    archive = InMemoryRawArchive()
    payload = json.dumps({"a": 1}).encode("utf-8")
    result = archive_payload(archive, payload, _meta())
    assert archive.read(result.archive_id) == payload


# --- Req 16.3 append-only ------------------------------------------------

def test_archive_port_exposes_no_update_or_delete() -> None:
    # append-only is enforced by the SHAPE of the port, not by a convention
    from aqm_ingestion.ports.protocols import RawArchive

    methods = {name for name in vars(RawArchive) if not name.startswith("_")}
    assert methods == {"write", "read"}
    for forbidden in ("update", "delete", "remove", "overwrite"):
        assert forbidden not in methods


def test_rearchiving_the_same_payload_is_idempotent() -> None:
    archive = InMemoryRawArchive()
    first = archive_payload(archive, _PAYLOAD, _meta())
    second = archive_payload(archive, _PAYLOAD, _meta())
    assert first.archive_id == second.archive_id
    assert archive.read(first.archive_id) == _PAYLOAD


# --- Req 16.6 never archive a credential or profile field ---------------

@pytest.mark.parametrize(
    "meta_field", ["Authorization", "api_key", "bearer_token", "asthma"]
)
def test_metadata_carries_no_credential_or_profile_field(meta_field: str) -> None:
    # ArchiveMeta's own shape is the guard: it has no field to put one in
    assert meta_field not in {f for f in ArchiveMeta.__dataclass_fields__}


def test_archive_meta_fields_are_exactly_the_documented_set() -> None:
    assert set(ArchiveMeta.__dataclass_fields__) == {
        "site_code",
        "transport",
        "received_at",
        "content_type",
    }


# --- Req 16.7 / 4.6 write failure ---------------------------------------

class FailingArchive:
    """A RawArchive whose write always fails."""

    def __init__(self) -> None:
        """Count attempts so a silent retry would be visible."""
        self.write_attempts = 0

    def write(self, payload: bytes, meta: ArchiveMeta) -> str:
        """Always fail, as an unavailable archive would."""
        self.write_attempts += 1
        raise OSError("archive unavailable")

    def read(self, archive_id: str) -> bytes:
        """Nothing was ever written, so every read misses."""
        raise KeyError(archive_id)


def test_write_failure_raises_a_named_error() -> None:
    with pytest.raises(ArchiveWriteFailedError) as caught:
        archive_payload(FailingArchive(), _PAYLOAD, _meta())
    assert "OSError" in str(caught.value)  # names the failure kind


def test_write_failure_logs_one_error(capsys: pytest.CaptureFixture[str]) -> None:
    from aqm_ingestion.observability.logging import configure_logging

    configure_logging("info")
    with pytest.raises(ArchiveWriteFailedError):
        archive_payload(FailingArchive(), _PAYLOAD, _meta())
    events = [
        json.loads(line)
        for line in capsys.readouterr().out.strip().splitlines()
        if line
    ]
    errors = [event for event in events if event["level"] == "error"]
    assert len(errors) == 1
    assert errors[0]["event"] == "archive_write_failed"
    assert errors[0]["error_type"] == "OSError"


def test_write_failure_never_logs_the_payload(
    capsys: pytest.CaptureFixture[str]
) -> None:
    from aqm_ingestion.observability.logging import configure_logging

    configure_logging("info")
    with pytest.raises(ArchiveWriteFailedError):
        archive_payload(FailingArchive(), b'{"secret":"do-not-log"}', _meta())
    assert "do-not-log" not in capsys.readouterr().out


def test_failure_processes_nothing() -> None:
    # the caller gets an exception, so no derived record can exist (Req 16.7)
    archive = FailingArchive()
    with pytest.raises(ArchiveWriteFailedError):
        archive_payload(archive, _PAYLOAD, _meta())
    assert archive.write_attempts == 1  # attempted once, not retried silently
