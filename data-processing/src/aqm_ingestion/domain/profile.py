"""The User_Profile: an allowlist over health-adjacent data.

Every decision here is governed by §7's minimisation rule and Requirement 17's criteria, and
three of them are worth stating because a looser choice would look equally reasonable:

**An allowlist REJECTS rather than ignores** (Requirement 17.4). Silently dropping a medication
name or a symptom narrative would still have accepted the request, leaving the caller believing
the data was stored and the operator unaware it was ever sent. `extra="forbid"` makes the write
fail and names the field.

**Locations are rounded on the way IN, not on the way out** (Requirement 17.5). Rounding at
read time would mean the precise coordinate was stored, and a store is exactly where a
subpoena, a backup, or a log of a failed write can find it. Three decimals is about 110 m —
precise enough to select nearby sensors, too coarse to identify a dwelling.

**The model refuses to render its own contents** (Requirement 17.9). The logger redacts by key
name, which cannot help when a profile is interpolated into an f-string or passed as one object.
So ``__repr__`` shows only the pseudonymous identity, which Requirement 17.9 explicitly permits
in diagnostics. That is a structural guarantee rather than a discipline: a careless caller
cannot leak what the object will not print.

Note the tension the key-name redactor cannot resolve on its own: Requirement 15.9 REQUIRES
logging a site's latitude and longitude, while Requirement 17.9 forbids logging a user's. Same
shape, different subject — so the redactor must not blanket "latitude", and the profile's own
opacity is what covers the user's side.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_LOCATION_PRECISION = 3
"""Requirement 17.5's default rounding, about 110 m."""

DEFAULT_LOCATION_LIMIT = 5
"""Requirement 17.6's default maximum User_Location count."""

RECOGNIZED_CONSENT_VERSIONS: frozenset[str] = frozenset({"2026-01-01"})

DEFAULT_THRESHOLD_SPECIES: tuple[str, ...] = ("PM25", "NO2")
"""Species a Personal_Threshold may name by default (Requirement 22.4).

Mirrors the shipped Breakpoint_Tables. Deliberately NOT imported from the AQI layer: the profile
model is validated against configuration handed to it, so it keeps no dependency on a registry.
The caller overrides this via ``ProfileLimits.threshold_species``, and a test asserts the two
agree so this constant cannot drift from the tables that actually ship.
"""

SPECIES_THRESHOLD_UNITS: Mapping[str, str] = {"PM25": "ug.m-3", "NO2": "ppb"}
"""The unit each species' Breakpoint_Table interpolates in (Requirement 22.5).

The strings are the CONTRACT's own spellings — `ug.m-3`, as Requirement 1.8's Units field and
the shipped table both write it, not the `ug/m3` one would guess. A test asserts each entry
equals its table's own unit, which is how that guess was caught.

A concentration threshold in any other unit is REJECTED rather than converted, because silently
reinterpreting ppb as µg/m³ would move the user's trigger point instead of failing.
"""
"""Requirement 17.7's recognized consent versions.

Configuration rather than a constant in spirit: an unrecognised version must be REJECTED, so
the set has to be explicit somewhere, and the Config_Loader replaces it.
"""


class Condition(StrEnum):
    """Requirement 17.3's permitted Condition values."""

    ASTHMA = "asthma"
    COPD = "copd"
    ALLERGIC_RHINITIS = "allergic_rhinitis"
    ASTHMA_COPD_OVERLAP = "asthma_copd_overlap"
    NONE_DECLARED = "none_declared"


class SensitivityLevel(StrEnum):
    """Requirement 17.3's permitted Sensitivity_Level values."""

    STANDARD = "standard"
    ELEVATED = "elevated"
    HIGH = "high"


class LocationName(StrEnum):
    """Requirement 17.5's permitted User_Location names."""

    HOME = "home"
    WORK = "work"
    COMMUTE = "commute"


class ThresholdKind(StrEnum):
    """Which of Requirement 22.4's two forms a Personal_Threshold takes.

    This enum exists because a bare number cannot say. PM2.5 at 35 µg/m³ is a Sub_Index around
    100 — the bottom of the Orange band — while Sub_Index 35 is comfortably Good, so reading one
    as the other either warns a user almost continuously or never warns them at all.
    """

    SUB_INDEX = "sub_index"
    CONCENTRATION = "concentration"


class ActivityLevel(StrEnum):
    """Requirement 23.2's permitted activity levels."""

    REST = "rest"
    LIGHT = "light"
    MODERATE = "moderate"
    VIGOROUS = "vigorous"


DEFAULT_PROFILE_CONDITION = Condition.NONE_DECLARED
"""Requirement 17.12's default Condition."""

DEFAULT_PROFILE_SENSITIVITY = SensitivityLevel.STANDARD
"""Requirement 17.12's default Sensitivity_Level."""


