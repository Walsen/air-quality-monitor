"""Unit tests for this service's own record contract copy (task 2.1).

- Req 1.1: exactly nine field names on a measurement record, all required.
- Req 1.2: case-sensitive Species and RatificationStatus enumerations.
- Req 1.3: DateTime is ISO-8601 UTC, whole seconds, no fraction, trailing Z.
- Req 1.4: Duration is an ISO-8601 duration; the accepted set defaults to PT1H only.
- Req 1.5: ScaledValue is preserved as received — NO rounding of our own.
- Req 1.6: NO2 and PM25 are Mass_Concentration measurements in the named Units.
- Req 1.9: SiteCode carries no format constraint beyond being a non-empty string.
- Req 2.1-2.6: twenty metadata fields in declared order, 7-decimal lat/lon strings
  character-identical to Location.coordinates, the two enumerations, and the
  StartDate/EndDate rules.
- Req 28.3: this is an INDEPENDENT copy — the golden payload below is a captured
  literal, not an import, so drift between the two services fails HERE.
"""

from __future__ import annotations

import json
from typing import cast

import pytest
from pydantic import ValidationError

from aqm_ingestion.contract.records import (
    DATA_FIELD_ORDER,
    METADATA_FIELD_ORDER,
    SensorDataRecord,
    SensorMetadataRecord,
)

# A payload captured from Service 1's serializer. Held as a LITERAL on purpose:
# importing Service 1 would couple the two copies and defeat Requirement 28.3,
# so a drift on either side must break this test instead.
GOLDEN_DATA_PAYLOAD = (
    '{"Species":"PM25","Source":"Measurement","Units":"ug.m-3",'
    '"SiteCode":"CB0001","DateTime":"2026-07-01T11:00:00Z","Duration":"PT1H",'
    '"ScaledValue":14.27,"RatificationStatus":"R","SensorContract":"Cellular-BO"}'
)

GOLDEN_METADATA_PAYLOAD = (
    '{"SiteCode":"CB0001","SiteName":"Cochabamba 0001","DeviceCode":"AQM-CB0001",'
    '"InstallationCode":"INST-CB0001","Facility":"Cochabamba",'
    '"Location":{"type":"Feature","geometry":{"type":"Point",'
    '"coordinates":["-17.3912345","-66.1523456"]}},'
    '"Latitude":"-17.3912345","Longitude":"-66.1523456","Borough":"Cochabamba",'
    '"SiteClassification":"Urban Background","SensorHeightAboveGround":3.0,'
    '"DistanceToKerb":12.5,"SponsorName":"Kanata Air Quality Network",'
    '"SiteLocationType":"Urban Background","StartDate":"2024-01-01T00:00:00Z",'
    '"EndDate":null,"PowerTag":"Mains","SiteDescription":null,'
    '"SitePhotoURL":null,"SensorContract":"Cellular-BO"}'
)


def _data_fields() -> dict[str, object]:
    return cast("dict[str, object]", json.loads(GOLDEN_DATA_PAYLOAD))


def _metadata_fields() -> dict[str, object]:
    return cast("dict[str, object]", json.loads(GOLDEN_METADATA_PAYLOAD))


# --- Req 1.1 field set and order ------------------------------------------

def test_measurement_has_exactly_nine_fields() -> None:
    assert len(DATA_FIELD_ORDER) == 9
    assert DATA_FIELD_ORDER == (
        "Species", "Source", "Units", "SiteCode", "DateTime",
        "Duration", "ScaledValue", "RatificationStatus", "SensorContract",
    )


def test_measurement_dump_preserves_declared_order() -> None:
    record = SensorDataRecord(**_data_fields())  # type: ignore[arg-type]
    assert tuple(record.model_dump(mode="json").keys()) == DATA_FIELD_ORDER


def test_measurement_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        SensorDataRecord(**{**_data_fields(), "Extra": 1})  # type: ignore[arg-type]


@pytest.mark.parametrize("missing", DATA_FIELD_ORDER)
def test_every_measurement_field_is_required(missing: str) -> None:
    fields = _data_fields()
    del fields[missing]
    with pytest.raises(ValidationError):
        SensorDataRecord(**fields)  # type: ignore[arg-type]


# --- Req 1.2 enumerations, case-sensitive ---------------------------------

