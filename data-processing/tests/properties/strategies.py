"""Shared hypothesis strategies for the contract property tests.

Kept in one place so every property draws records the same way: a drift between
two ad-hoc generators would make two properties disagree about what "valid" means.

Example counts come from the profile registered in conftest (ci = 100, the floor
Requirement 28.11 sets), so no strategy or test hardcodes a lower cap.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from hypothesis import strategies as st

SPECIES = ("NO2", "PM25", "NO2Index", "PM25Index")
RATIFICATION = ("P", "R")
CLASSIFICATIONS = ("Roadside", "Urban Background", "Suburban")
POWER_TAGS = ("Mains", "Solar")

_EPOCH = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)


@st.composite
def utc_timestamps(draw: st.DrawFn) -> str:
    """A whole-second UTC timestamp ending in Z (Requirements 1.3, 2.5)."""
    offset = draw(st.integers(min_value=0, max_value=10 * 365 * 24 * 3600))
    moment = _EPOCH + dt.timedelta(seconds=offset)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@st.composite
def latitudes(draw: st.DrawFn) -> str:
    """A signed decimal string with exactly 7 fractional digits, in range."""
    value = draw(
        st.floats(min_value=-89.9, max_value=89.9, allow_nan=False, allow_infinity=False)
    )
    return f"{value:.7f}"


@st.composite
def longitudes(draw: st.DrawFn) -> str:
    """A signed decimal string with exactly 7 fractional digits, in range."""
    value = draw(
        st.floats(
            min_value=-179.9, max_value=179.9, allow_nan=False, allow_infinity=False
        )
    )
    return f"{value:.7f}"


@st.composite
def data_records(draw: st.DrawFn) -> dict[str, Any]:
    """A valid Sensor_Data_Record as a plain mapping, in declared field order."""
    return {
        "Species": draw(st.sampled_from(SPECIES)),
        "Source": draw(st.sampled_from(["Measurement", "Estimate"])),
        "Units": draw(st.sampled_from(["ug.m-3", "index"])),
        "SiteCode": draw(
            st.text(
                alphabet=st.characters(min_codepoint=48, max_codepoint=90),
                min_size=1,
                max_size=12,
            )
        ),
        "DateTime": draw(utc_timestamps()),
        "Duration": draw(st.sampled_from(["PT1H", "PT15M", "PT24H"])),
        # allow_nan/allow_infinity are off because JSON cannot represent either,
        # so a round-trip through the Serializer could not hold for them.
        "ScaledValue": draw(
            st.floats(
                min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False
            )
        ),
        "RatificationStatus": draw(st.sampled_from(RATIFICATION)),
        "SensorContract": draw(st.sampled_from(["Cellular-BO", "Cellular-REF"])),
    }


@st.composite
def metadata_records(draw: st.DrawFn) -> dict[str, Any]:
    """A valid Sensor_Metadata_Record, with Location built from the siblings.

    ``coordinates`` is Latitude THEN Longitude (Requirement 2.3) and must be
    character-identical to the sibling values, so it is built from the very same
    strings rather than re-formatted.
    """
    latitude = draw(latitudes())
    longitude = draw(longitudes())
    start = draw(utc_timestamps())
    # EndDate is either null (an active site) or no earlier than StartDate, so a
    # generated record never trips the Requirement 2.6 quarantine rule.
    end = draw(st.one_of(st.none(), st.just(start)))
    return {
        "SiteCode": draw(
            st.text(
                alphabet=st.characters(min_codepoint=48, max_codepoint=90),
                min_size=1,
                max_size=12,
            )
        ),
        "SiteName": draw(st.text(min_size=1, max_size=20)),
        "DeviceCode": draw(st.one_of(st.none(), st.text(min_size=1, max_size=12))),
        "InstallationCode": draw(st.one_of(st.none(), st.text(min_size=1, max_size=12))),
        "Facility": draw(st.one_of(st.none(), st.text(min_size=1, max_size=12))),
        "Location": {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [latitude, longitude]},
        },
        "Latitude": latitude,
        "Longitude": longitude,
        "Borough": draw(st.text(min_size=1, max_size=20)),
        "SiteClassification": draw(st.sampled_from(CLASSIFICATIONS)),
        "SensorHeightAboveGround": draw(
            st.floats(min_value=0, max_value=50, allow_nan=False, allow_infinity=False)
        ),
        "DistanceToKerb": draw(
            st.floats(min_value=0, max_value=500, allow_nan=False, allow_infinity=False)
        ),
        "SponsorName": draw(st.text(min_size=1, max_size=30)),
        "SiteLocationType": draw(st.one_of(st.none(), st.text(min_size=1, max_size=20))),
        "StartDate": start,
        "EndDate": end,
        "PowerTag": draw(st.sampled_from(POWER_TAGS)),
        "SiteDescription": draw(st.one_of(st.none(), st.text(max_size=40))),
        "SitePhotoURL": draw(st.one_of(st.none(), st.text(max_size=40))),
        "SensorContract": draw(st.sampled_from(["Cellular-BO", "Cellular-REF"])),
    }


@st.composite
def invalid_data_records(draw: st.DrawFn) -> dict[str, Any]:
    """A record broken in exactly one of the four ways Requirement 3.3 names."""
    record = draw(data_records())
    fault = draw(
        st.sampled_from(["missing_field", "unknown_field", "wrong_type", "invalid_enum"])
    )
    if fault == "missing_field":
        del record[draw(st.sampled_from(sorted(record)))]
    elif fault == "unknown_field":
        record["NotAContractField"] = draw(st.integers())
    elif fault == "wrong_type":
        record["ScaledValue"] = draw(st.text(min_size=1, max_size=6))
    else:
        record["Species"] = draw(
            st.sampled_from(["Ozone", "pm25", "no2", "SO2", "PM10"])
        )
    return record
