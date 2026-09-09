"""S3 adapter for the raw archive.

The key and identifier derivation live in ``ports/archive_key.py``, shared with the in-memory
adapter so a replay cannot land in a different place depending on which archive wrote it.
"""

from aqm_ingestion.adapters.s3.archive import S3RawArchive

__all__ = ["S3RawArchive"]
