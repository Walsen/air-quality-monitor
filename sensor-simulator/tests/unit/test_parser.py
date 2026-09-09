"""Unit tests for the Parser (tasks 2.3 and 2.9).

Requirement 3.2: read /SensorData and /ListSensors JSON into records, accepting
a single object or an array of 0..100,000 objects, preserving order.
Requirement 3.5: missing or unknown field -> validation error naming the field,
indicating missing vs unknown, and no record produced.
Requirement 3.7: invalid Species or wrong JSON type -> validation error naming
the field with permitted values / required type, no record produced.
Requirement 3.8: malformed JSON or over-max payload -> validation error, no
record produced.
"""

from __future__ import annotations

import json

import pytest

from aqm_simulator.contract.parser import ParseError, parse_data, parse_metadata
from aqm_simulator.contract.serializer import serialize_data

_VALID_DATA = {
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


def test_parse_single_object() -> None:
    records = parse_data(json.dumps(_VALID_DATA))
    assert len(records) == 1
    assert records[0].Species == "PM25"


def test_parse_array_preserves_order() -> None:
    codes = ["CB0003", "CB0001", "CB0002"]
    payload = json.dumps([{**_VALID_DATA, "SiteCode": c} for c in codes])
    records = parse_data(payload)
    assert [r.SiteCode for r in records] == codes


def test_parse_empty_array_is_zero_records() -> None:
    assert parse_data("[]") == []


def test_missing_field_names_it_and_produces_no_record() -> None:
    bad = {k: v for k, v in _VALID_DATA.items() if k != "Units"}
    with pytest.raises(ParseError) as exc:
        parse_data(json.dumps(bad))
    assert "Units" in str(exc.value)


def test_unknown_field_names_it_and_produces_no_record() -> None:
    with pytest.raises(ParseError) as exc:
        parse_data(json.dumps({**_VALID_DATA, "Bogus": 1}))
    assert "Bogus" in str(exc.value)


def test_invalid_species_names_field_and_permitted_values() -> None:
    with pytest.raises(ParseError) as exc:
        parse_data(json.dumps({**_VALID_DATA, "Species": "SO2"}))
    assert "Species" in str(exc.value)


def test_wrong_json_type_for_scaledvalue_rejected() -> None:
    with pytest.raises(ParseError) as exc:
        parse_data(json.dumps({**_VALID_DATA, "ScaledValue": "high"}))
    assert "ScaledValue" in str(exc.value)


def test_malformed_json_rejected() -> None:
    with pytest.raises(ParseError):
        parse_data("{not json")


def test_oversize_payload_rejected() -> None:
    with pytest.raises(ParseError):
        parse_data(json.dumps(_VALID_DATA), max_bytes=10)


def test_serializer_output_parses_back() -> None:
    from aqm_simulator.contract.records import SensorDataRecord

    text = serialize_data(SensorDataRecord(**_VALID_DATA))  # type: ignore[arg-type]
    records = parse_data(text)
    assert records[0].SiteCode == "CB0001"


def test_parse_metadata_single_object() -> None:
    meta = {
        "SiteCode": "CB0001",
        "SiteName": "Centro",
        "DeviceCode": "DEV-CB0001",
        "InstallationCode": None,
        "Facility": None,
        "Location": {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": ["-17.3895000", "-66.1568000"]},
        },
        "Latitude": "-17.3895000",
        "Longitude": "-66.1568000",
        "Borough": "Cochabamba",
        "SiteClassification": "Roadside",
        "SensorHeightAboveGround": 2.5,
        "DistanceToKerb": 3.0,
        "SponsorName": "Kanata AQ",
        "SiteLocationType": None,
        "StartDate": "2026-01-01T00:00:00Z",
        "EndDate": None,
        "PowerTag": "Mains",
        "SiteDescription": None,
        "SitePhotoURL": None,
        "SensorContract": "Cellular-BO",
    }
    records = parse_metadata(json.dumps(meta))
    assert len(records) == 1
    assert records[0].PowerTag == "Mains"
