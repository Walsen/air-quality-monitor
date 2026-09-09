"""Deriving an archive entry's identifier and key.

This lives beside :class:`~aqm_ingestion.ports.protocols.ArchiveMeta` rather than in
the pipeline or in one adapter, because it is the CONTRACT between the archive port
and every adapter implementing it: the in-memory adapter, the S3 adapter, and the
pipeline that writes through them must agree byte-for-byte, or a replay lands in a
different place than the original write. Requirement 16.8 tests both adapters
against one shared suite for the same reason.

It contains no cloud types and no I/O — just arithmetic over the metadata.
"""

from __future__ import annotations

import datetime as dt
import hashlib

from aqm_ingestion.ports.protocols import ArchiveMeta

_KEY_TEMPLATE = "raw/{year:04d}/{month:02d}/{day:02d}/{hour:02d}/{archive_id}.json"


def require_aware(instant: dt.datetime) -> dt.datetime:
    """Return the instant in UTC, refusing a naive one.

    A naive datetime would be read as local time, which would file the entry under
    an ambiguous hour and break the time-ordered enumeration of Requirement 16.5.
    """
    if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
        raise ValueError(
            "archive instant must be timezone-aware; a naive value would place the "
            f"entry in an ambiguous hour: {instant!r}"
        )
    return instant.astimezone(dt.UTC)


def derive_archive_id(payload: bytes, meta: ArchiveMeta) -> str:
    """Derive the archive identifier from the payload and its metadata.

    Derived rather than random so replaying the same payload reproduces the same
    identifier (Requirement 27.6). Each component is separated by a NUL byte so two
    different field splits cannot produce the same digest input. Hex output drops
    straight into a key path without escaping.
    """
    digest = hashlib.sha256()
    digest.update(payload)
    digest.update(b"\x00")
    digest.update(meta.transport.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(meta.source.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(require_aware(meta.ingested_at).isoformat().encode("utf-8"))
    return digest.hexdigest()


def archive_key(ingested_at: dt.datetime, archive_id: str) -> str:
    """Build the time-ordered archive key (Requirement 16.5).

    Every component is zero-padded so LEXICAL order follows CHRONOLOGICAL order,
    which is what lets a time range be enumerated without scanning the archive.
    """
    instant = require_aware(ingested_at)
    return _KEY_TEMPLATE.format(
        year=instant.year,
        month=instant.month,
        day=instant.day,
        hour=instant.hour,
        archive_id=archive_id,
    )
