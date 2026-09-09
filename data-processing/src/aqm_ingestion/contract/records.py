"""This service's own copy of the record contract.

Requirement 28.3 and assumption A4 keep this copy INDEPENDENT: nothing here
imports from another service directory. The two copies are held together by the
golden-payload tests instead, so a drift fails a test rather than silently
corrupting data.

Two rules here differ from a naive reading and are easy to get wrong:

- ``ScaledValue`` is preserved exactly as received, with no rounding of our own
  (Requirement 1.5). Rounding belongs to calibration and reporting, not to the
  contract, and the archived payload must stay byte-comparable with what arrived
  (Requirement 1.10).
- ``Location.geometry.coordinates`` holds ``Latitude`` THEN ``Longitude``
  (Requirement 2.3), which is the reverse of GeoJSON's usual longitude-first
  convention. The values must be character-identical to the sibling fields, so
  they are compared as strings rather than as parsed numbers.

Validation here is structural only. The cross-field quarantine rules that need a
rejection reason (a coordinate disagreement, an ``EndDate`` before ``StartDate``,
a wrong unit for a concentration species) belong to the validation stage, which
reports them rather than raising.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Case-sensitive enumerations (Requirements 1.2, 2.4).
SpeciesName = Literal["NO2", "PM25", "NO2Index", "PM25Index"]
RatificationStatusName = Literal["P", "R"]
SiteClassificationName = Literal["Roadside", "Urban Background", "Suburban"]
PowerTagName = Literal["Mains", "Solar"]

# ISO-8601 UTC at whole-second precision with a trailing Z and NO fractional
# component (Requirements 1.3, 2.5, 2.6).
_UTC_SECOND_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
Iso8601Utc = Annotated[str, Field(pattern=_UTC_SECOND_PATTERN)]

# A signed decimal string with EXACTLY seven fractional digits (Requirement 2.2).
_LATLON_PATTERN = r"^-?\d+\.\d{7}$"
LatLonString = Annotated[str, Field(pattern=_LATLON_PATTERN)]

# The species carrying a mass concentration, and the unit they must be in
# (Requirements 1.6, 1.8). The index species are stored as received and never
# feed a value this service computes (Requirement 1.7).
MASS_CONCENTRATION_SPECIES: frozenset[str] = frozenset({"NO2", "PM25"})
EXPECTED_CONCENTRATION_UNIT = "ug.m-3"

DATA_FIELD_ORDER: tuple[str, ...] = (
    "Species",
    "Source",
    "Units",
    "SiteCode",
    "DateTime",
    "Duration",
    "ScaledValue",
    "RatificationStatus",
    "SensorContract",
)

METADATA_FIELD_ORDER: tuple[str, ...] = (
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
)

_LATITUDE_LIMIT = 90.0
_LONGITUDE_LIMIT = 180.0


class _StrictModel(BaseModel):
    """Rejects unknown fields, so an unexpected key is a rejection not a silent drop."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)


class PointGeometry(_StrictModel):
    """The GeoJSON Point inside a :class:`GeoLocation`."""

    type: Literal["Point"]
    # Latitude first, then Longitude (Requirement 2.3) — deliberately not the
    # GeoJSON longitude-first order, and kept as STRINGS so the comparison with
    # the sibling fields is character-identical rather than numeric.
    coordinates: tuple[str, str]


class GeoLocation(_StrictModel):
    """The GeoJSON Feature wrapping a :class:`PointGeometry` (Requirement 2.3)."""

    type: Literal["Feature"]
    geometry: PointGeometry


class SensorDataRecord(_StrictModel):
    """One measurement record: exactly the nine fields of Requirement 1.1."""

    Species: SpeciesName
    Source: str
    Units: str
    SiteCode: str = Field(min_length=1)  # non-empty, no other format rule (Req 1.9)
    DateTime: Iso8601Utc
    Duration: str = Field(min_length=1)
    ScaledValue: float  # preserved as received; never rounded here (Req 1.5)
    RatificationStatus: RatificationStatusName
    SensorContract: str

    @property
    def is_mass_concentration(self) -> bool:
        """True when this record carries a mass concentration (Requirement 1.6)."""
        return self.Species in MASS_CONCENTRATION_SPECIES

    @property
    def is_network_index(self) -> bool:
        """True for the emitting network's own index species (Requirement 1.7).

        Such a record is stored as received but never used to derive any
        Sub_Index, Overall_AQI, Band, or Threshold_Crossing this service computes.
        """
        return not self.is_mass_concentration


class SensorMetadataRecord(_StrictModel):
    """One metadata record: exactly the twenty fields of Requirement 2.1, in order."""

    SiteCode: str = Field(min_length=1)
    SiteName: str
    DeviceCode: str | None
    InstallationCode: str | None
    Facility: str | None
    Location: GeoLocation
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

    @field_validator("Latitude")
    @classmethod
    def _latitude_in_range(cls, value: str) -> str:
        if abs(float(value)) > _LATITUDE_LIMIT:
            raise ValueError(
                f"Latitude {value} is outside -90.0000000 to 90.0000000"
            )
        return value

    @field_validator("Longitude")
    @classmethod
    def _longitude_in_range(cls, value: str) -> str:
        if abs(float(value)) > _LONGITUDE_LIMIT:
            raise ValueError(
                f"Longitude {value} is outside -180.0000000 to 180.0000000"
            )
        return value