@dataclass(frozen=True, slots=True)
class ProfileLimits:
    """The configured bounds a profile write is validated against (§1)."""

    location_precision: int = DEFAULT_LOCATION_PRECISION
    location_limit: int = DEFAULT_LOCATION_LIMIT
    consent_versions: frozenset[str] = RECOGNIZED_CONSENT_VERSIONS
    threshold_species: frozenset[str] = frozenset(DEFAULT_THRESHOLD_SPECIES)
    """Species a Personal_Threshold may name (Requirement 22.4).

    Passed IN rather than read from a Breakpoint_Table registry here, so the profile model keeps
    no dependency on the AQI layer and §1's narrow-parameter rule holds. The caller supplies
    ``registry.species_for(table_id)``.
    """


DEFAULT_PROFILE_LIMITS = ProfileLimits()


class _MinimalModel(BaseModel):
    """Frozen, and an allowlist: an unknown field is a rejection (Requirement 17.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ConsentRecord(_MinimalModel):
    """Requirement 17.7's Consent_Record: a version and when it was given."""

    version: str
    given_at: dt.datetime

    @field_validator("given_at")
    @classmethod
    def _must_be_aware(cls, value: dt.datetime) -> dt.datetime:
        """Refuse a naive instant (Requirement 27.8, §2)."""
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("consent given_at must be a timezone-aware UTC instant")
        return value.astimezone(dt.UTC)


class PersonalThreshold(_MinimalModel):
    """One species' trigger point, in whichever of Requirement 22.4's forms the user gave.

    ``unit`` is REQUIRED for a concentration and REFUSED for a Sub_Index. A unit on a Sub_Index
    means the caller has confused the two forms, which is precisely the confusion this model
    exists to prevent, so it is a rejection rather than an ignored extra.
    """

    kind: ThresholdKind
    value: float
    unit: str | None = None

    @model_validator(mode="after")
    def _check_form(self) -> PersonalThreshold:
        """Enforce Requirement 22.4's ranges and the unit's presence per form."""
        if self.kind is ThresholdKind.SUB_INDEX:
            if self.unit is not None:
                raise ValueError(
                    "a Sub_Index threshold must not carry a unit; use kind='concentration' "
                    "to express a threshold in µg/m³ or ppb"
                )
            # Requirement 22.4: "from 1 to 500", both ends inclusive.
            if not 1 <= self.value <= 500:
                raise ValueError("a Sub_Index threshold must be between 1 and 500 inclusive")
        else:
            if self.unit is None:
                raise ValueError(
                    "a concentration threshold must name its unit, so it can be converted "
                    "through the same Breakpoint_Table the response uses"
                )
            if self.value < 0:
                raise ValueError("a concentration threshold cannot be negative")
        return self


class UserLocation(_MinimalModel):
    """One saved location, at reduced precision (Requirement 17.5)."""

    name: LocationName
    latitude: Annotated[float, Field(ge=-90.0, le=90.0)]
    longitude: Annotated[float, Field(ge=-180.0, le=180.0)]


class UserProfile(_MinimalModel):
    """Exactly the fields Requirement 17.2 declares, and nothing else.

    Does not render its own contents — see the module docstring.
    """

    user_id: str
    condition: Condition
    sensitivity_level: SensitivityLevel
    personal_thresholds: Mapping[str, PersonalThreshold] = Field(default_factory=dict)
    locations: tuple[UserLocation, ...] = ()
    activity_level: ActivityLevel | None = None
    activity_duration_hours: Annotated[float | None, Field(ge=0.0)] = None
    consent: ConsentRecord
    created_at: dt.datetime
    updated_at: dt.datetime

    def __repr__(self) -> str:
        """Reveal only the pseudonymous identity (Requirement 17.9).

        Requirement 17.9 permits referring to a profile in diagnostics BY the pseudonymous
        identity, and forbids everything else — so that is exactly what this prints.
        """
        return f"UserProfile(user_id={self.user_id!r}, <redacted>)"

    __str__ = __repr__


def build_profile(
    fields: Mapping[str, object],
    limits: ProfileLimits = DEFAULT_PROFILE_LIMITS,
) -> UserProfile:
    """Validate and minimise a profile write (Requirements 17.2-17.7).

    Args:
        fields: the submitted fields, exactly as received.
        limits: the configured precision, location cap, and consent versions.

    Returns:
        The stored profile, with locations already reduced in precision.

    Raises:
        pydantic.ValidationError: naming the offending FIELD for any allowlist violation, an
            unrecognised consent version, or a location count over the limit. The message never
            echoes a submitted VALUE, since Requirement 17.9 covers error messages too.
    """
    prepared = dict(fields)
    raw_locations = prepared.get("locations")
    if isinstance(raw_locations, (list, tuple)):
        if len(raw_locations) > limits.location_limit:
            raise _limit_error(limits.location_limit, len(raw_locations))
        prepared["locations"] = tuple(
            _round_location(entry, limits.location_precision)
            for entry in raw_locations
        )

    profile = _validate_without_echoing(prepared)

    if profile.consent.version not in limits.consent_versions:
        raise _consent_error()

    # Requirement 22.4: a threshold must name a species the Breakpoint_Table defines, since
    # Requirement 22.5 converts it through that table. Checked here rather than on the model
    # because the permitted set is configuration the model must not reach for (§1).
    for species in sorted(profile.personal_thresholds):
        if species not in limits.threshold_species:
            raise _threshold_species_error(species, limits.threshold_species)
        _check_threshold_unit(species, profile.personal_thresholds[species])
    return profile


