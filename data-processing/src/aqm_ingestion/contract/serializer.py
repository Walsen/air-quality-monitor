"""Rendering records back to JSON text.

Requirement 3.4 makes the Serializer the other half of the round-trip: whatever
the Parser accepted must render and re-parse to an equal record, including
null-valued fields, so a null is emitted as ``null`` rather than dropped.

The render alters nothing. In particular ``ScaledValue`` is emitted as the number
the record holds, never rounded (Requirement 1.5), because the archived payload
has to stay byte-comparable with what arrived (Requirement 1.10).

Field order comes from the models' declaration order, which
``DATA_FIELD_ORDER`` and ``METADATA_FIELD_ORDER`` state explicitly; the assertion
below keeps the two from drifting apart silently.
"""

from __future__ import annotations

import json

from aqm_ingestion.contract.records import (
    DATA_FIELD_ORDER,
    METADATA_FIELD_ORDER,
    SensorDataRecord,
    SensorMetadataRecord,
)

# Compact separators keep the output single-line, which the archive and the log
# transport both expect.
_SEPARATORS = (",", ":")


def _render(payload: dict[str, object], expected_order: tuple[str, ...]) -> str:
    # A mismatch here means the model's declared order and the published constant
    # have diverged — a contract bug worth failing loudly rather than emitting a
    # subtly reordered payload.
    if tuple(payload.keys()) != expected_order:
        raise AssertionError(
            "serialized field order does not match the declared contract order: "
            f"got {tuple(payload.keys())}, expected {expected_order}"
        )
    return json.dumps(payload, separators=_SEPARATORS)


def serialize_data(record: SensorDataRecord) -> str:
    """Render a measurement record as one line of JSON (Requirement 3.4)."""
    return _render(record.model_dump(mode="json"), DATA_FIELD_ORDER)


def serialize_metadata(record: SensorMetadataRecord) -> str:
    """Render a metadata record as one line of JSON (Requirement 3.4)."""
    return _render(record.model_dump(mode="json"), METADATA_FIELD_ORDER)
