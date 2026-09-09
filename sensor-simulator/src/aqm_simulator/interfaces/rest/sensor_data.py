"""``/SensorData`` query validation, windowing, and filtering.

Two contrasts with ``/ListSensors`` are deliberate and easy to get wrong:

- field filters here are matched CASE-SENSITIVELY (Requirement 2.12), where
  ``/ListSensors`` matches case-insensitively (Requirement 1.9);
- ``RadiusKM`` is permitted over 0.01 to 500 km (Requirement 2.18), where
  ``/ListSensors`` permits any value above 0 up to 500 (Requirement 1.13).

The served window is always intersected with the retention window, measured
backward from the current simulated timestamp, so a ``startTime`` earlier than
retention yields 200 with the in-window records rather than an error
(Requirements 14.7, 14.11). Faults accumulate so one response names every
offending parameter (§5).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from aqm_simulator.contract.records import SensorDataRecord
from aqm_simulator.geography.distance import great_circle_km
from aqm_simulator.interfaces.rest.list_sensors import QueryError

PERMITTED_SPECIES = ("PM25", "NO2", "PM25Index", "NO2Index")
_MIN_RADIUS_KM = 0.01
_MAX_RADIUS_KM = 500.0
_LAT_RANGE = (-90.0, 90.0)
_LON_RANGE = (-180.0, 180.0)
# Query names that filter on sensor METADATA rather than on the record itself.
_METADATA_FILTERS = {"Borough": "Borough", "Sponsor": "SponsorName", "Facility": "Facility"}


@dataclass(frozen=True, slots=True)
class SensorDataQuery:
    """The validated, recognized subset of a /SensorData query."""

    species: str | None
    site_code: str | None
    metadata_filters: dict[str, str]
    latitude: float | None
    longitude: float | None
    radius_km: float | None
    start: dt.datetime | None
    end: dt.datetime | None

    @property
    def has_radius(self) -> bool:
        return self.radius_km is not None


def _parse_timestamp(raw: str, name: str, problems: list[str]) -> dt.datetime | None:
    """Parse an ISO-8601 UTC timestamp, appending a problem instead of raising."""
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        problems.append(
            f"{name} must be an ISO-8601 UTC timestamp ending in Z; got {raw!r}"
        )
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _parse_number(
    raw: str, name: str, low: float, high: float, problems: list[str]
) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        problems.append(f"{name} must be a number between {low} and {high}; got {raw!r}")
        return None
    if value != value or value in (float("inf"), float("-inf")):
        problems.append(f"{name} must be a finite number; got {raw!r}")
        return None
    if not low <= value <= high:
        problems.append(f"{name} must be between {low} and {high}; got {value}")
        return None
    return value


def parse_sensor_data_query(params: dict[str, str]) -> SensorDataQuery:
    """Validate a /SensorData query, naming every offending parameter."""
    problems: list[str] = []

    species = params.get("Species") or None
    if species is not None and species not in PERMITTED_SPECIES:
        # matched case-sensitively (Requirement 2.15)
        problems.append(
            "Species must be one of the permitted values "
            f"{', '.join(PERMITTED_SPECIES)}; got {species!r}"
        )
        species = None

    # startTime and endTime must be supplied together (Requirement 2.16)
    raw_start = params.get("startTime", "")
    raw_end = params.get("endTime", "")
    if raw_start and not raw_end:
        problems.append("startTime requires endTime; endTime is missing")
    if raw_end and not raw_start:
        problems.append("endTime requires startTime; startTime is missing")

    start = _parse_timestamp(raw_start, "startTime", problems) if raw_start else None
    end = _parse_timestamp(raw_end, "endTime", problems) if raw_end else None
    if start is not None and end is not None and start > end:
        problems.append(
            f"invalid time range: startTime {raw_start} is later than endTime {raw_end}"
        )

    latitude = longitude = radius = None
    if params.get("Latitude"):
        latitude = _parse_number(params["Latitude"], "Latitude", *_LAT_RANGE, problems)
    if params.get("Longitude"):
        longitude = _parse_number(params["Longitude"], "Longitude", *_LON_RANGE, problems)
    if params.get("RadiusKM"):
        radius = _parse_number(
            params["RadiusKM"], "RadiusKM", _MIN_RADIUS_KM, _MAX_RADIUS_KM, problems
        )
        missing = [n for n in ("Latitude", "Longitude") if not params.get(n)]
        if missing:
            problems.append(
                "RadiusKM requires both Latitude and Longitude; missing: "
                + ", ".join(missing)
            )

    if problems:
        raise QueryError(problems)

    return SensorDataQuery(
        species=species,
        site_code=params.get("SiteCode") or None,
        metadata_filters={
            field: params[name]
            for name, field in _METADATA_FILTERS.items()
            if params.get(name)
        },
        latitude=latitude,
        longitude=longitude,
        radius_km=radius,
        start=start,
        end=end,
    )


def resolve_window(
    query: SensorDataQuery,
    now: dt.datetime,
    publish_minutes: int,
    retention_days: int,
) -> tuple[dt.datetime, dt.datetime]:
    """Resolve the served window, clipped to retention (Req 2.10, 14.7, 14.11)."""
    interval = dt.timedelta(minutes=publish_minutes)
    retention_start = now - dt.timedelta(days=retention_days)

    if query.start is None or query.end is None:
        # the most recently COMPLETED interval: the latest whose end is <= now
        completed_end = _floor_to_interval(now, interval)
        start, end = completed_end - interval, completed_end
    else:
        start, end = query.start, query.end

    # never serve outside retention, and never beyond simulated now
    start = max(start, retention_start)
    end = min(end, now)
    if end < start:
        end = start  # empty window rather than an inverted one
    return start, end


def _floor_to_interval(moment: dt.datetime, interval: dt.timedelta) -> dt.datetime:
    """Round down to the nearest Publish_Interval boundary."""
    seconds = int(interval.total_seconds())
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    elapsed = int((moment - epoch).total_seconds())
    return epoch + dt.timedelta(seconds=elapsed - (elapsed % seconds))


def select_records(
    records: list[SensorDataRecord],
    query: SensorDataQuery,
    metadata_by_site: dict[str, dict[str, object]],
    coordinates_by_site: dict[str, tuple[float, float]],
) -> list[SensorDataRecord]:
    """Apply the case-sensitive filters and radius, ascending DateTime (Req 2.11)."""
    selected: list[SensorDataRecord] = []
    for record in records:
        if query.species is not None and record.Species != query.species:
            continue
        if query.site_code is not None and record.SiteCode != query.site_code:
            continue
        if not _metadata_matches(record, query, metadata_by_site):
            continue
        if query.has_radius and not _within_radius(record, query, coordinates_by_site):
            continue
        selected.append(record)
    return sorted(selected, key=lambda r: (r.DateTime, r.SiteCode, r.Species))


def _metadata_matches(
    record: SensorDataRecord,
    query: SensorDataQuery,
    metadata_by_site: dict[str, dict[str, object]],
) -> bool:
    """Case-SENSITIVE equality on the metadata-backed filters (Requirement 2.12)."""
    if not query.metadata_filters:
        return True
    metadata = metadata_by_site.get(record.SiteCode)
    if metadata is None:
        return False
    for field, wanted in query.metadata_filters.items():
        if str(metadata.get(field, "")) != wanted:
            return False
    return True


def _within_radius(
    record: SensorDataRecord,
    query: SensorDataQuery,
    coordinates_by_site: dict[str, tuple[float, float]],
) -> bool:
    assert query.latitude is not None and query.longitude is not None
    assert query.radius_km is not None
    position = coordinates_by_site.get(record.SiteCode)
    if position is None:
        return False
    distance = great_circle_km(query.latitude, query.longitude, *position)
    return distance <= query.radius_km
