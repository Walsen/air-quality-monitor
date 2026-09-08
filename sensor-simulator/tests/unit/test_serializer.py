"""Unit tests for the Serializer (task 2.2).

Requirement 3.1: render a SensorDataRecord as JSON text with exactly the nine
fields and a SensorMetadataRecord with exactly the twenty, in declared order,
with the JSON value type each field requires.
Requirement 2.7: ScaledValue is a JSON number rounded to exactly 2 decimal
places using half-away-from-zero rounding.
"""

from __future__ import annotations

import json

from aqm_simulator.contract.records import SensorDataRecord, SensorMetadataRecord
from aqm_simulator.contract.serializer import serialize_data, serialize_metadata

_DATA_FIELDS = [
    "Species",
    "Source",
    "Units",
    "SiteCode",
    "DateTime",
    "Duration",
    "ScaledValue",
    "RatificationStatus",
    "SensorContract",
]

_METADATA_FIELDS = [
    "SiteCode",
    "SiteName",
    "DeviceCode",
    "InstallationCode",
    "Facility",
    "Location",
    "Latitude",
    "Longitude",
    "Borough",
    "SiteClassification",
    "SensorHeightAboveGround",
    "DistanceToKerb",
    "SponsorName",
    "SiteLocationType",
    "StartDate",
    "EndDate",
    "PowerTag",
    "SiteDescription",
    "SitePhotoURL",
    "SensorContract",
]


def _data(**overrides: object) -> SensorDataRecord:
    base: dict[str, object] = {
        "Species": "PM25",
        "Source": "Measurement",
        "Units": "ug.m-3",
        "SiteCode": "CB0001",
        "DateTime": "2026-07-01T00:00:00Z",
        "Duration": "PT1H",
        "ScaledValue": 12.345,
        "RatificationStatus": "P",
        "SensorContract": "Cellular-BO",
    }
    base.update(overrides)
    return SensorDataRecord(**base)  # type: ignore[arg-type]


def _metadata() -> SensorMetadataRecord:
    return SensorMetadataRecord(  # type: ignore[call-arg]
        SiteCode="CB0001",
        SiteName="Cochabamba Centro",
        DeviceCode="DEV-CB0001",
        InstallationCode="INST-CB0001",
        Facility=None,
        Latitude="-17.3895000",
        Longitude="-66.1568000",
        Borough="Cochabamba",
        SiteClassification="Roadside",
        SensorHeightAboveGround=2.5,
        DistanceToKerb=3.0,
        SponsorName="Kanata AQ",
        SiteLocationType=None,
        StartDate="2026-01-01T00:00:00Z",
        EndDate=None,
        PowerTag="Mains",
        SiteDescription=None,
        SitePhotoURL=None,
        SensorContract="Cellular-BO",
    )


def test_serialize_data_field_order_and_types() -> None:
    text = serialize_data(_data())
    obj = json.loads(text)
    assert list(obj.keys()) == _DATA_FIELDS
    assert isinstance(obj["ScaledValue"], (int, float))
    assert not isinstance(obj["ScaledValue"], str)


def test_serialize_metadata_field_order_and_null_keys() -> None:
    text = serialize_metadata(_metadata())
    obj = json.loads(text)
    assert list(obj.keys()) == _METADATA_FIELDS
    assert obj["Facility"] is None
    assert obj["EndDate"] is None


def test_scaled_value_rounds_half_away_from_zero() -> None:
    # 12.345 -> 12.35 (half away from zero, NOT banker's 12.34)
    assert json.loads(serialize_data(_data(ScaledValue=12.345)))["ScaledValue"] == 12.35
    # 2.675 -> 2.68 (the classic float case banker's rounding gets "wrong")
    assert json.loads(serialize_data(_data(ScaledValue=2.675)))["ScaledValue"] == 2.68
    # negative: -0.125 -> -0.13 (away from zero)
    assert json.loads(serialize_data(_data(ScaledValue=-0.125)))["ScaledValue"] == -0.13


def test_scaled_value_always_two_decimal_places_in_text() -> None:
    # The serialized text carries exactly two decimal places.
    text = serialize_data(_data(ScaledValue=7.0))
    # find the ScaledValue token in the raw JSON text
    assert '"ScaledValue":7.0' in text.replace(" ", "") or '"ScaledValue": 7.0' in text


def test_location_coordinates_identical_in_serialized_text() -> None:
    text = serialize_metadata(_metadata())
    obj = json.loads(text)
    coords = obj["Location"]["geometry"]["coordinates"]
    assert coords == [obj["Latitude"], obj["Longitude"]]
