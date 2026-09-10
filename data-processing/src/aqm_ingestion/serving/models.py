"""Response models mirroring Requirement 19.2's pinned body shape.

Requirements 19.2, 19.12, 19.13.

ONE MODEL PER NESTED MEMBER, so a missing member fails at CONSTRUCTION rather than being noticed
by a client. Requirement 19.12 is explicit that an unavailable member emits ``null`` rather than
having its key dropped, which is why every optional field is declared with an explicit ``None``
default and serialisation never excludes unset keys: a dropped key and a null value say
different things to a consumer, and only one of them is permitted.

A DISCREPANCY BETWEEN TWO REQUIREMENTS, reconciled here rather than papered over. Requirement
25.5 asks for the RH source, the conversion source and the NowCast window PER SPECIES, while
Requirement 19.2's pinned body carries each as ONE value beside a per-species
``calibrationStrategies`` map. Both are satisfiable at once because of which species each thing
applies to: humidity correction only ever applies to PM2.5 (NO2 uses ``identity``), conversion
only to NO2 (PM2.5 is already a mass concentration), and NowCast only to PM2.5. So at most one
species contributes a non-empty value to each, and the flattening loses nothing. Where no
species contributes one, the member is ``null`` per Requirement 19.12 — and if two ever
disagreed, the mapping refuses rather than silently picking one.

Requirement 19.13: every instant is rendered ISO-8601 UTC at whole-second precision ending in Z.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, field_serializer

from aqm_ingestion.serving.basis import Basis


def iso_z(instant: dt.datetime) -> str:
    """Render an instant as Requirement 19.13 requires.

    Whole seconds and a trailing ``Z``: ``isoformat`` yields ``+00:00`` and would keep any
    microseconds, neither of which the contract permits.
    """
    return instant.astimezone(dt.UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class _ResponseModel(BaseModel):
    """Frozen, and populated by field name so the wire names stay camelCase."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class LocationOut(_ResponseModel):
    """One User_Location as Requirement 19.2 shapes it."""

    name: str
    lat: float
    lon: float


class MeasurementOut(_ResponseModel):
    """One species' measurement at a site.

    ``qualityFlag`` and ``confidence`` are REQUIRED, not optional: Requirement 25.6 and
    Requirement 13.10 say a value is always accompanied by both, and a default would let a
    caller omit one.
    """

    species: str
    reportedValue: float
    correctedValue: float
    units: str
    qualityFlag: str
    confidence: str
    subIndex: int | None = None
    band: str | None = None
    method: str | None = None
    mixingRatioPpb: float | None = None

    @field_serializer("reportedValue", "correctedValue")
    def _round_values(self, value: float) -> float:
        """Round a reported concentration to the contract's two decimal places."""
        return round(value, 2)


class NearestSensorOut(_ResponseModel):
    """One selected site, its measurements, and its Overall_AQI.

    ``overallAqi``, ``band``, ``drivingPollutant`` and ``confidence`` are all nullable together:
    Requirement 20.9 permits a selected site with no fresh Reading, and Requirement 12.6 forbids
    reporting a default band when nothing contributed.
    """

    siteCode: str
    siteName: str | None = None
    siteClassification: str | None = None
    locationName: str | None = None
    distanceKm: float
    asOf: dt.datetime | None = None
    measurements: tuple[MeasurementOut, ...] = ()
    overallAqi: int | None = None
    band: str | None = None
    drivingPollutant: str | None = None
    confidence: str | None = None

    @field_serializer("asOf")
    def _serialize_as_of(self, value: dt.datetime | None) -> str | None:
        """Requirement 19.13's instant form, or null when nothing is fresh."""
        return None if value is None else iso_z(value)


class CrossingOut(_ResponseModel):
    """One Threshold_Crossing as Requirement 19.2 shapes it."""

    siteCode: str
    species: str
    subIndex: int
    threshold: int


class DoseWindowOut(_ResponseModel):
    """One routine window's Inhaled_Dose (Requirement 23.1a).

    Carries no band, severity or risk member, inheriting Requirement 23.8's framing: a dose is
    an
    exposure quantity, and there is nowhere here to put a clinical interpretation of one.
    """

    startTime: str
    durationHours: float
    activityLevel: str
    location: str | None
    concentrationUgM3: float
    breathingRateM3PerH: float
    micrograms: float
    confidence: str


class PersonalizedOut(_ResponseModel):
    """The personalization members of Requirements 20 through 23."""

    condition: str
    sensitivity: str
    usedDefaultProfile: bool
    weightedFocus: tuple[str, ...]
    unavailableWeightedSpecies: tuple[str, ...]
    escalationSubIndex: int | None = None
    thresholdCrossed: bool = False
    thresholdSource: str | None = None
    crossings: tuple[CrossingOut, ...] = ()
    pollen: dict[str, str] | None = None
    inhaledDose: float | None = None
    # Requirement 23.1b: reporting a dose without saying which basis produced it would make a
    # per-window sum and a whole-day figure indistinguishable, and they can differ by a lot.
    doseBasis: str | None = None
    inhaledDoseWindows: tuple[DoseWindowOut, ...] = ()
    unavailableDoseWindows: int = 0


