"""Unit tests for reading validation (task 5.1).

Requirement 6.1 is the governing rule: EVERY rule is evaluated and one reason
recorded per violation, rather than stopping at the first. So the interesting tests
are the ones that break several rules at once.

- 6.2 non-finite, or negative for NO2/PM25.
- 6.3 above the per-species plausibility ceiling (PM25 1,000; NO2 5,000 default).
- 6.4 future-dated beyond the clock-skew tolerance (default 5 minutes).
- 6.5 older than the Retention_Window.
- 6.6 DateTime not aligned to the Duration boundary.
- 6.1 unit agreement, already covered by the Req 1.8 rule in this module.

The current instant arrives as a PARAMETER — validation never reads a clock (§2).
"""

from __future__ import annotations

import datetime as dt
import json
import math
from typing import Any, cast

import pytest

from aqm_ingestion.contract.records import SensorDataRecord
from aqm_ingestion.domain.validation import (
    QuarantineReason,
    ValidationLimits,
    validate_reading,
)
from tests.unit.test_records import GOLDEN_DATA_PAYLOAD

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_LIMITS = ValidationLimits()


def _record(**overrides: object) -> SensorDataRecord:
    fields = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD)) | overrides
    return SensorDataRecord(**fields)


def _reasons(record: SensorDataRecord, now: dt.datetime = _NOW) -> set[QuarantineReason]:
    return {problem.reason for problem in validate_reading(record, now, _LIMITS)}


# --- a clean record passes ------------------------------------------------

def test_a_conforming_record_has_no_problems() -> None:
    assert validate_reading(_record(DateTime="2026-07-01T11:00:00Z"), _NOW, _LIMITS) == ()


# --- Req 6.2 finiteness and sign -----------------------------------------

def test_negative_pm25_is_quarantined() -> None:
    record = _record(Species="PM25", ScaledValue=-0.1, DateTime="2026-07-01T11:00:00Z")
    assert QuarantineReason.VALUE_OUT_OF_RANGE in _reasons(record)


def test_negative_no2_is_quarantined() -> None:
    record = _record(
        Species="NO2", ScaledValue=-5.0, DateTime="2026-07-01T11:00:00Z"
    )
    assert QuarantineReason.VALUE_OUT_OF_RANGE in _reasons(record)


def test_zero_is_accepted() -> None:
    # zero is a legitimate concentration, not a fault
    record = _record(Species="PM25", ScaledValue=0, DateTime="2026-07-01T11:00:00Z")
    assert QuarantineReason.VALUE_OUT_OF_RANGE not in _reasons(record)


def test_negative_index_species_is_not_a_sign_violation() -> None:
    # Req 6.2 names the sign rule for NO2 and PM25 only; an index is the emitting
    # network's own value and is stored as received (Req 1.7)
    record = _record(
        Species="PM25Index", Units="index", ScaledValue=-1.0,
        DateTime="2026-07-01T11:00:00Z",
    )
    assert QuarantineReason.VALUE_OUT_OF_RANGE not in _reasons(record)


def test_problem_names_the_field_and_value() -> None:
    record = _record(Species="PM25", ScaledValue=-3.5, DateTime="2026-07-01T11:00:00Z")
    problem = next(
        p for p in validate_reading(record, _NOW, _LIMITS)
        if p.reason is QuarantineReason.VALUE_OUT_OF_RANGE
    )
    assert problem.field == "ScaledValue"
    assert "-3.5" in problem.detail


# --- Req 6.3 plausibility ceilings ---------------------------------------

def test_pm25_above_its_ceiling_is_quarantined() -> None:
    record = _record(
        Species="PM25", ScaledValue=1000.1, DateTime="2026-07-01T11:00:00Z"
    )
    assert QuarantineReason.IMPLAUSIBLE_VALUE in _reasons(record)


def test_pm25_at_its_ceiling_is_accepted() -> None:
    record = _record(
        Species="PM25", ScaledValue=1000.0, DateTime="2026-07-01T11:00:00Z"
    )
    assert QuarantineReason.IMPLAUSIBLE_VALUE not in _reasons(record)


def test_no2_uses_its_own_higher_ceiling() -> None:
    # 1,500 exceeds PM25's ceiling but is well under NO2's, so the ceiling must be
    # per species rather than shared
    record = _record(Species="NO2", ScaledValue=1500.0, DateTime="2026-07-01T11:00:00Z")
    assert QuarantineReason.IMPLAUSIBLE_VALUE not in _reasons(record)


def test_no2_above_its_ceiling_is_quarantined() -> None:
    record = _record(Species="NO2", ScaledValue=5000.1, DateTime="2026-07-01T11:00:00Z")
    assert QuarantineReason.IMPLAUSIBLE_VALUE in _reasons(record)


def test_ceiling_problem_names_species_value_and_ceiling() -> None:
    record = _record(
        Species="PM25", ScaledValue=2000.0, DateTime="2026-07-01T11:00:00Z"
    )
    problem = next(
        p for p in validate_reading(record, _NOW, _LIMITS)
        if p.reason is QuarantineReason.IMPLAUSIBLE_VALUE
    )
    assert "PM25" in problem.detail
    assert "2000" in problem.detail
    assert "1000" in problem.detail


def test_ceilings_are_configurable() -> None:
    limits = ValidationLimits(pm25_ceiling=10.0)
    record = _record(Species="PM25", ScaledValue=20.0, DateTime="2026-07-01T11:00:00Z")
    reasons = {p.reason for p in validate_reading(record, _NOW, limits)}
    assert QuarantineReason.IMPLAUSIBLE_VALUE in reasons


