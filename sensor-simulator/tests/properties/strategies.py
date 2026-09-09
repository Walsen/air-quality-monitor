"""Hypothesis strategies generating valid contract records.

Shared by the round-trip and stability property tests. Strategies cover all
four Species, both PowerTag values, and all three SiteClassification values so
the properties exercise the whole contract surface (Requirement 16.3).
"""

from __future__ import annotations

import datetime as dt
from typing import cast

from hypothesis import strategies as st

from aqm_simulator.contract.records import (
    PowerTagName,
    RatificationStatusName,
    SensorDataRecord,
    SensorMetadataRecord,
    SiteClassificationName,
    SpeciesName,
)

_SPECIES = ["NO2", "PM25", "NO2Index", "PM25Index"]
_POWER = ["Mains", "Solar"]
_CLASS = ["Roadside", "Urban Background", "Suburban"]


@st.composite
def iso_utc(draw: st.DrawFn) -> str:
    base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    offset = draw(st.integers(min_value=0, max_value=365 * 24))
    ts = base + dt.timedelta(hours=offset)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


@st.composite
def latlon(draw: st.DrawFn) -> str:
    # signed decimal with exactly 7 fractional digits
    whole = draw(st.integers(min_value=-89, max_value=89))
    frac = draw(st.integers(min_value=0, max_value=9_999_999))
    return f"{whole}.{frac:07d}"


@st.composite
def data_records(draw: st.DrawFn) -> SensorDataRecord:
    species = cast(SpeciesName, draw(st.sampled_from(_SPECIES)))
    index = species.endswith("Index")
    return SensorDataRecord(
        Species=species,
        Source="Measurement",
        Units="index" if index else "ug.m-3",
        SiteCode=draw(st.from_regex(r"CB[0-9]{4}", fullmatch=True)),
        DateTime=draw(iso_utc()),
        Duration="PT1H",
        ScaledValue=draw(
            st.floats(min_value=0, max_value=1000, allow_nan=False, allow_infinity=False)
        ),
        RatificationStatus=cast(RatificationStatusName, draw(st.sampled_from(["P", "R"]))),
        SensorContract="Cellular-BO",
    )


@st.composite
def metadata_records(draw: st.DrawFn) -> SensorMetadataRecord:
    return SensorMetadataRecord(
        SiteCode=draw(st.from_regex(r"CB[0-9]{4}", fullmatch=True)),
        SiteName=draw(st.text(min_size=1, max_size=30)),
        DeviceCode=draw(st.from_regex(r"DEV-CB[0-9]{4}", fullmatch=True)),
        InstallationCode=draw(st.none() | st.text(min_size=1, max_size=20)),
        Facility=draw(st.none() | st.text(min_size=1, max_size=20)),
        Latitude=draw(latlon()),
        Longitude=draw(latlon()),
        Borough=draw(st.sampled_from(["Cochabamba", "Sacaba", "Quillacollo"])),
        SiteClassification=cast(SiteClassificationName, draw(st.sampled_from(_CLASS))),
        SensorHeightAboveGround=draw(st.floats(min_value=2.0, max_value=3.0)),
        DistanceToKerb=draw(st.floats(min_value=0.5, max_value=30.0)),
        SponsorName=draw(st.text(min_size=1, max_size=20)),
        SiteLocationType=draw(st.none() | st.text(min_size=1, max_size=20)),
        StartDate=draw(iso_utc()),
        EndDate=draw(st.none() | iso_utc()),
        PowerTag=cast(PowerTagName, draw(st.sampled_from(_POWER))),
        SiteDescription=draw(st.none() | st.text(min_size=1, max_size=20)),
        SitePhotoURL=draw(st.none() | st.text(min_size=1, max_size=20)),
        SensorContract="Cellular-BO",
    )
