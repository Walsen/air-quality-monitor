"""The Parser.

Reads ``/SensorData`` and ``/ListSensors`` JSON text into the canonical record
models. It accepts either a single JSON object or an array of 0..100,000
objects and preserves array order (Requirement 3.2). Any missing, unknown,
wrong-typed, or invalid-``Species`` field raises a :class:`ParseError` naming
the offending field and produces no record (Requirements 3.5, 3.7); malformed
JSON and an over-maximum payload likewise raise and produce nothing
(Requirement 3.8).

Reading is the Parser's sole responsibility; rendering belongs to the
Serializer (engineering-practices §1). A raw stack trace never reaches a caller
— errors are caught at this boundary and re-raised as a ParseError (§5).
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ValidationError

from aqm_simulator.contract.records import SensorDataRecord, SensorMetadataRecord

_MAX_BYTES_DEFAULT = 10 * 1024 * 1024  # 10 MB (Requirement 3.8)
_MAX_ITEMS = 100_000  # Requirement 3.2


class ParseError(ValueError):
    """Raised when input cannot be read into records. Names the offending field."""


def _describe(error: ValidationError) -> str:
    parts: list[str] = []
    for err in error.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        etype = err["type"]
        if etype == "extra_forbidden":
            parts.append(f"unknown field {loc!r}")
        elif etype == "missing":
            parts.append(f"missing field {loc!r}")
        else:
            parts.append(f"field {loc!r}: {err['msg']}")
    return "; ".join(parts)


def _parse[M: BaseModel](text: str, model: type[M], max_bytes: int) -> list[M]:
    if len(text.encode("utf-8")) > max_bytes:
        raise ParseError(
            f"payload exceeds maximum size of {max_bytes} bytes and was not read as JSON"
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"input could not be read as JSON: {exc}") from exc

    items = data if isinstance(data, list) else [data]
    if len(items) > _MAX_ITEMS:
        raise ParseError(f"array holds {len(items)} objects, exceeding the {_MAX_ITEMS} maximum")

    records: list[M] = []
    for index, item in enumerate(items):
        try:
            records.append(model.model_validate(item))
        except ValidationError as exc:
            where = "" if not isinstance(data, list) else f" at index {index}"
            raise ParseError(f"invalid record{where}: {_describe(exc)}") from exc
    return records


def parse_data(text: str, max_bytes: int = _MAX_BYTES_DEFAULT) -> list[SensorDataRecord]:
    """Parse ``/SensorData`` JSON into SensorDataRecord values, order preserved."""
    return _parse(text, SensorDataRecord, max_bytes)


def parse_metadata(text: str, max_bytes: int = _MAX_BYTES_DEFAULT) -> list[SensorMetadataRecord]:
    """Parse ``/ListSensors`` JSON into SensorMetadataRecord values, order preserved."""
    return _parse(text, SensorMetadataRecord, max_bytes)
