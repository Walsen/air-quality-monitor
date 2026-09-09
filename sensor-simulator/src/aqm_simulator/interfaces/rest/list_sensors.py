"""``/ListSensors`` query validation and metadata projection.

Validation lives here in a parameter object rather than in the handler body
(engineering-practices §1), and ACCUMULATES faults so one response names every
offending parameter with its permitted range instead of only the first
(Requirement 1.13, §5). Unrecognized parameter names are ignored rather than
rejected (Requirement 1.14), which is why the query is read from the raw mapping
instead of a strict model that would reject unknown keys.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from aqm_simulator.contract.records import SensorMetadataRecord
from aqm_simulator.geography.distance import great_circle_km
from aqm_simulator.swarm.factory import VirtualSensor

# Only these names act as filters; anything else is ignored (Requirement 1.14).
_TEXT_FILTERS = {
    "SiteCode": "SiteCode",
    "Borough": "Borough",
    "Sponsor": "SponsorName",  # Sponsor matches SponsorName (Requirement 1.9)
    "Facility": "Facility",
}
_LAT_RANGE = (-90.0, 90.0)
_LON_RANGE = (-180.0, 180.0)
_MAX_RADIUS_KM = 500.0
_DEFAULT_START = "2024-01-01T00:00:00Z"


class QueryError(ValueError):
    """One or more query parameters were rejected (Requirement 1.13)."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class ListSensorsQuery:
    """The validated, recognized subset of a /ListSensors query."""

    text_filters: dict[str, str]
    latitude: float | None
    longitude: float | None
    radius_km: float | None

    @property
    def has_radius(self) -> bool:
        return self.radius_km is not None


def _parse_number(
    raw: str, name: str, low: float, high: float, problems: list[str]
) -> float | None:
    """Parse one numeric parameter, appending a problem instead of raising."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        problems.append(
            f"{name} must be a number between {low} and {high}; got {raw!r}"
        )
        return None
    # reject nan/inf explicitly: both survive float() but compare falsely
    if value != value or value in (float("inf"), float("-inf")):
        problems.append(
            f"{name} must be a finite number between {low} and {high}; got {raw!r}"
        )
        return None
    if not low <= value <= high:
        problems.append(f"{name} must be between {low} and {high}; got {value}")
        return None
    return value


def parse_list_sensors_query(params: dict[str, str]) -> ListSensorsQuery:
    """Validate a /ListSensors query, naming every offending parameter."""
    problems: list[str] = []
    text_filters = {
        field: params[name]
        for name, field in _TEXT_FILTERS.items()
        if name in params and params[name] != ""
    }

    latitude = longitude = radius = None
    if "Latitude" in params:
        latitude = _parse_number(params["Latitude"], "Latitude", *_LAT_RANGE, problems)
    if "Longitude" in params:
        longitude = _parse_number(
            params["Longitude"], "Longitude", *_LON_RANGE, problems
        )
    if "RadiusKM" in params:
        radius = _parse_number(
            params["RadiusKM"], "RadiusKM", 0.0, _MAX_RADIUS_KM, problems
        )
        if radius == 0.0:
            problems.append(
                f"RadiusKM must be greater than 0 and at most {_MAX_RADIUS_KM}; got 0"
            )
            radius = None

    # a radius needs BOTH coordinates (Requirement 1.11)
    if "RadiusKM" in params:
        missing = [
            name for name in ("Latitude", "Longitude") if params.get(name, "") == ""
        ]
        if missing:
            problems.append(
                "RadiusKM requires both Latitude and Longitude; missing: "
                + ", ".join(missing)
            )

    if problems:
        raise QueryError(problems)
    return ListSensorsQuery(text_filters, latitude, longitude, radius)


def metadata_for(sensor: VirtualSensor, sensor_contract: str) -> SensorMetadataRecord:
    """Project a Virtual_Sensor onto its Sensor_Metadata_Record."""
    return SensorMetadataRecord(
        SiteCode=sensor.site_code,
        SiteName=sensor.site_name,
        DeviceCode=sensor.device_code,
        InstallationCode=sensor.installation_code,
        Facility=sensor.borough,  # the hosting facility label for this deployment
        Latitude=sensor.latitude,
        Longitude=sensor.longitude,
        Borough=sensor.borough,
        SiteClassification=sensor.classification,  # type: ignore[arg-type]
        SensorHeightAboveGround=sensor.sensor_height_m,
        DistanceToKerb=sensor.distance_to_kerb_m,
        SponsorName="Air Quality Monitor",
        SiteLocationType=sensor.classification,
        StartDate=_DEFAULT_START,
        EndDate=None,
        PowerTag=sensor.power_tag,  # type: ignore[arg-type]
        SiteDescription=None,
        SitePhotoURL=None,
        SensorContract=sensor_contract,
    )


def select_sensors(
    records: list[SensorMetadataRecord], query: ListSensorsQuery
) -> list[SensorMetadataRecord]:
    """Apply the recognized filters, ordered by ascending SiteCode (Req 1.8)."""
    selected = []
    for record in records:
        if not _matches_text(record, query.text_filters):
            continue
        if query.has_radius and not _within_radius(record, query):
            continue
        selected.append(record)
    return sorted(selected, key=lambda r: r.SiteCode)


def _matches_text(record: SensorMetadataRecord, filters: dict[str, str]) -> bool:
    """Whole-field, case-insensitive match on every supplied filter (Req 1.9)."""
    for field, wanted in filters.items():
        actual = getattr(record, field, None)
        if actual is None or str(actual).casefold() != wanted.casefold():
            return False
    return True


def _within_radius(record: SensorMetadataRecord, query: ListSensorsQuery) -> bool:
    """Great-circle distance <= RadiusKM, boundary inclusive (Req 1.10)."""
    assert query.latitude is not None and query.longitude is not None
    assert query.radius_km is not None
    distance = great_circle_km(
        query.latitude, query.longitude, float(record.Latitude), float(record.Longitude)
    )
    return distance <= query.radius_km


def utc_now_iso(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
