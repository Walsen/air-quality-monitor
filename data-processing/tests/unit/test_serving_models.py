"""Unit tests for the serving response models (task 25.1).

Requirements 19.2, 19.12, 19.13.

The top-level member list is transcribed FROM Requirement 19.2, not read off the model, so a
model that quietly gained or lost a member fails here rather than shipping a body no documented
client expects.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from aqm_ingestion.domain.models import (
    CalibratedReading,
    Confidence,
    DedupKey,
    QualityFlag,
)
from aqm_ingestion.serving.basis import assemble_basis
from aqm_ingestion.serving.models import (
    BasisOut,
    ForecastOut,
    LocationOut,
    MeasurementOut,
    NearestSensorOut,
    PersonalizedOut,
    ServingResponse,
    basis_out,
    iso_z,
)

_NOW = dt.datetime(2026, 9, 8, 1, 0, 0, tzinfo=dt.UTC)

# Transcribed from Requirement 19.2's own prose, in its order.
_REQUIRED_TOP_LEVEL = (
    "user",
    "generatedAt",
    "locations",
    "nearestSensors",
    "personalized",
    "forecast",
    "basis",
    "advisoryScope",
    "emergencyGuidance",
    "disclaimer",
)


def _reading(
    species: str = "PM25",
    *,
    strategy: str = "rh_linear",
    humidity_source: str = "provider",
    conversion_source: str | None = None,
    window: int | None = 12,
) -> CalibratedReading:
    return CalibratedReading(
        key=DedupKey(
            site_code="CB0086", species=species, interval_start=_NOW, duration="PT1H"
        ),
        reported_value=24.1,
        corrected_value=18.2,
        units="ug.m-3",
        quality_flag=QualityFlag.CALIBRATED,
        confidence=Confidence.HIGH,
        calibration_strategy=strategy,
        breakpoint_table="epa-2024-05-06",
        ratification_status="R",
        ingested_at=_NOW,
        archive_id="archive-1",
        sub_index=68,
        band="Moderate",
        humidity_source=humidity_source,  # type: ignore[arg-type]
        conversion_source=conversion_source,  # type: ignore[arg-type]
        conversion_temperature_k=288.15 if conversion_source else None,
        conversion_pressure_pa=74000.0 if conversion_source else None,
        nowcast_window_hours=window,
        nowcast_hours_available=12 if window else None,
        nowcast_weight_factor=0.72 if window else None,
    )


def _response(**overrides: object) -> ServingResponse:
    base: dict[str, object] = {
        "user": "u_123",
        "generatedAt": _NOW,
        "locations": (LocationOut(name="home", lat=-17.394, lon=-66.157),),
        "nearestSensors": (),
        "personalized": PersonalizedOut(
            condition="asthma",
            sensitivity="high",
            usedDefaultProfile=False,
            weightedFocus=("PM25", "NO2"),
            unavailableWeightedSpecies=("O3",),
        ),
        "forecast": ForecastOut(),
        "basis": BasisOut(),
        "advisoryScope": "exposure-reduction",
        "emergencyGuidance": "guidance",
        "disclaimer": "disclaimer",
    }
    return ServingResponse(**(base | overrides))  # type: ignore[arg-type]


# --- Req 19.2: exactly these top-level members -------------------------

def test_the_top_level_members_are_exactly_the_requirements() -> None:
    assert set(ServingResponse.model_fields) == set(_REQUIRED_TOP_LEVEL)


def test_the_top_level_order_matches_the_requirement() -> None:
    # The pinned example lists them in an order; keeping it makes a diff against the spec
    # readable, and pydantic preserves declaration order in the dump.
    assert tuple(ServingResponse.model_fields) == _REQUIRED_TOP_LEVEL


def test_a_missing_top_level_member_fails_at_construction() -> None:
    # 25.1's point: a member is missed at CONSTRUCTION, not by a client reading the body.
    with pytest.raises(ValidationError):
        ServingResponse(user="u_123")  # type: ignore[call-arg]


def test_an_unknown_member_is_refused() -> None:
    with pytest.raises(ValidationError):
        _response(surpriseMember="nope")


# --- Req 19.12: null, never a dropped key ------------------------------

def test_an_unavailable_member_is_null_not_dropped() -> None:
    body = _response().model_dump()
    forecast = body["forecast"]
    # Every forecast member is unavailable here, and each key must still be present.
    for member in ("tomorrowAqi", "trend", "source", "retrievedAt"):
        assert member in forecast
        assert forecast[member] is None


def test_no_top_level_key_is_ever_dropped() -> None:
    body = _response().model_dump()
    assert set(body) == set(_REQUIRED_TOP_LEVEL)


def test_an_unavailable_overall_aqi_is_null_not_dropped() -> None:
    # Req 20.9's stale site and Req 12.6's no-default-band meet here.
    sensor = NearestSensorOut(siteCode="CB0086", distanceKm=0.4)
    dumped = sensor.model_dump()
    for member in ("overallAqi", "band", "drivingPollutant", "confidence", "asOf"):
        assert member in dumped
        assert dumped[member] is None


def test_a_measurement_requires_its_flag_and_confidence() -> None:
    # Req 25.6 / 13.10: a value is always accompanied by both, so neither has a default.
    with pytest.raises(ValidationError):
        MeasurementOut(  # type: ignore[call-arg]
            species="PM25",
            reportedValue=24.1,
            correctedValue=18.2,
            units="ug.m-3",
        )


# --- Req 19.13: instants are whole-second UTC with Z -------------------

def test_an_instant_is_rendered_with_a_trailing_z() -> None:
    assert iso_z(_NOW) == "2026-09-08T01:00:00Z"


def test_microseconds_are_dropped() -> None:
    assert iso_z(_NOW.replace(microsecond=123456)) == "2026-09-08T01:00:00Z"


def test_a_non_utc_instant_is_converted_not_relabelled() -> None:
    # Relabelling would move the instant by the offset while claiming Z, which is worse than
    # rejecting it.
    offset = dt.datetime(2026, 9, 8, 3, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert iso_z(offset) == "2026-09-08T01:00:00Z"


def test_the_serialised_generated_at_is_the_z_form() -> None:
    body = _response().model_dump()
    assert body["generatedAt"] == "2026-09-08T01:00:00Z"


def test_no_serialised_instant_uses_an_offset_suffix() -> None:
    import json

    rendered = json.dumps(_response().model_dump())
    assert "+00:00" not in rendered


# --- Req 19.2 vs 25.5: the flattened basis ----------------------------

def test_the_basis_carries_a_per_species_strategy_map() -> None:
    basis = assemble_basis([_reading("PM25"), _reading("NO2", strategy="identity")])
    out = basis_out(basis)
    assert out.calibrationStrategies == {"PM25": "rh_linear", "NO2": "identity"}


def test_the_humidity_source_flattens_to_the_species_that_used_one() -> None:
    # PM2.5 corrects for humidity; NO2 uses identity and reports "none". Req 19.2 has one slot,
    # and the flattening is lossless precisely because only one species contributes.
    basis = assemble_basis(
        [
            _reading("PM25", humidity_source="provider"),
            _reading("NO2", strategy="identity", humidity_source="none", window=None),
        ]
    )
    assert basis_out(basis).humiditySource == "provider"


def test_the_conversion_source_flattens_to_the_species_that_needed_one() -> None:
    basis = assemble_basis(
        [
            _reading("PM25", conversion_source=None),
            _reading("NO2", strategy="identity", conversion_source="default", window=None),
        ]
    )
    assert basis_out(basis).conversionSource == "default"


def test_an_absent_humidity_source_is_null() -> None:
    basis = assemble_basis(
        [_reading("NO2", strategy="identity", humidity_source="none", window=None)]
    )
    assert basis_out(basis).humiditySource is None


def test_the_nowcast_block_comes_from_the_species_that_used_one() -> None:
    basis = assemble_basis([_reading("PM25", window=12)])
    nowcast = basis_out(basis).nowcast
    assert nowcast is not None
    assert nowcast.windowHours == 12
    assert nowcast.hoursAvailable == 12
    assert nowcast.weightFactor == 0.72


def test_an_absent_nowcast_is_null() -> None:
    basis = assemble_basis([_reading("NO2", strategy="identity", window=None)])
    assert basis_out(basis).nowcast is None


def test_a_genuine_disagreement_on_a_flattened_member_is_refused() -> None:
    # The flattening is only lossless while one species contributes. If two ever did, Req 19.2
    # has one slot and choosing either would misreport the other, so it raises rather than
    # silently picking (§5). This is the case that makes the flattening honest rather than
    # lucky.
    basis = assemble_basis(
        [
            _reading("PM25", humidity_source="provider"),
            _reading("NO2", strategy="identity", humidity_source="channel", window=None),
        ]
    )
    with pytest.raises(ValueError, match="humiditySource"):
        basis_out(basis)


def test_the_basis_records_carry_the_requirements_field_names() -> None:
    # Req 19.2 names dateTime and duration, not interval_start.
    out = basis_out(assemble_basis([_reading()]))
    record = out.records[0].model_dump()
    assert set(record) == {"siteCode", "species", "dateTime", "duration"}
    assert record["dateTime"] == "2026-09-08T01:00:00Z"


def test_an_empty_basis_serialises_with_null_members() -> None:
    out = basis_out(assemble_basis([]))
    dumped = out.model_dump()
    assert dumped["breakpointTable"] is None
    assert dumped["humiditySource"] is None
    assert dumped["nowcast"] is None
    assert dumped["records"] == ()


# --- rounding ---------------------------------------------------------

def test_a_measurement_value_is_rounded_to_two_decimals() -> None:
    measurement = MeasurementOut(
        species="PM25",
        reportedValue=24.126,
        correctedValue=18.244,
        units="ug.m-3",
        qualityFlag="calibrated",
        confidence="high",
    )
    dumped = measurement.model_dump()
    assert dumped["reportedValue"] == 24.13
    assert dumped["correctedValue"] == 18.24


def test_the_models_are_immutable() -> None:
    response = _response()
    with pytest.raises(ValidationError):
        response.user = "someone-else"