@pytest.mark.parametrize("species", ["NO2", "PM25", "NO2Index", "PM25Index"])
def test_permitted_species_are_accepted(species: str) -> None:
    record = SensorDataRecord(**{**_data_fields(), "Species": species})  # type: ignore[arg-type]
    assert record.Species == species


@pytest.mark.parametrize("species", ["pm25", "no2", "PM2.5", "Ozone", ""])
def test_other_species_are_rejected(species: str) -> None:
    with pytest.raises(ValidationError):
        SensorDataRecord(**{**_data_fields(), "Species": species})  # type: ignore[arg-type]


@pytest.mark.parametrize("status", ["P", "R"])
def test_permitted_ratification_values_are_accepted(status: str) -> None:
    record = SensorDataRecord(**{**_data_fields(), "RatificationStatus": status})  # type: ignore[arg-type]
    assert record.RatificationStatus == status


@pytest.mark.parametrize("status", ["p", "r", "provisional", "X"])
def test_other_ratification_values_are_rejected(status: str) -> None:
    with pytest.raises(ValidationError):
        SensorDataRecord(**{**_data_fields(), "RatificationStatus": status})  # type: ignore[arg-type]


# --- Req 1.3 DateTime -----------------------------------------------------

@pytest.mark.parametrize(
    "moment",
    [
        "2026-07-01T11:00:00.5Z",  # fractional seconds
        "2026-07-01T11:00:00",      # no zone
        "2026-07-01T11:00:00+00:00",  # offset rather than Z
        "2026-07-01 11:00:00Z",     # space separator
        "not-a-time",
    ],
)
def test_non_conforming_datetime_is_rejected(moment: str) -> None:
    with pytest.raises(ValidationError):
        SensorDataRecord(**{**_data_fields(), "DateTime": moment})  # type: ignore[arg-type]


def test_whole_second_utc_datetime_is_accepted() -> None:
    record = SensorDataRecord(**{**_data_fields(), "DateTime": "2026-01-31T23:59:59Z"})  # type: ignore[arg-type]
    assert record.DateTime == "2026-01-31T23:59:59Z"


# --- Req 1.5 ScaledValue preserved unrounded ------------------------------

@pytest.mark.parametrize("value", [0, 14.27, 14.276543210987, 1e-7, -0.0, 999999.5])
def test_scaled_value_is_preserved_as_received(value: float) -> None:
    record = SensorDataRecord(**{**_data_fields(), "ScaledValue": value})  # type: ignore[arg-type]
    assert record.ScaledValue == value  # no rounding of our own (Req 1.5)


def test_scaled_value_keeps_full_precision_through_dump() -> None:
    precise = 14.276543210987
    record = SensorDataRecord(**{**_data_fields(), "ScaledValue": precise})  # type: ignore[arg-type]
    assert record.model_dump(mode="json")["ScaledValue"] == precise


# --- Req 1.9 SiteCode -----------------------------------------------------

@pytest.mark.parametrize("site", ["CB0001", "RF9999", "any-network-convention", "1"])
def test_sitecode_accepts_any_non_empty_string(site: str) -> None:
    record = SensorDataRecord(**{**_data_fields(), "SiteCode": site})  # type: ignore[arg-type]
    assert record.SiteCode == site


def test_empty_sitecode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SensorDataRecord(**{**_data_fields(), "SiteCode": ""})  # type: ignore[arg-type]


# --- Req 2.1 metadata field set and order ---------------------------------

def test_metadata_has_exactly_twenty_fields() -> None:
    assert len(METADATA_FIELD_ORDER) == 20


def test_metadata_dump_preserves_declared_order() -> None:
    record = SensorMetadataRecord(**_metadata_fields())  # type: ignore[arg-type]
    assert tuple(record.model_dump(mode="json").keys()) == METADATA_FIELD_ORDER


def test_metadata_keeps_null_valued_keys_present() -> None:
    record = SensorMetadataRecord(**_metadata_fields())  # type: ignore[arg-type]
    dumped = record.model_dump(mode="json")
    for nullable in ("EndDate", "SiteDescription", "SitePhotoURL"):
        assert nullable in dumped  # present even when null (Req 2.1)
        assert dumped[nullable] is None


# --- Req 2.2 lat/lon strings ---------------------------------------------