class ForecastOut(_ResponseModel):
    """The Requirement 24 enrichment members."""

    tomorrowAqi: float | None = None
    trend: str | None = None
    source: str | None = None
    retrievedAt: dt.datetime | None = None
    degraded: bool = False

    @field_serializer("retrievedAt")
    def _serialize_retrieved_at(self, value: dt.datetime | None) -> str | None:
        """Requirement 19.13's instant form, or null when nothing was retrieved."""
        return None if value is None else iso_z(value)


class NowcastOut(_ResponseModel):
    """The NowCast window and coverage (Requirement 11.9)."""

    windowHours: int
    hoursAvailable: int
    weightFactor: float


class RecordOut(_ResponseModel):
    """One contributing Reading's identifying fields (Requirement 25.5)."""

    siteCode: str
    species: str
    dateTime: dt.datetime
    duration: str

    @field_serializer("dateTime")
    def _serialize_date_time(self, value: dt.datetime) -> str:
        """Requirement 19.13's instant form."""
        return iso_z(value)


class BasisOut(_ResponseModel):
    """The reviewable basis, in Requirement 19.2's flattened wire shape."""

    breakpointTable: str | None = None
    calibrationStrategies: dict[str, str] = {}
    humiditySource: str | None = None
    conversionSource: str | None = None
    nowcast: NowcastOut | None = None
    records: tuple[RecordOut, ...] = ()


class ServingResponse(_ResponseModel):
    """Requirement 19.2's top-level shape: exactly these members, none dropped."""

    user: str
    generatedAt: dt.datetime
    locations: tuple[LocationOut, ...]
    nearestSensors: tuple[NearestSensorOut, ...]
    personalized: PersonalizedOut
    forecast: ForecastOut
    basis: BasisOut
    advisoryScope: str
    emergencyGuidance: str
    disclaimer: str

    @field_serializer("generatedAt")
    def _serialize_generated_at(self, value: dt.datetime) -> str:
        """Requirement 19.13's instant form."""
        return iso_z(value)


class HistoryReadingOut(_ResponseModel):
    """One Reading in a history response (Requirement 19.3)."""

    dateTime: dt.datetime
    species: str
    correctedValue: float
    units: str
    qualityFlag: str
    confidence: str
    subIndex: int | None = None
    band: str | None = None

    @field_serializer("dateTime")
    def _serialize_date_time(self, value: dt.datetime) -> str:
        """Requirement 19.13's instant form."""
        return iso_z(value)


class HistoryResponse(_ResponseModel):
    """The history body, which carries the Guardrail_Envelope too (Requirement 25.12)."""

    siteCode: str
    startTime: dt.datetime
    endTime: dt.datetime
    readings: tuple[HistoryReadingOut, ...]
    truncated: bool
    basis: BasisOut
    advisoryScope: str
    emergencyGuidance: str
    disclaimer: str

    @field_serializer("startTime", "endTime")
    def _serialize_bounds(self, value: dt.datetime) -> str:
        """Requirement 19.13's instant form."""
        return iso_z(value)


def basis_out(basis: Basis) -> BasisOut:
    """Flatten the internal Basis into Requirement 19.2's wire shape.

    See the module docstring for why the flattening is lossless. A disagreement between two
    species on a single-valued member is refused rather than resolved by picking one, because
    there would be no honest way to report both.

    Raises:
        ValueError: if two species report different non-empty humidity or conversion sources.
    """
    return BasisOut(
        breakpointTable=basis.breakpoint_table,
        calibrationStrategies={
            entry.species: entry.calibration_strategy for entry in basis.species
        },
        humiditySource=_single(
            "humiditySource",
            [
                entry.humidity_source
                for entry in basis.species
                if entry.humidity_source not in (None, "none")
            ],
        ),
        conversionSource=_single(
            "conversionSource",
            [
                entry.conversion_source
                for entry in basis.species
                if entry.conversion_source is not None
            ],
        ),
        nowcast=next(
            (
                NowcastOut(
                    windowHours=entry.nowcast_window_hours,
                    hoursAvailable=entry.nowcast_hours_available or 0,
                    weightFactor=entry.nowcast_weight_factor or 0.0,
                )
                for entry in basis.species
                if entry.nowcast_window_hours is not None
            ),
            None,
        ),
        records=tuple(
            RecordOut(
                siteCode=reference.site_code,
                species=reference.species,
                dateTime=reference.interval_start,
                duration=reference.duration,
            )
            for reference in basis.records
        ),
    )


def _single(member: str, values: list[str]) -> str | None:
    """The one distinct value for a flattened member, or None when there is none.

    Raises:
        ValueError: on a genuine disagreement. Requirement 19.2 gives this member one slot, so
            two different values cannot both be reported and choosing either would misreport the
            other (§5).
    """
    distinct = sorted(set(values))
    if not distinct:
        return None
    if len(distinct) > 1:
        raise ValueError(
            f"Requirement 19.2 carries one {member}, but the readings report {distinct}"
        )
    return distinct[0]


__all__ = [
    "BasisOut",
    "CrossingOut",
    "ForecastOut",
    "HistoryReadingOut",
    "HistoryResponse",
    "LocationOut",
    "MeasurementOut",
    "NearestSensorOut",
    "NowcastOut",
    "PersonalizedOut",
    "RecordOut",
    "ServingResponse",
    "basis_out",
    "iso_z",
]
