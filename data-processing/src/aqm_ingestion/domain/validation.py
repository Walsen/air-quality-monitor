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

import datetime as dt
import math
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
    VALUE_OUT_OF_RANGE = "value_out_of_range"
    IMPLAUSIBLE_VALUE = "implausible_value"
    FUTURE_DATED = "future_dated"
    STALE = "stale"
    INTERVAL_MISALIGNED = "interval_misaligned"
    UNSUPPORTED_DURATION = "unsupported_duration"


@dataclass(frozen=True, slots=True)
class ValidationLimits:
    """The configured bounds validation checks against.

    A narrow parameter object rather than the whole configuration (§1): validation
    needs these six numbers and nothing else, and its signature should say so.
    Defaults are the documented ones from Requirement 6.
    """

    pm25_ceiling: float = 1_000.0
    no2_ceiling: float = 5_000.0
    clock_skew_tolerance: dt.timedelta = dt.timedelta(minutes=5)
    retention_days: int = 30

    def ceiling_for(self, species: str) -> float | None:
        """Return the plausibility ceiling for a species, or None if it has none."""
        return {"PM25": self.pm25_ceiling, "NO2": self.no2_ceiling}.get(species)


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


# Durations this service understands, mapped to their length. Kept as a table so a
# newly accepted duration is one entry rather than another branch (§1 open/closed).
_DURATION_SECONDS: dict[str, int] = {
    "PT15M": 15 * 60,
    "PT30M": 30 * 60,
    "PT1H": 60 * 60,
    "PT24H": 24 * 60 * 60,
}
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_instant(value: str) -> dt.datetime:
    """Parse a contract timestamp; the model has already fixed its shape."""
    return dt.datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=dt.UTC)


def validate_reading(
    record: SensorDataRecord,
    now: dt.datetime,
    limits: ValidationLimits,
) -> tuple[QuarantineProblem, ...]:
    """Evaluate every reading rule of Requirement 6, collecting all violations.

    Requirement 6.1 forbids short-circuiting: a record breaking four rules must
    come back with four reasons, because an operator fixing upstream data needs the
    whole picture rather than whichever fault happened to be checked first.

    ``now`` is a PARAMETER, not a clock read (§2). That is what makes future-dating
    and staleness testable, and it is why the same record can be future-dated
    relative to one instant and valid relative to a later one.

    Args:
        record: a record that has already parsed, so its shapes are sound.
        now: the current instant, supplied by the caller's Clock.
        limits: the configured ceilings, skew tolerance, and retention window.

    Returns:
        One problem per violated rule, in a stable order. Empty when the record is
        storable.
    """
    problems: list[QuarantineProblem] = []
    problems.extend(validate_data_record(record))  # the Req 1.8 unit rule
    problems.extend(_check_value(record, limits))
    problems.extend(_check_timing(record, now, limits))
    return tuple(problems)


def _check_value(
    record: SensorDataRecord, limits: ValidationLimits
) -> list[QuarantineProblem]:
    """Finiteness, sign, and the per-species ceiling (Requirements 6.2, 6.3)."""
    problems: list[QuarantineProblem] = []
    value = record.ScaledValue

    # Non-finite first: nan and inf survive JSON parsing in some encoders, and
    # every comparison against them is false, so they must be caught explicitly
    # rather than by a range check that would silently pass.
    if not math.isfinite(value):
        problems.append(
            QuarantineProblem(
                reason=QuarantineReason.VALUE_OUT_OF_RANGE,
                field="ScaledValue",
                received=str(value),
                detail=f"ScaledValue {value} is not a finite number",
            )
        )
        return problems  # a non-finite value cannot meaningfully be range-checked

    # The sign rule applies to the mass-concentration species only: an index is the
    # emitting network's own value and is stored as received (Requirement 1.7).
    if record.is_mass_concentration and value < 0:
        problems.append(
            QuarantineProblem(
                reason=QuarantineReason.VALUE_OUT_OF_RANGE,
                field="ScaledValue",
                received=str(value),
                detail=(
                    f"ScaledValue {value} is negative for Species {record.Species}"
                ),
            )
        )

    ceiling = limits.ceiling_for(record.Species)
    if ceiling is not None and value > ceiling:
        problems.append(
            QuarantineProblem(
                reason=QuarantineReason.IMPLAUSIBLE_VALUE,
                field="ScaledValue",
                received=str(value),
                expected=f"at most {ceiling}",
                detail=(
                    f"ScaledValue {value} for Species {record.Species} exceeds the "
                    f"plausibility ceiling {ceiling}"
                ),
            )
        )
    return problems


def _check_timing(
    record: SensorDataRecord, now: dt.datetime, limits: ValidationLimits
) -> list[QuarantineProblem]:
    """Future-dating, staleness, and interval alignment (Requirements 6.4-6.6)."""
    problems: list[QuarantineProblem] = []
    instant = _parse_instant(record.DateTime)

    # Future-dated beyond the skew tolerance. The boundary is INCLUSIVE: a record
    # exactly at the tolerance is within it, since the tolerance exists precisely
    # to accommodate that much disagreement.
    if instant > now + limits.clock_skew_tolerance:
        problems.append(
            QuarantineProblem(
                reason=QuarantineReason.FUTURE_DATED,
                field="DateTime",
                received=record.DateTime,
                detail=(
                    f"DateTime {record.DateTime} is later than the current instant "
                    f"{now.strftime(_UTC_FORMAT)} plus the clock-skew tolerance "
                    f"{limits.clock_skew_tolerance}"
                ),
            )
        )

    if now - instant > dt.timedelta(days=limits.retention_days):
        problems.append(
            QuarantineProblem(
                reason=QuarantineReason.STALE,
                field="DateTime",
                received=record.DateTime,
                detail=(
                    f"DateTime {record.DateTime} is older than the retention window "
                    f"of {limits.retention_days} days"
                ),
            )
        )

    problems.extend(_check_alignment(record, instant))
    return problems


def _check_alignment(
    record: SensorDataRecord, instant: dt.datetime
) -> list[QuarantineProblem]:
    """DateTime must sit on the boundary of its Duration (Requirement 6.6)."""
    length = _DURATION_SECONDS.get(record.Duration)
    if length is None:
        # Reported rather than raised: an unknown duration is upstream data this
        # service does not accept, which is a quarantine, not a crash (§5).
        return [
            QuarantineProblem(
                reason=QuarantineReason.UNSUPPORTED_DURATION,
                field="Duration",
                received=record.Duration,
                expected=", ".join(sorted(_DURATION_SECONDS)),
                detail=(
                    f"Duration {record.Duration!r} is not one of the accepted values"
                ),
            )
        ]

    seconds_into_day = (
        instant.hour * 3600 + instant.minute * 60 + instant.second
    )
    if seconds_into_day % length != 0:
        return [
            QuarantineProblem(
                reason=QuarantineReason.INTERVAL_MISALIGNED,
                field="DateTime",
                received=record.DateTime,
                expected=f"a boundary of {record.Duration}",
                detail=(
                    f"DateTime {record.DateTime} is not aligned to the "
                    f"{record.Duration} interval boundary"
                ),
            )
        ]
    return []