# --- Req 6.4 future-dating -----------------------------------------------

def test_future_dated_beyond_skew_is_quarantined() -> None:
    record = _record(DateTime="2026-07-01T12:06:00Z")  # 6 min ahead of now
    assert QuarantineReason.FUTURE_DATED in _reasons(record)


def test_within_the_skew_tolerance_is_accepted() -> None:
    record = _record(DateTime="2026-07-01T12:04:00Z")  # 4 min ahead
    assert QuarantineReason.FUTURE_DATED not in _reasons(record)


def test_exactly_at_the_skew_boundary_is_accepted() -> None:
    record = _record(DateTime="2026-07-01T12:05:00Z")  # exactly 5 min
    assert QuarantineReason.FUTURE_DATED not in _reasons(record)


def test_future_problem_names_both_instants() -> None:
    record = _record(DateTime="2026-07-01T13:00:00Z")
    problem = next(
        p for p in validate_reading(record, _NOW, _LIMITS)
        if p.reason is QuarantineReason.FUTURE_DATED
    )
    assert "2026-07-01T13:00:00Z" in problem.detail
    assert "2026-07-01T12:00:00Z" in problem.detail


# --- Req 6.5 staleness ---------------------------------------------------

def test_older_than_retention_is_quarantined() -> None:
    record = _record(DateTime="2026-05-01T11:00:00Z")  # ~61 days old
    assert QuarantineReason.STALE in _reasons(record)


def test_within_retention_is_accepted() -> None:
    record = _record(DateTime="2026-06-25T11:00:00Z")  # ~6 days old
    assert QuarantineReason.STALE not in _reasons(record)


def test_retention_window_is_configurable() -> None:
    limits = ValidationLimits(retention_days=1)
    record = _record(DateTime="2026-06-28T11:00:00Z")
    reasons = {p.reason for p in validate_reading(record, _NOW, limits)}
    assert QuarantineReason.STALE in reasons


def test_stale_problem_names_the_instant_and_window() -> None:
    record = _record(DateTime="2026-01-01T11:00:00Z")
    problem = next(
        p for p in validate_reading(record, _NOW, _LIMITS)
        if p.reason is QuarantineReason.STALE
    )
    assert "2026-01-01T11:00:00Z" in problem.detail
    assert "30" in problem.detail


# --- Req 6.6 interval alignment ------------------------------------------

@pytest.mark.parametrize(
    "moment", ["2026-07-01T11:30:00Z", "2026-07-01T11:00:30Z", "2026-07-01T11:01:01Z"]
)
def test_hourly_record_not_on_the_hour_is_quarantined(moment: str) -> None:
    record = _record(Duration="PT1H", DateTime=moment)
    assert QuarantineReason.INTERVAL_MISALIGNED in _reasons(record)


def test_hourly_record_on_the_hour_is_accepted() -> None:
    record = _record(Duration="PT1H", DateTime="2026-07-01T11:00:00Z")
    assert QuarantineReason.INTERVAL_MISALIGNED not in _reasons(record)


def test_fifteen_minute_alignment_is_honoured() -> None:
    record = _record(Duration="PT15M", DateTime="2026-07-01T11:15:00Z")
    assert QuarantineReason.INTERVAL_MISALIGNED not in _reasons(record)


def test_fifteen_minute_misalignment_is_quarantined() -> None:
    record = _record(Duration="PT15M", DateTime="2026-07-01T11:07:00Z")
    assert QuarantineReason.INTERVAL_MISALIGNED in _reasons(record)


def test_unparseable_duration_is_reported_not_crashed() -> None:
    # §5: bad input must never raise out of validation
    record = _record(Duration="banana", DateTime="2026-07-01T11:00:00Z")
    reasons = _reasons(record)
    assert QuarantineReason.UNSUPPORTED_DURATION in reasons


# --- Req 6.1 every rule is evaluated -------------------------------------

def test_all_violated_rules_are_reported_together() -> None:
    # negative AND future-dated AND misaligned AND wrong unit, in one record
    record = _record(
        Species="PM25",
        Units="ppb",
        ScaledValue=-1.0,
        Duration="PT1H",
        DateTime="2026-07-01T13:30:00Z",
    )
    reasons = _reasons(record)
    assert {
        QuarantineReason.VALUE_OUT_OF_RANGE,
        QuarantineReason.FUTURE_DATED,
        QuarantineReason.INTERVAL_MISALIGNED,
        QuarantineReason.UNEXPECTED_UNIT,
    } <= reasons


def test_validation_never_raises_on_a_hostile_value() -> None:
    for value in (math.inf, -math.inf, math.nan):
        record = _record(
            Species="PM25", ScaledValue=value, DateTime="2026-07-01T11:00:00Z"
        )
        assert QuarantineReason.VALUE_OUT_OF_RANGE in _reasons(record)


def test_every_problem_has_a_detail() -> None:
    record = _record(
        Species="PM25", ScaledValue=-1.0, DateTime="2026-07-01T13:30:00Z"
    )
    for problem in validate_reading(record, _NOW, _LIMITS):
        assert problem.detail.strip()


def test_validation_takes_the_instant_as_a_parameter() -> None:
    # §2: the same record is future-dated relative to one instant and not another,
    # which is only expressible because validation never reads a clock
    record = _record(DateTime="2026-07-01T12:30:00Z")
    assert QuarantineReason.FUTURE_DATED in _reasons(record, _NOW)
    later = dt.datetime(2026, 7, 1, 13, tzinfo=dt.UTC)
    assert QuarantineReason.FUTURE_DATED not in _reasons(record, later)
