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

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_LOCATION_PRECISION = 3
"""Requirement 17.5's default rounding, about 110 m."""

DEFAULT_LOCATION_LIMIT = 5
"""Requirement 17.6's default maximum User_Location count."""

RECOGNIZED_CONSENT_VERSIONS: frozenset[str] = frozenset({"2026-01-01"})
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
    personal_thresholds: Mapping[str, float] = Field(default_factory=dict)
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