def _validate_without_echoing(prepared: Mapping[str, object]) -> UserProfile:
    """Validate, re-raising any failure with the submitted VALUES stripped.

    Requirement 17.9 covers error messages, and pydantic's own ``extra_forbidden`` error
    includes the offending input — so a rejected medication name or diagnosis code would
    otherwise travel in the very error that rejected it, which is the opposite of what the
    allowlist is for. This catches the original and rebuilds it from the field LOCATIONS
    alone.
    """
    from pydantic import ValidationError
    from pydantic_core import InitErrorDetails
    from pydantic_core import ValidationError as CoreValidationError

    try:
        return UserProfile.model_validate(prepared)
    except ValidationError as error:
        details = [
            InitErrorDetails(
                type="value_error",
                loc=tuple(entry["loc"]),
                input=None,  # never the submitted value
                ctx={
                    "error": ValueError(
                        f"field {'.'.join(str(part) for part in entry['loc'])!r} is not "
                        "accepted by the User_Profile allowlist or failed validation"
                    )
                },
            )
            for entry in error.errors()
        ]
        raise CoreValidationError.from_exception_data("UserProfile", details) from None


def _round_location(entry: object, precision: int) -> object:
    """Round a location's coordinates BEFORE storage (Requirement 17.5).

    On the way IN, so the precise coordinate is never stored — see the module docstring.
    """
    if not isinstance(entry, Mapping):
        return entry
    reduced = dict(entry)
    for axis in ("latitude", "longitude"):
        value = reduced.get(axis)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            reduced[axis] = round(float(value), precision)
    return reduced


def _limit_error(limit: int, received: int) -> Exception:
    """A location-count rejection naming the limit (Requirement 17.6)."""
    from pydantic_core import InitErrorDetails, ValidationError

    return ValidationError.from_exception_data(
        "UserProfile",
        [
            InitErrorDetails(
                type="value_error",
                loc=("locations",),
                input=received,
                ctx={
                    "error": ValueError(
                        f"at most {limit} User_Location entries are accepted, "
                        f"received {received}"
                    )
                },
            )
        ],
    )


def _threshold_species_error(species: str, permitted: frozenset[str]) -> Exception:
    """A Requirement 22.4 rejection for a species no Breakpoint_Table defines.

    Names the SPECIES and the permitted set, which Requirement 17.9 allows: a species name is
    not health-adjacent in the way a threshold VALUE is, and without it the operator cannot tell
    a typo from a genuinely unsupported pollutant (§5).
    """
    from pydantic_core import InitErrorDetails, ValidationError

    return ValidationError.from_exception_data(
        "UserProfile",
        [
            InitErrorDetails(
                type="value_error",
                loc=("personal_thresholds", species),
                input=None,
                ctx={
                    "error": ValueError(
                        f"no Breakpoint_Table defines {species!r}, so a threshold for it "
                        f"could not be compared to a Sub_Index; permitted: "
                        f"{', '.join(sorted(permitted))}"
                    )
                },
            )
        ],
    )


def _threshold_unit_error(species: str, expected: str) -> Exception:
    """A Requirement 22.4 rejection for a concentration threshold in the wrong unit."""
    from pydantic_core import InitErrorDetails, ValidationError

    return ValidationError.from_exception_data(
        "UserProfile",
        [
            InitErrorDetails(
                type="value_error",
                loc=("personal_thresholds", species, "unit"),
                input=None,
                ctx={
                    "error": ValueError(
                        f"a concentration threshold for {species} must be expressed in "
                        f"{expected}, the unit its Breakpoint_Table uses"
                    )
                },
            )
        ],
    )


def _check_threshold_unit(species: str, threshold: PersonalThreshold) -> None:
    """Refuse a concentration threshold whose unit its table would not accept.

    Converting ppb as though it were µg/m³ would silently move the user's trigger point rather
    than fail, which is why a mismatch is a rejection and not a coercion.
    """
    if threshold.kind is not ThresholdKind.CONCENTRATION:
        return
    expected = SPECIES_THRESHOLD_UNITS.get(species)
    if expected is not None and threshold.unit != expected:
        raise _threshold_unit_error(species, expected)


def _consent_error() -> Exception:
    """An unrecognised-consent rejection naming the field (Requirement 17.7).

    Names the FIELD and not the submitted version, because Requirement 17.9 covers error
    messages and a rejected value is still a submitted value.
    """
    from pydantic_core import InitErrorDetails, ValidationError

    return ValidationError.from_exception_data(
        "UserProfile",
        [
            InitErrorDetails(
                type="value_error",
                loc=("consent", "version"),
                input=None,
                ctx={
                    "error": ValueError(
                        "consent version is not recognized by this configuration"
                    )
                },
            )
        ],
    )