@pytest.mark.parametrize("bad", ["-17.391234", "-17.39123456", "-17.39", "abc", "17"])
def test_latitude_must_have_exactly_seven_decimals(bad: str) -> None:
    fields = _metadata_fields()
    fields["Latitude"] = bad
    with pytest.raises(ValidationError):
        SensorMetadataRecord(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("out_of_range", ["-90.0000001", "90.0000001", "123.0000000"])
def test_latitude_range_is_enforced(out_of_range: str) -> None:
    fields = _metadata_fields()
    fields["Latitude"] = out_of_range
    fields["Location"] = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [out_of_range, "-66.1523456"]},
    }
    with pytest.raises(ValidationError):
        SensorMetadataRecord(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("out_of_range", ["-180.0000001", "180.0000001"])
def test_longitude_range_is_enforced(out_of_range: str) -> None:
    fields = _metadata_fields()
    fields["Longitude"] = out_of_range
    fields["Location"] = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": ["-17.3912345", out_of_range]},
    }
    with pytest.raises(ValidationError):
        SensorMetadataRecord(**fields)  # type: ignore[arg-type]


# --- Req 2.4 metadata enumerations ---------------------------------------

@pytest.mark.parametrize("value", ["Roadside", "Urban Background", "Suburban"])
def test_permitted_classifications_are_accepted(value: str) -> None:
    fields = _metadata_fields()
    fields["SiteClassification"] = value
    assert SensorMetadataRecord(**fields).SiteClassification == value  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["roadside", "URBAN BACKGROUND", "Rural"])
def test_other_classifications_are_rejected(value: str) -> None:
    fields = _metadata_fields()
    fields["SiteClassification"] = value
    with pytest.raises(ValidationError):
        SensorMetadataRecord(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["Mains", "Solar"])
def test_permitted_power_tags_are_accepted(value: str) -> None:
    fields = _metadata_fields()
    fields["PowerTag"] = value
    assert SensorMetadataRecord(**fields).PowerTag == value  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["mains", "Battery", ""])
def test_other_power_tags_are_rejected(value: str) -> None:
    fields = _metadata_fields()
    fields["PowerTag"] = value
    with pytest.raises(ValidationError):
        SensorMetadataRecord(**fields)  # type: ignore[arg-type]


# --- Req 2.3 Location shape ----------------------------------------------

def test_location_requires_feature_and_point_types() -> None:
    fields = _metadata_fields()
    fields["Location"] = {
        "type": "FeatureCollection",
        "geometry": {"type": "Point", "coordinates": ["-17.3912345", "-66.1523456"]},
    }
    with pytest.raises(ValidationError):
        SensorMetadataRecord(**fields)  # type: ignore[arg-type]


def test_location_requires_exactly_two_coordinates() -> None:
    fields = _metadata_fields()
    fields["Location"] = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": ["-17.3912345"]},
    }
    with pytest.raises(ValidationError):
        SensorMetadataRecord(**fields)  # type: ignore[arg-type]


def test_coordinates_are_latitude_then_longitude() -> None:
    record = SensorMetadataRecord(**_metadata_fields())  # type: ignore[arg-type]
    assert record.Location.geometry.coordinates == (record.Latitude, record.Longitude)


# --- Req 2.6 EndDate -----------------------------------------------------

def test_null_enddate_denotes_an_active_site() -> None:
    record = SensorMetadataRecord(**_metadata_fields())  # type: ignore[arg-type]
    assert record.EndDate is None


def test_enddate_accepts_a_whole_second_utc_timestamp() -> None:
    fields = _metadata_fields()
    fields["EndDate"] = "2026-06-30T00:00:00Z"
    assert SensorMetadataRecord(**fields).EndDate == "2026-06-30T00:00:00Z"  # type: ignore[arg-type]


# --- Req 28.3 golden payload drift guard ---------------------------------

def test_golden_measurement_payload_parses_unchanged() -> None:
    record = SensorDataRecord(**_data_fields())  # type: ignore[arg-type]
    assert json.loads(GOLDEN_DATA_PAYLOAD) == record.model_dump(mode="json")


def test_golden_metadata_payload_parses_unchanged() -> None:
    record = SensorMetadataRecord(**_metadata_fields())  # type: ignore[arg-type]
    assert json.loads(GOLDEN_METADATA_PAYLOAD) == record.model_dump(mode="json")


def test_no_import_from_another_service() -> None:
    """Req 28.3: the contract copy must not reach into another service directory."""
    import aqm_ingestion.contract.records as module

    source = module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        body = handle.read()
    assert "aqm_simulator" not in body
    assert "sensor_simulator" not in body
