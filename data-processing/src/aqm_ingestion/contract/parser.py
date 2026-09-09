"""Parsing JSON text into records, with a reason for everything rejected.

Two failure scopes, deliberately handled differently (Requirements 3.2 vs 3.5):

- A WHOLE-PAYLOAD failure — malformed JSON, an oversize payload, a batch longer
  than the maximum — raises :class:`ParseError`, because there is no meaningful
  partial result to return.
- A PER-RECORD failure inside an array is RETURNED, not raised: the accepted
  siblings must survive, so the result carries the accepted records together with
  one rejection per failed element identified by its array index.

A record is never partially populated (Requirement 3.7): validation happens before
any record value exists, so a rejected element yields a rejection and nothing else.

Every rejection names the offending field and which of the four conditions of
Requirement 3.3 it violated, so an operator can diagnose upstream data from the
log alone rather than re-reading the payload.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from aqm_ingestion.contract.records import SensorDataRecord, SensorMetadataRecord

DEFAULT_MAX_BATCH = 100_000
DEFAULT_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024  # 8 MiB


class RejectionKind(StrEnum):
    """Why a payload or a record was refused.

    The first three are whole-payload scoped; the last four are the per-record
    conditions Requirement 3.3 enumerates.
    """

    MALFORMED_JSON = "malformed_json"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    BATCH_TOO_LARGE = "batch_too_large"
    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    WRONG_TYPE = "wrong_type"
    INVALID_ENUM = "invalid_enum"


@dataclass(frozen=True, slots=True)
class Rejection:
    """One refusal, diagnosable without the original payload."""

    kind: RejectionKind
    detail: str
    field: str | None = None
    # The array index of the failing element, or None for a single object
    # (Requirement 3.5).
    index: int | None = None


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Accepted records, per-record rejections, and diverted meteorology elements.

    ``meteorology`` holds elements whose ``Species`` the configured mapping routes to a
    meteorology channel (Requirement 3.6). They are returned UNINTERPRETED: the parser
    decides whose an element is, while what it means belongs to the calibration stage
    that owns the mapping (§1).
    """

    records: tuple[Any, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    meteorology: tuple[Mapping[str, Any], ...] = ()


class ParseError(Exception):
    """A whole-payload failure; no record value is produced (Requirement 3.2)."""

    def __init__(self, rejection: Rejection) -> None:
        """Carry the rejection describing why the whole payload was refused."""
        super().__init__(rejection.detail)
        self.rejection = rejection


# Pydantic error types mapped to the four conditions of Requirement 3.3. Anything
# unmapped is reported as a type mismatch, which is the honest default: the value
# was not acceptable for that field.
_MISSING_ERRORS = frozenset({"missing"})
_UNKNOWN_ERRORS = frozenset({"extra_forbidden"})
_ENUM_ERRORS = frozenset({"literal_error", "enum"})


def _classify(error: Mapping[str, Any]) -> tuple[RejectionKind, str | None]:
    """Map one pydantic error onto a rejection kind and the offending field."""
    error_type = str(error.get("type", ""))
    location = error.get("loc") or ()
    field = str(location[0]) if location else None
    if error_type in _MISSING_ERRORS:
        return RejectionKind.MISSING_FIELD, field
    if error_type in _UNKNOWN_ERRORS:
        return RejectionKind.UNKNOWN_FIELD, field
    if error_type in _ENUM_ERRORS:
        return RejectionKind.INVALID_ENUM, field
    # A pattern or range failure on a constrained STRING field is a value fault,
    # not a JSON type fault, but both mean "this value is not acceptable here";
    # WRONG_TYPE is the condition Requirement 3.3 provides for it.
    return RejectionKind.WRONG_TYPE, field


def _reject_from_validation(
    error: ValidationError, index: int | None
) -> Rejection:
    """Build ONE rejection for a failed record (Requirement 3.5: one per record)."""
    first = error.errors()[0]
    kind, field = _classify(first)
    message = str(first.get("msg", "value is not acceptable"))
    where = f" at index {index}" if index is not None else ""
    detail = (
        f"record{where} rejected ({kind}): field {field!r} {message}"
        if field
        else f"record{where} rejected ({kind}): {message}"
    )
    return Rejection(kind=kind, detail=detail, field=field, index=index)


def _load(
    text: str, max_payload_bytes: int
) -> dict[str, object] | list[object]:
    """Decode the payload, raising for a whole-payload failure (Requirement 3.2)."""
    size = len(text.encode("utf-8"))
    if size > max_payload_bytes:
        raise ParseError(
            Rejection(
                kind=RejectionKind.PAYLOAD_TOO_LARGE,
                detail=(
                    f"payload of {size} bytes exceeds the maximum of "
                    f"{max_payload_bytes} bytes"
                ),
            )
        )
    try:
        loaded = json.loads(text)
    except (json.JSONDecodeError, TypeError) as error:
        raise ParseError(
            Rejection(
                kind=RejectionKind.MALFORMED_JSON,
                detail=f"payload is not well-formed JSON: {error}",
            )
        ) from error
    if not isinstance(loaded, (dict, list)):
        raise ParseError(
            Rejection(
                kind=RejectionKind.MALFORMED_JSON,
                detail=(
                    "payload must be a JSON object or an array of objects; got "
                    f"{type(loaded).__name__}"
                ),
            )
        )
    return loaded


def _parse[RecordT](
    text: str,
    model: type[RecordT],
    max_batch: int,
    max_payload_bytes: int,
    meteorology_species: frozenset[str] = frozenset(),
) -> ParseResult:
    loaded = _load(text, max_payload_bytes)

    if isinstance(loaded, dict):
        elements: list[Any] = [loaded]
        indexed = False
    else:
        if len(loaded) > max_batch:
            raise ParseError(
                Rejection(
                    kind=RejectionKind.BATCH_TOO_LARGE,
                    detail=(
                        f"batch of {len(loaded)} records exceeds the maximum of "
                        f"{max_batch}"
                    ),
                )
            )
        elements = list(loaded)
        indexed = True

    records: list[RecordT] = []
    rejections: list[Rejection] = []
    meteorology: list[Mapping[str, Any]] = []
    for position, element in enumerate(elements):
        index = position if indexed else None
        if not isinstance(element, dict):
            rejections.append(
                Rejection(
                    kind=RejectionKind.WRONG_TYPE,
                    detail=(
                        f"array element at index {position} must be a JSON object; "
                        f"got {type(element).__name__}"
                    ),
                    index=index,
                )
            )
            continue
        # Requirement 3.6: a Species the configured meteorology mapping recognises is
        # ROUTED, not rejected. Checked BEFORE construction because the contract's
        # Species is a closed literal, so the model could never hold one — and
        # Requirement 3.7 forbids emitting a partially populated record.
        if meteorology_species and element.get("Species") in meteorology_species:
            meteorology.append(element)
            continue
        try:
            records.append(model(**element))
        except ValidationError as error:
            # Continue past the failure so accepted siblings survive (Req 3.5).
            rejections.append(_reject_from_validation(error, index))
    return ParseResult(
        records=tuple(records),
        rejections=tuple(rejections),
        meteorology=tuple(meteorology),
    )


def parse_data_records(
    text: str,
    max_batch: int = DEFAULT_MAX_BATCH,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    meteorology_species: frozenset[str] = frozenset(),
) -> ParseResult:
    """Parse measurement records from JSON text.

    Args:
        text: the payload.
        max_batch: maximum array length (Requirement 3.1).
        max_payload_bytes: maximum payload size (Requirement 3.2).
        meteorology_species: `Species` values the configured meteorology channel
            mapping recognises. An element carrying one is DIVERTED to
            ``ParseResult.meteorology`` rather than rejected (Requirement 3.6), and is
            never produced as a Reading.
    """
    return _parse(
        text, SensorDataRecord, max_batch, max_payload_bytes, meteorology_species
    )


def parse_metadata_records(
    text: str,
    max_batch: int = DEFAULT_MAX_BATCH,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> ParseResult:
    """Parse metadata records from JSON text.

    Metadata records carry no `Species`, so the Requirement 3.6 diversion does not
    apply to them.
    """
    return _parse(text, SensorMetadataRecord, max_batch, max_payload_bytes)
