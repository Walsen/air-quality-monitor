"""Archiving a payload before anything derives from it.

Requirement 16.1 puts this FIRST in the pipeline: the payload is archived exactly
as received, before parsing, validation, deduplication or calibration. That
ordering is the whole point — a stored value can only be replayed or reprocessed
after a calibration change if the bytes that produced it were kept, and a payload
that failed to parse is often the one worth keeping.

So this module deliberately does not look inside the payload. It takes bytes.

Two derivations live here so both archive adapters share them rather than each
inventing its own (§1):

- :func:`derive_archive_id` builds the identifier from the payload and its
  metadata, never from a random source, so replaying the same payload reproduces
  the same identifier (Requirement 27.6).
- :func:`archive_key` lays the entry out as
  ``raw/{yyyy}/{MM}/{dd}/{HH}/{archive_id}.json`` (Requirement 16.5). Every
  component is zero-padded so lexical order follows chronological order, which is
  what lets a time range be enumerated without scanning the whole archive.

On failure the caller gets :class:`ArchiveWriteFailedError` and nothing proceeds
(Requirement 16.7): the push path leaves the message unacknowledged and the pull
path fails the invocation. The failure is logged once, naming the failure kind but
never the payload — an unparseable payload may hold anything (§6, §7).
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Protocol

from aqm_ingestion.observability.logging import get_logger, log_handled_error
from aqm_ingestion.ports.protocols import ArchiveMeta

_logger = get_logger("ingest.archive")
_KEY_TEMPLATE = "raw/{year:04d}/{month:02d}/{day:02d}/{hour:02d}/{archive_id}.json"


class _ArchiveWriter(Protocol):
    """The slice of RawArchive this module needs (§1 Interface Segregation)."""

    def write(self, payload: bytes, meta: ArchiveMeta) -> str:
        """Archive a payload and return its identifier."""
        ...


class ArchiveWriteFailedError(RuntimeError):
    """The archive write failed, so nothing may be processed (Requirement 16.7)."""


@dataclass(frozen=True, slots=True)
class ArchiveOutcome:
    """What was archived and where, for every derived reading to reference."""

    archive_id: str
    key: str


def _require_aware(instant: dt.datetime) -> dt.datetime:
    if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
        raise ValueError(
            "archive instant must be timezone-aware; a naive value would place the "
            f"entry in an ambiguous hour: {instant!r}"
        )
    return instant.astimezone(dt.UTC)


def derive_archive_id(payload: bytes, meta: ArchiveMeta) -> str:
    """Derive the archive identifier from the payload and its metadata.

    Derived rather than random so a replay reproduces it (Requirement 27.6), and
    hex so it drops straight into a key path without escaping.
    """
    digest = hashlib.sha256()
    digest.update(payload)
    digest.update(b"\x00")
    digest.update(str(meta.site_code).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(meta.transport.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(_require_aware(meta.received_at).isoformat().encode("utf-8"))
    return digest.hexdigest()


def archive_key(received_at: dt.datetime, archive_id: str) -> str:
    """Build the time-ordered archive key (Requirement 16.5)."""
    instant = _require_aware(received_at)
    return _KEY_TEMPLATE.format(
        year=instant.year,
        month=instant.month,
        day=instant.day,
        hour=instant.hour,
        archive_id=archive_id,
    )


def archive_payload(
    archive: _ArchiveWriter, payload: bytes, meta: ArchiveMeta
) -> ArchiveOutcome:
    """Archive a payload verbatim, before anything derives from it.

    Args:
        archive: the RawArchive port to write through.
        payload: the bytes exactly as received — NOT parsed or normalised.
        meta: the ingestion instant, transport, and source.

    Returns:
        The identifier and key, so every derived reading can name its origin
        (Requirement 16.2).

    Raises:
        ArchiveWriteFailedError: if the write failed. The caller must process
            nothing: leave the message unacknowledged on the push path, fail the
            invocation on the pull path (Requirement 16.7).
    """
    try:
        archive_id = archive.write(payload, meta)
    except (OSError, TimeoutError, RuntimeError) as error:
        # Logged once here, naming the failure KIND. The payload is never logged:
        # a payload that failed to archive may hold anything at all (§6, §7).
        log_handled_error(
            _logger,
            "archive_write_failed",
            error,
            transport=meta.transport,
            SiteCode=meta.site_code,
            payload_bytes=len(payload),
        )
        raise ArchiveWriteFailedError(
            f"archive write failed ({type(error).__name__}); payload not processed"
        ) from error

    return ArchiveOutcome(
        archive_id=archive_id,
        key=archive_key(meta.received_at, archive_id),
    )
