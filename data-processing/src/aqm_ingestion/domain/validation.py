"""Cross-field validation that QUARANTINES rather than raises.

Three rules cannot live in the contract models, and the distinction is
deliberate. A model rejects a payload that is structurally not a record. These
rules concern a record that parsed perfectly well but disagrees with itself, and
the pipeline must respond by quarantining it WITH A REASON and carrying on with
its siblings (engineering-practices §5). Raising would conflate the two and lose
the sibling records.

So the functions here RETURN problems and never raise:

- Requirement 1.8: ``Units`` other than ``ug.m-3`` on a mass-concentration
  species is quarantined naming the field, the received value and the expected
  value — explicitly rather than assuming the expected unit.
- Requirement 2.3: ``Location.coordinates`` must be CHARACTER-identical to the
  sibling ``Latitude`` then ``Longitude``. A numerically equal but differently
  written coordinate is still a disagreement, so the comparison is textual.
- Requirement 2.6: a non-null ``EndDate`` earlier than ``StartDate`` is
  quarantined naming both values.

Every problem in a record is accumulated so one pass reports them all, rather
than an operator fixing one fault per round trip.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aqm_ingestion.contract.records import (
    EXPECTED_CONCENTRATION_UNIT,
    SensorDataRecord,
    SensorMetadataRecord,
)


class QuarantineReason(StrEnum):
    """Why a well-formed record is nonetheless not storable."""

    UNEXPECTED_UNIT = "unexpected_unit"
    COORDINATE_DISAGREEMENT = "coordinate_disagreement"
    END_BEFORE_START = "end_before_start"


@dataclass(frozen=True, slots=True)
class QuarantineProblem:
    """One reason a record was quarantined, diagnosable on its own."""

    reason: QuarantineReason
    detail: str
    field: str | None = None
    received: str | None = None
    expected: str | None = None


def validate_data_record(record: SensorDataRecord) -> tuple[QuarantineProblem, ...]:
    """Check a measurement record's cross-field rules (Requirement 1.8)."""
    problems: list[QuarantineProblem] = []

    # Only the mass-concentration species carry an expected unit; the index
    # species are the emitting network's own values (Req 1.6/1.7), so demanding
    # ug.m-3 of them would quarantine perfectly valid records.
    if record.is_mass_concentration and record.Units != EXPECTED_CONCENTRATION_UNIT:
        problems.append(
            QuarantineProblem(
                reason=QuarantineReason.UNEXPECTED_UNIT,
                field="Units",
                received=record.Units,
                expected=EXPECTED_CONCENTRATION_UNIT,
                detail=(
                    f"Units for Species {record.Species} was {record.Units!r}; "
                    f"expected {EXPECTED_CONCENTRATION_UNIT!r}"
                ),
            )
        )
    return tuple(problems)


def validate_metadata_record(
    record: SensorMetadataRecord,
) -> tuple[QuarantineProblem, ...]:
    """Check a metadata record's cross-field rules (Requirements 2.3, 2.6)."""
    problems: list[QuarantineProblem] = []

    latitude, longitude = record.Location.geometry.coordinates
    # Compared as TEXT, because Requirement 2.3 demands character-identical
    # values: "17.3912345" and "17.3912345000" are numerically equal yet still a
    # disagreement, and silently accepting one would let two spellings of a
    # position into the registry.
    if (latitude, longitude) != (record.Latitude, record.Longitude):
        problems.append(
            QuarantineProblem(
                reason=QuarantineReason.COORDINATE_DISAGREEMENT,
                field="Location",
                detail=(
                    "Location.coordinates "
                    f"({latitude!r}, {longitude!r}) disagree with the sibling "
                    f"Latitude {record.Latitude!r} and Longitude "
                    f"{record.Longitude!r}"
                ),
            )
        )

    # Both are ISO-8601 UTC strings at whole-second precision and fixed width, so
    # a lexical comparison is also chronological — no parsing, and therefore no
    # clock dependency here (§2).
    if record.EndDate is not None and record.EndDate < record.StartDate:
        problems.append(
            QuarantineProblem(
                reason=QuarantineReason.END_BEFORE_START,
                field="EndDate",
                received=record.EndDate,
                expected=f"no earlier than StartDate {record.StartDate}",
                detail=(
                    f"EndDate {record.EndDate} is earlier than StartDate "
                    f"{record.StartDate}"
                ),
            )
        )
    return tuple(problems)
