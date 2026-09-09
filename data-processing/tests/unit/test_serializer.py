"""Unit tests for the Serializer (task 2.2).

Requirement 3.4: rendering a record and parsing the output again must produce a
record whose every field equals the original, INCLUDING null-valued fields. The
serializer therefore renders in declared field order with the contract's JSON
value types and alters nothing — in particular it must not round ScaledValue
(Requirement 1.5).
"""

from __future__ import annotations

import json
from typing import cast

from aqm_ingestion.contract.records import (
    DATA_FIELD_ORDER,
    METADATA_FIELD_ORDER,
    SensorDataRecord,
    SensorMetadataRecord,
)
from aqm_ingestion.contract.serializer import serialize_data, serialize_metadata
from tests.unit.test_records import GOLDEN_DATA_PAYLOAD, GOLDEN_METADATA_PAYLOAD


def _data() -> SensorDataRecord:
    fields = cast("dict[str, object]", json.loads(GOLDEN_DATA_PAYLOAD))
    return SensorDataRecord(**fields)  # type: ignore[arg-type]


def _metadata() -> SensorMetadataRecord:
    fields = cast("dict[str, object]", json.loads(GOLDEN_METADATA_PAYLOAD))
    return SensorMetadataRecord(**fields)  # type: ignore[arg-type]


def test_data_is_rendered_in_declared_field_order() -> None:
    rendered = serialize_data(_data())
    assert tuple(json.loads(rendered).keys()) == DATA_FIELD_ORDER


def test_metadata_is_rendered_in_declared_field_order() -> None:
    rendered = serialize_metadata(_metadata())
    assert tuple(json.loads(rendered).keys()) == METADATA_FIELD_ORDER


def test_data_render_is_single_line() -> None:
    assert "\n" not in serialize_data(_data())


def test_data_value_types_match_the_contract() -> None:
    parsed = json.loads(serialize_data(_data()))
    assert isinstance(parsed["ScaledValue"], (int, float))
    for text_field in (
        "Species", "Source", "Units", "SiteCode", "DateTime", "Duration",
        "RatificationStatus", "SensorContract",
    ):
        assert isinstance(parsed[text_field], str)


def test_metadata_value_types_match_the_contract() -> None:
    parsed = json.loads(serialize_metadata(_metadata()))
    assert isinstance(parsed["Latitude"], str)  # lat/lon are STRINGS
    assert isinstance(parsed["Longitude"], str)
    assert isinstance(parsed["SensorHeightAboveGround"], (int, float))
    assert isinstance(parsed["DistanceToKerb"], (int, float))
    assert isinstance(parsed["Location"], dict)


def test_metadata_render_keeps_null_fields_present() -> None:
    parsed = json.loads(serialize_metadata(_metadata()))
    for nullable in ("EndDate", "SiteDescription", "SitePhotoURL"):
        assert nullable in parsed  # Req 3.4 includes null-valued fields
        assert parsed[nullable] is None


def test_coordinates_render_as_a_two_element_array_of_strings() -> None:
    parsed = json.loads(serialize_metadata(_metadata()))
    coordinates = parsed["Location"]["geometry"]["coordinates"]
    assert coordinates == [parsed["Latitude"], parsed["Longitude"]]
    assert all(isinstance(item, str) for item in coordinates)


def test_scaled_value_is_not_rounded_by_rendering() -> None:
    precise = 14.276543210987
    record = _data().model_copy(update={"ScaledValue": precise})
    assert json.loads(serialize_data(record))["ScaledValue"] == precise


def test_render_matches_the_golden_payload_exactly() -> None:
    # the captured Service 1 payload is already in declared order, so rendering
    # the parsed record must reproduce it byte for byte
    assert serialize_data(_data()) == GOLDEN_DATA_PAYLOAD


def test_metadata_render_matches_the_golden_payload_exactly() -> None:
    assert serialize_metadata(_metadata()) == GOLDEN_METADATA_PAYLOAD
