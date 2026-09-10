"""Archiving a payload before anything derives from it.

Requirement 16.1 puts this FIRST in the pipeline: the payload is archived exactly
as received, before parsing, validation, deduplication or calibration. That
ordering is the whole point — a stored value can only be replayed or reprocessed
after a calibration change if the bytes that produced it were kept, and a payload
that failed to parse is often the one most worth keeping.

So this module deliberately does not look inside the payload. It takes bytes.

The identifier and key derivation live in
:mod:`aqm_ingestion.ports.archive_key`, beside the metadata dataclass, so this
module and every archive adapter share one copy (§1). What this module owns is the
ORDERING and the failure contract.

On failure the caller gets :class:`ArchiveWriteFailedError` and nothing proceeds
(Requirement 16.7): the push path leaves the message unacknowledged and the pull
path fails the invocation. The failure is logged once, naming the failure kind but
never the payload — an unparseable payload may hold anything (§6, §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from aqm_ingestion.observability.logging import get_logger, log_handled_error
from aqm_ingestion.ports.archive_key import archive_key, derive_archive_id
from aqm_ingestion.ports.protocols import ArchiveMeta

__all__ = [
    "ArchiveOutcome",
    "ArchiveWriteFailedError",
    "archive_key",
    "archive_payload",
    "derive_archive_id",
]

_logger = get_logger("ingest.archive")


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


def archive_payload(
    archive: _ArchiveWriter, payload: bytes, meta: ArchiveMeta
) -> ArchiveOutcome:
    """Archive a payload verbatim, before anything derives from it.

    Args:
        archive: the RawArchive port to write through.
        payload: the bytes exactly as received — NOT parsed or normalised.
        meta: the ingestion instant, transport, and source topic or request window.

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
            source=meta.source,
            payload_bytes=len(payload),
        )
        raise ArchiveWriteFailedError(
            f"archive write failed ({type(error).__name__}); payload not processed"
        ) from error

    return ArchiveOutcome(
        archive_id=archive_id,
        key=archive_key(meta.ingested_at, archive_id),
    )
