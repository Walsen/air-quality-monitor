"""Unit tests for the canonical contract record models (task 2.1).

Requirement 1.1: SensorMetadataRecord has exactly the twenty named fields in
the declared order, every key present even when null.
Requirement 1.2/1.3: Latitude/Longitude are 7-decimal JSON strings, and
Location.coordinates holds two character-identical strings [Latitude, Longitude].
Requirement 2.1: SensorDataRecord has exactly the nine named fields in order.
Requirement 2.7: ScaledValue is a JSON number.

These assert model shape only; serialization (task 2.2) and parsing (2.3) come
next. Model-level field-order and field-set checks use the JSON the model
produces via model_dump(mode="json").
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aqm_simulator.contract.records import SensorDataRecord, SensorMetadataRecord

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


def _data_record(**overrides: object) -> SensorDataRecord:
    base: dict[str, object] = {
        "Species": "PM25",
        "Source": "Measurement",
        "Units": "ug.m-3",
        "SiteCode": "CB0001",
        "DateTime": "2026-07-01T00:00:00Z",
        "Duration": "PT1H",
        "ScaledValue": 12.34,
        "RatificationStatus": "P",
        "SensorContract": "Cellular-BO",
    }
    base.update(overrides)
    return SensorDataRecord(**base)  # type: ignore[arg-type]


def _metadata_record(**overrides: object) -> SensorMetadataRecord:
    base: dict[str, object] = {
        "SiteCode": "CB0001",
        "SiteName": "Cochabamba Centro",
        "DeviceCode": "DEV-CB0001",
        "InstallationCode": "INST-CB0001",
        "Facility": None,
        "Latitude": "-17.3895000",
        "Longitude": "-66.1568000",
        "Borough": "Cochabamba",
        "SiteClassification": "Roadside",
        "SensorHeightAboveGround": 2.50,
        "DistanceToKerb": 3.00,
        "SponsorName": "Kanata AQ",
        "SiteLocationType": None,
        "StartDate": "2026-01-01T00:00:00Z",
        "EndDate": None,
        "PowerTag": "Mains",
        "SiteDescription": None,
        "SitePhotoURL": None,
        "SensorContract": "Cellular-BO",
    }
    base.update(overrides)
    return SensorMetadataRecord(**base)  # type: ignore[arg-type]


def test_data_record_has_exactly_nine_fields_in_order() -> None:
    record = _data_record()
    dumped = record.model_dump(mode="json")
    assert list(dumped.keys()) == _DATA_FIELDS


def test_metadata_record_has_exactly_twenty_fields_in_order() -> None:
    record = _metadata_record()
    dumped = record.model_dump(mode="json")
    assert list(dumped.keys()) == _METADATA_FIELDS


def test_metadata_null_keys_present_even_when_none() -> None:
    # Requirement 1.1: every field key present even when its value is null.
    dumped = _metadata_record().model_dump(mode="json")
    for nullable in ("Facility", "SiteLocationType", "EndDate", "SiteDescription", "SitePhotoURL"):
        assert nullable in dumped
        assert dumped[nullable] is None


def test_scaled_value_is_number_not_string() -> None:
    # Requirement 2.7: ScaledValue is a JSON number.
    dumped = _data_record(ScaledValue=7.0).model_dump(mode="json")
    assert isinstance(dumped["ScaledValue"], (int, float))
    assert not isinstance(dumped["ScaledValue"], str)


def test_location_coordinates_are_character_identical_to_lat_lon() -> None:
    # Requirement 1.3: Location is a GeoJSON Point Feature whose coordinates are
    # two JSON strings character-identical to the sibling Latitude then Longitude.
    record = _metadata_record(Latitude="-17.3895000", Longitude="-66.1568000")
    dumped = record.model_dump(mode="json")
    loc = dumped["Location"]
    assert loc["type"] == "Feature"
    assert loc["geometry"]["type"] == "Point"
    coords = loc["geometry"]["coordinates"]
    assert coords == [dumped["Latitude"], dumped["Longitude"]]
    assert coords == ["-17.3895000", "-66.1568000"]


def test_latitude_longitude_are_seven_decimal_strings() -> None:
    # Requirement 1.2: Latitude/Longitude are JSON strings with exactly 7 dp.
    dumped = _metadata_record(Latitude="-17.3895000", Longitude="-66.1568000").model_dump(
        mode="json"
    )
    for key in ("Latitude", "Longitude"):
        value = dumped[key]
        assert isinstance(value, str)
        assert len(value.split(".")[1]) == 7


def test_data_record_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        _data_record(Bogus="x")


def test_powertag_must_be_mains_or_solar() -> None:
    # Requirement 1.5: PowerTag is exactly Mains or Solar (case-sensitive).
    with pytest.raises(ValidationError):
        _metadata_record(PowerTag="battery")
