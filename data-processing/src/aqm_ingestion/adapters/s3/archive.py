"""The S3 raw archive adapter.

Requirements 16.5, 16.8, 16.9.

THE KEY AND THE IDENTIFIER ARE DERIVED BY ``ports/archive_key.py``, NOT HERE. That module exists
precisely so this adapter and the in-memory one cannot disagree about where a replay lands: if
they
derived independently, a payload archived in production and replayed against the offline stack
would
be looked for in a different place, and nothing would fail until someone needed the bytes. The
shared contract suite asserts the agreement directly, which is what Requirement 16.8's one-suite
rule is for.

THIS ADAPTER IMPOSES NO EXPIRY OF ITS OWN (Requirement 16.9). Archive retention and the
storage-class transition are DEPLOYMENT concerns — a lifecycle policy on the bucket, which
an operator can see and change — and a service that also expired objects would give two
places to look when bytes went missing, one of them invisible from the console.

APPEND-ONLY IS THE PORT'S SHAPE, NOT A PROMISE HERE (Requirement 16.3): RawArchive exposes only
write and read, so there is no delete to call. The shared suite asserts that for every adapter.
"""

from __future__ import annotations

import boto3
from botocore.exceptions import ClientError

from aqm_ingestion.observability.logging import get_logger
from aqm_ingestion.ports.archive_key import archive_key, derive_archive_id
from aqm_ingestion.ports.protocols import ArchiveMeta

_logger = get_logger("adapters.s3")

_INDEX_PREFIX = "index"
"""Where an identifier-to-key pointer lives.

The archive is READ BY IDENTIFIER but STORED under a time-ordered key (Requirement 16.5), so
the identifier alone does not say which hour its object is under. A tiny pointer object
avoids the alternative — listing the bucket to find one payload — which would turn every
read into a scan of exactly the layout the padding was designed to make enumerable.
"""


class S3RawArchive:
    """Raw payloads in S3, under Requirement 16.5's time-ordered key layout."""

    def __init__(self, bucket: str, endpoint_url: str | None = None) -> None:
        """Bind the bucket."""
        self._bucket = bucket
        self._client = boto3.client("s3", endpoint_url=endpoint_url)

    def write(self, payload: bytes, meta: ArchiveMeta) -> str:
        """Archive bytes VERBATIM and return the derived identifier.

        The payload is never inspected, parsed or normalised: Requirement 16.1 archives before
        anything derives from it, so the bytes worth keeping most are exactly the ones that
        would
        fail a parse.
        """
        identifier = derive_archive_id(payload, meta)
        key = archive_key(meta.ingested_at, identifier)

        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=payload,
            # Metadata, not a rewritten body: the transport and source are Requirement 4.7's
            # provenance and belong beside the bytes rather than inside them.
            Metadata={"transport": meta.transport, "source": meta.source},
        )
        self._client.put_object(
            Bucket=self._bucket, Key=f"{_INDEX_PREFIX}/{identifier}", Body=key.encode()
        )
        return identifier

    def read(self, archive_id: str) -> bytes:
        """Return the archived bytes for an identifier.

        RAISES for an unknown identifier rather than returning None, because the PORT declares
        ``read(...) -> bytes`` and the in-memory adapter raises. My first draft returned None
        the shared contract suite caught the divergence immediately — which is the whole reason
        that suite exists, and the port is the contract, not my preference.

        Raising is also the right shape on its own terms: this archive is append-only, so a
        caller
        holding an identifier it cannot read has hit a broken invariant, not a normal branch.

        Raises:
            KeyError: naming the identifier, matching the in-memory adapter exactly.
        """
        key = self._resolve_key(archive_id)
        if key is None:
            raise KeyError(archive_id)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            if _is_missing(error):
                # The pointer exists but the object does not, which means something deleted from
                # an append-only archive. Logged loudly before raising, because that is an
                # operational fault rather than a caller mistake.
                _logger.error(
                    "archive_object_missing", archive_id=archive_id, key=key
                )
                raise KeyError(archive_id) from error
            raise
        body = response["Body"].read()
        return bytes(body)

    def _resolve_key(self, identifier: str) -> str | None:
        """The time-ordered key for an identifier, via the pointer object."""
        try:
            response = self._client.get_object(
                Bucket=self._bucket, Key=f"{_INDEX_PREFIX}/{identifier}"
            )
        except ClientError as error:
            if _is_missing(error):
                return None
            raise
        decoded: str = response["Body"].read().decode()
        return decoded


def _is_missing(error: ClientError) -> bool:
    """Whether a ClientError means "no such object" rather than a real fault."""
    code = error.response.get("Error", {}).get("Code")
    return code in ("NoSuchKey", "404", "NotFound")


__all__ = ["S3RawArchive"]
