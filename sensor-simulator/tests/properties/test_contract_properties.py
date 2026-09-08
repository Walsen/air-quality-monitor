"""Contract-fidelity property tests (tasks 2.4, 2.5, 2.6, 2.8).

Feature: sensor-simulator-service
- Property 1: Sensor data record round-trip (Req 3.3, 2.1, 2.5, 2.6, 2.7)
- Property 2: Sensor metadata record round-trip (Req 3.4, 1.1, 1.2, 1.3, 1.7)
- Property 3: Serialize-parse-serialize stability (Req 3.9)
- Property 6: Ratification status by age (Req 2.8, 2.9)
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from aqm_simulator.contract.parser import parse_data, parse_metadata
from aqm_simulator.contract.ratification import (
    DEFAULT_RATIFICATION_LAG,
    derive_ratification_status,
)
from aqm_simulator.contract.serializer import (
    round_half_away_from_zero,
    serialize_data,
    serialize_metadata,
)
from tests.properties.strategies import data_records, metadata_records


@given(record=data_records())
def test_property_1_data_record_round_trip(record: object) -> None:
    """Feature: sensor-simulator-service, Property 1."""
    text = serialize_data(record)  # type: ignore[arg-type]
    parsed = parse_data(text)[0]
    assert parsed.Species == record.Species  # type: ignore[attr-defined]
    assert parsed.SiteCode == record.SiteCode  # type: ignore[attr-defined]
    assert parsed.DateTime == record.DateTime  # type: ignore[attr-defined]
    # ScaledValue equal at 2 decimal places
    assert parsed.ScaledValue == round_half_away_from_zero(record.ScaledValue)  # type: ignore[attr-defined]


@given(record=metadata_records())
def test_property_2_metadata_record_round_trip(record: object) -> None:
    """Feature: sensor-simulator-service, Property 2."""
    text = serialize_metadata(record)  # type: ignore[arg-type]
    parsed = parse_metadata(text)[0]
    # every field equal, including null EndDate and 7-dp lat/lon char-for-char
    assert parsed.model_dump() == record.model_dump()  # type: ignore[attr-defined]
    assert parsed.Latitude == record.Latitude  # type: ignore[attr-defined]
    assert parsed.Longitude == record.Longitude  # type: ignore[attr-defined]


@given(record=data_records())
def test_property_3_serialize_parse_serialize_stable(record: object) -> None:
    """Feature: sensor-simulator-service, Property 3."""
    once = serialize_data(record)  # type: ignore[arg-type]
    twice = serialize_data(parse_data(once)[0])
    assert once == twice


@given(
    record_dt=st.datetimes(
        min_value=dt.datetime(2020, 1, 1),
        max_value=dt.datetime(2030, 1, 1),
        timezones=st.just(dt.UTC),
    ),
    age_days=st.integers(min_value=0, max_value=400),
)
def test_property_6_ratification_by_age(record_dt: dt.datetime, age_days: int) -> None:
    """Feature: sensor-simulator-service, Property 6."""
    ref = record_dt + dt.timedelta(days=age_days)
    status = derive_ratification_status(record_dt, ref)
    expected = "R" if dt.timedelta(days=age_days) >= DEFAULT_RATIFICATION_LAG else "P"
    assert status == expected
