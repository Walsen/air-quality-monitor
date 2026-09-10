"""Unit tests for the cross-field quarantine rules (task 2.8).

These three rules QUARANTINE a record with a reason rather than raising, so they
live in the domain rather than in the contract models:

- Req 2.3: Location.coordinates disagreeing with the sibling Latitude/Longitude is
  quarantined naming the disagreeing values.
- Req 2.6: a non-null EndDate earlier than StartDate is quarantined naming both.
- Req 1.8: a Units value other than ug.m-3 on a Species of NO2 or PM25 is
  quarantined naming the field, the received value, and the expected value —
  rather than assuming the expected unit.

Every problem in a record is accumulated (§5), so one pass reports them all.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from aqm_ingestion.contract.records import SensorDataRecord, SensorMetadataRecord
from aqm_ingestion.domain.validation import (
    QuarantineReason,
    validate_data_record,
    validate_metadata_record,
)
from tests.unit.test_records import GOLDEN_DATA_PAYLOAD, GOLDEN_METADATA_PAYLOAD


def _data(**overrides: object) -> SensorDataRecord:
    fields = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD)) | overrides
    return SensorDataRecord(**fields)


def _metadata(**overrides: object) -> SensorMetadataRecord:
    fields = cast("dict[str, Any]", json.loads(GOLDEN_METADATA_PAYLOAD)) | overrides
    return SensorMetadataRecord(**fields)


# --- Req 1.8 unit mismatch on a concentration species ---------------------

@pytest.mark.parametrize("species", ["NO2", "PM25"])
def test_expected_unit_on_a_concentration_species_passes(species: str) -> None:
    assert validate_data_record(_data(Species=species, Units="ug.m-3")) == ()


@pytest.mark.parametrize("species", ["NO2", "PM25"])
@pytest.mark.parametrize("units", ["ppb", "mg.m-3", "ug/m3", "", "UG.M-3"])
def test_wrong_unit_on_a_concentration_species_is_quarantined(
    species: str, units: str
) -> None:
    problems = validate_data_record(_data(Species=species, Units=units))
    assert len(problems) == 1
    problem = problems[0]
    assert problem.reason is QuarantineReason.UNEXPECTED_UNIT
    assert problem.field == "Units"
    assert problem.received == units       # names the RECEIVED value
    assert problem.expected == "ug.m-3"    # and the EXPECTED one


@pytest.mark.parametrize("species", ["NO2Index", "PM25Index"])
def test_index_species_are_not_unit_checked(species: str) -> None:
    # the index species are the emitting network's own values, not a mass
    # concentration, so ug.m-3 is not expected of them (Req 1.6/1.7)
    assert validate_data_record(_data(Species=species, Units="index")) == ()


def test_unit_check_is_case_sensitive() -> None:
    problems = validate_data_record(_data(Species="PM25", Units="UG.M-3"))
    assert problems and problems[0].reason is QuarantineReason.UNEXPECTED_UNIT


# --- Req 2.3 coordinate disagreement -------------------------------------

def test_matching_coordinates_pass() -> None:
    assert validate_metadata_record(_metadata()) == ()


def test_coordinate_disagreement_is_quarantined_naming_both() -> None:
    record = _metadata(
        Location={
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": ["-17.3912346", "-66.1523456"],  # latitude differs
            },
        }
    )
    problems = validate_metadata_record(record)
    assert len(problems) == 1
    problem = problems[0]
    assert problem.reason is QuarantineReason.COORDINATE_DISAGREEMENT
    # both disagreeing values appear in the reason
    assert "-17.3912346" in problem.detail
    assert "-17.3912345" in problem.detail


def test_longitude_disagreement_is_quarantined() -> None:
    record = _metadata(
        Location={
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": ["-17.3912345", "-66.1523457"],
            },
        }
    )
    problems = validate_metadata_record(record)
    assert problems[0].reason is QuarantineReason.COORDINATE_DISAGREEMENT


def test_numerically_equal_but_textually_different_still_disagrees() -> None:
    # Req 2.3 demands CHARACTER-identical values, so a differently written but
    # numerically equal coordinate is still a disagreement
    record = _metadata(
        Latitude="17.3912345",
        Location={
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": ["17.3912345000", "-66.1523456"],
            },
        },
    )
    problems = validate_metadata_record(record)
    assert problems and problems[0].reason is QuarantineReason.COORDINATE_DISAGREEMENT


# --- Req 2.6 EndDate before StartDate ------------------------------------

def test_null_enddate_passes() -> None:
    assert validate_metadata_record(_metadata(EndDate=None)) == ()


def test_enddate_equal_to_startdate_passes() -> None:
    record = _metadata(StartDate="2024-01-01T00:00:00Z", EndDate="2024-01-01T00:00:00Z")
    assert validate_metadata_record(record) == ()


def test_enddate_after_startdate_passes() -> None:
    record = _metadata(StartDate="2024-01-01T00:00:00Z", EndDate="2025-01-01T00:00:00Z")
    assert validate_metadata_record(record) == ()


def test_enddate_before_startdate_is_quarantined_naming_both() -> None:
    record = _metadata(StartDate="2025-01-01T00:00:00Z", EndDate="2024-01-01T00:00:00Z")
    problems = validate_metadata_record(record)
    assert len(problems) == 1
    problem = problems[0]
    assert problem.reason is QuarantineReason.END_BEFORE_START
    assert "2025-01-01T00:00:00Z" in problem.detail
    assert "2024-01-01T00:00:00Z" in problem.detail


# --- §5 every problem accumulates ----------------------------------------

def test_both_metadata_problems_are_reported_together() -> None:
    record = _metadata(
        StartDate="2025-01-01T00:00:00Z",
        EndDate="2024-01-01T00:00:00Z",
        Location={
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": ["-17.9999999", "-66.1523456"],
            },
        },
    )
    problems = validate_metadata_record(record)
    reasons = {problem.reason for problem in problems}
    assert reasons == {
        QuarantineReason.COORDINATE_DISAGREEMENT,
        QuarantineReason.END_BEFORE_START,
    }


def test_problem_detail_never_empty() -> None:
    problems = validate_data_record(_data(Species="PM25", Units="ppb"))
    assert problems[0].detail.strip()


def test_problems_are_returned_not_raised() -> None:
    # the pipeline needs to quarantine and carry on, so validation must not raise
    assert validate_data_record(_data(Species="PM25", Units="ppb")) != ()
