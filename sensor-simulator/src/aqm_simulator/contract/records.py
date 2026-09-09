"""Canonical record contract models.

Two Pydantic models reproduce the reference network contract exactly:
``SensorDataRecord`` (``/SensorData``, nine fields) and
``SensorMetadataRecord`` (``/ListSensors``, twenty fields). Contract fidelity
is the service's load-bearing constraint, so field set, field order, JSON value
types, and the presence of every key (even when null) are enforced by the
models themselves rather than in handler bodies (engineering-practices §0).

Field order matters: Pydantic preserves declaration order in
``model_dump``/``model_dump_json``, so the fields are declared in the exact
order Requirements 1.1 and 2.1 mandate. ``Location`` is a stored field derived
from ``Latitude``/``Longitude`` by a before-validator so its coordinates are
character-identical to the sibling values (Requirement 1.3) while keeping the
declared field position.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Case-sensitive enumerations from the contract.
SpeciesName = Literal["NO2", "PM25", "NO2Index", "PM25Index"]
PowerTagName = Literal["Mains", "Solar"]
SiteClassificationName = Literal["Roadside", "Urban Background", "Suburban"]
RatificationStatusName = Literal["P", "R"]

# ISO-8601 UTC timestamp string at whole-second precision ending in Z.
Iso8601Utc = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")]

# Signed decimal with exactly 7 fractional digits (Requirement 1.2).
LatLonString = Annotated[str, Field(pattern=r"^-?\d+\.\d{7}$")]


class _StrictModel(BaseModel):
    """Base: reject unknown fields, validate on assignment, immutable."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)


class SensorDataRecord(_StrictModel):
    """One ``/SensorData`` record (Requirement 2.1 — exactly nine fields)."""

    Species: SpeciesName
    Source: Literal["Measurement"]
    Units: str
    SiteCode: str
    DateTime: Iso8601Utc
    Duration: str
    ScaledValue: float
    RatificationStatus: RatificationStatusName
    SensorContract: str


class PointGeometry(_StrictModel):
    """GeoJSON Point geometry whose coordinates are two contract strings."""

    type: Literal["Point"] = "Point"
    coordinates: tuple[str, str]


class GeoLocation(_StrictModel):
    """GeoJSON Feature wrapping a :class:`PointGeometry` (Requirement 1.3)."""

    type: Literal["Feature"] = "Feature"
    geometry: PointGeometry


class SensorMetadataRecord(_StrictModel):
    """One ``/ListSensors`` record (Requirement 1.1 — exactly twenty fields).

    ``Location`` is derived from ``Latitude``/``Longitude`` so its coordinates
    are character-identical to the sibling values (Requirement 1.3).
    """

    SiteCode: str
    SiteName: str
    DeviceCode: str
    InstallationCode: str | None
    Facility: str | None
    Location: GeoLocation = None  # type: ignore[assignment]  # populated by validator
    Latitude: LatLonString
    Longitude: LatLonString
    Borough: str
    SiteClassification: SiteClassificationName
    SensorHeightAboveGround: float
    DistanceToKerb: float
    SponsorName: str
    SiteLocationType: str | None
    StartDate: Iso8601Utc
    EndDate: Iso8601Utc | None
    PowerTag: PowerTagName
    SiteDescription: str | None
    SitePhotoURL: str | None
    SensorContract: str

    @model_validator(mode="before")
    @classmethod
    def _derive_location(cls, data: Any) -> Any:
        # Build Location from Latitude/Longitude so coordinates are
        # character-identical (Requirement 1.3). Always overridden from the
        # sibling values so the invariant is single-sourced; a caller need not
        # (and should not) supply Location.
        if isinstance(data, dict):
            lat, lon = data.get("Latitude"), data.get("Longitude")
            if isinstance(lat, str) and isinstance(lon, str):
                data = {
                    **data,
                    "Location": {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": (lat, lon)},
                    },
                }
        return data

    @model_validator(mode="after")
    def _round_meter_fields(self) -> SensorMetadataRecord:
        # Requirement 1.2: SensorHeightAboveGround and DistanceToKerb are JSON
        # numbers rounded to 2 decimal places.
        h = round(self.SensorHeightAboveGround, 2)
        k = round(self.DistanceToKerb, 2)
        if (h, k) != (self.SensorHeightAboveGround, self.DistanceToKerb):
            object.__setattr__(self, "SensorHeightAboveGround", h)
            object.__setattr__(self, "DistanceToKerb", k)
        return self
