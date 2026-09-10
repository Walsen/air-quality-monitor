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
from pydantic_core import ErrorDetails

DEFAULT_LOCATION_PRECISION = 3
"""Requirement 17.5's default rounding, about 110 m."""

DEFAULT_LOCATION_LIMIT = 5
"""Requirement 17.6's default maximum User_Location count."""

DEFAULT_MEDICATION_LIMIT = 10
"""Requirement 30.4's default maximum Medication_Entry count."""

DEFAULT_ROUTINE_LIMIT = 14
"""Requirement 30.6's default maximum Routine_Entry count.

Fourteen rather than seven because a routine is one activity window, not one day: a commute out
and a commute back on each of five weekdays is already ten.
"""

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


class MedicationRole(StrEnum):
    """Requirement 30.2's permitted Medication_Role values.

    The role is the ONLY clinical property stored about a medication, and it is stored because
    it is the only one preparedness guidance needs: "have your reliever to hand" is expressible
    from a role, while "take two puffs" is not expressible from anything this model holds.
    """

    RELIEVER = "reliever"
    PREVENTER = "preventer"
    OTHER = "other"


class Weekday(StrEnum):
    """The days a Routine_Entry may name (Requirement 30.5)."""

    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


WEEKDAY_ORDER: tuple[Weekday, ...] = (
    Weekday.MONDAY,
    Weekday.TUESDAY,
    Weekday.WEDNESDAY,
    Weekday.THURSDAY,
    Weekday.FRIDAY,
    Weekday.SATURDAY,
    Weekday.SUNDAY,
)
"""The canonical weekday order Requirement 30.8 sorts by.

Declared explicitly rather than relying on ``StrEnum`` definition order or on alphabetical
comparison — ``"friday" < "monday"`` alphabetically, which would order a Friday routine before
a Monday one and make the per-window dose report of Requirement 23.1a depend on a spelling
accident. A test pins that this covers every member, since a missing day would make its own
routines unsortable.
"""

_WEEKDAY_INDEX: Mapping[Weekday, int] = {
    day: index for index, day in enumerate(WEEKDAY_ORDER)
}

HOURS_PER_DAY = 24.0
"""Requirement 30.7's bound: a routine may not run past the end of its start day."""


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

    max_activity_duration_hours: float = 24.0
    """Requirement 23.6's configured maximum, inclusive ("up to the configured maximum")."""

    medication_limit: int = DEFAULT_MEDICATION_LIMIT
    """Requirement 30.4's configured maximum Medication_Entry count."""

    routine_limit: int = DEFAULT_ROUTINE_LIMIT
    """Requirement 30.6's configured maximum Routine_Entry count."""


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


class MedicationEntry(_MinimalModel):
    """One medication the user already has: a display name and a role. Nothing else.

    Requirement 30.3 forbids storing a dose, frequency, route, prescriber or administration
    schedule, and this model satisfies it STRUCTURALLY rather than by validation — there is no
    field any of those could occupy, so dosing advice downstream has no stored input to draw on.
    That is the same technique Requirement 25.8 applies to the Audit_Record, and it is stronger
    than a rule because it cannot be forgotten at a later call site.

    A consequence worth stating: this model can never answer "when did I last take it", and that
    is deliberate. Answering it would require a schedule, and a schedule is what makes "you are
    due a dose" expressible.
    """

    name: str = Field(min_length=1, max_length=100)
    role: MedicationRole


class RoutineEntry(_MinimalModel):
    """One recurring activity window (Requirement 30.5).

    ``start_time`` is a NAIVE time of day, which is the one place in this codebase where a
    tz-aware value is the wrong shape: a routine happens at seven in the morning wherever the
    user is, so it is a wall-clock time and not an instant. An offset-carrying value is rejected
    rather than dropped, because silently discarding it would change the hour it means.
    """

    days: tuple[Weekday, ...] = Field(min_length=1)
    start_time: dt.time
    duration_hours: Annotated[float, Field(gt=0.0)]
    activity_level: ActivityLevel
    location: LocationName | None = None

    @field_validator("days")
    @classmethod
    def _ordered_and_unique(cls, value: tuple[Weekday, ...]) -> tuple[Weekday, ...]:
        """Collapse repeats and impose Requirement 30.8's order within the entry.

        A repeated day would make the per-window dose report of Requirement 23.1a count that day
        twice, which is a wrong number rather than a cosmetic flaw.
        """
        return tuple(sorted(set(value), key=lambda day: _WEEKDAY_INDEX[day]))

    @field_validator("start_time")
    @classmethod
    def _must_be_wall_clock(cls, value: dt.time) -> dt.time:
        """Refuse an offset-carrying time of day — see the class docstring."""
        if value.tzinfo is not None:
            raise ValueError(
                "start_time is a wall-clock time of day and must not carry a UTC offset"
            )
        return value

    @model_validator(mode="after")
    def _must_not_cross_midnight(self) -> RoutineEntry:
        """Enforce Requirement 30.7's day boundary.

        Landing exactly on midnight is permitted: 30.7 forbids extending PAST the end of the
        start day, and a window ending at 24:00 has not passed it. A window that wrapped would
        belong partly to a day its ``days`` set may not even name, so the dose it produced could
        not be attributed.

        The message names the STATIC range and not the hours actually remaining. "at most 1
        hour" would satisfy 30.7's "naming the permitted range" while disclosing that the start
        time is 23:00 — and a start time is a profile field, so Requirement 30.9 forbids putting
        it in an error message. The two clauses only appear to conflict: 30.7 wants the range,
        30.9 forbids the value, and a range derived from the value is still the value.
        """
        start_hours = (
            self.start_time.hour
            + self.start_time.minute / 60
            + self.start_time.second / 3600
            + self.start_time.microsecond / 3_600_000_000
        )
        if start_hours + self.duration_hours > HOURS_PER_DAY:
            raise ValueError(
                "duration_hours must be greater than 0 and must not extend the window past "
                f"the end of its start day, which is {HOURS_PER_DAY:g} hours long"
            )
        return self

    def sort_key(self) -> tuple[int, dt.time]:
        """Requirement 30.8's ordering: day of week, then start time.

        Keyed on the EARLIEST day in the entry, since an entry may name several. That makes the
        order total across entries whose day sets overlap, which a per-day key would not.
        """
        return (_WEEKDAY_INDEX[self.days[0]], self.start_time)


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
    # Requirement 23.6: "greater than 0", not at-or-above — a zero-duration activity never
    # happened, so storing one would record an event that did not occur. The configured MAXIMUM
    # is checked in build_profile, since it is configuration the model must not reach for (§1).
    activity_duration_hours: Annotated[float | None, Field(gt=0.0)] = None
    medications: tuple[MedicationEntry, ...] = ()
    routines: tuple[RoutineEntry, ...] = ()
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

    # Requirements 30.4 and 30.6. Checked BEFORE model validation so an over-long list is
    # rejected naming the limit rather than by whichever entry happens to be malformed — the
    # caller needs to know the cap, which a per-entry error would not tell them.
    _check_collection_limit(prepared, "medications", limits.medication_limit)
    _check_collection_limit(prepared, "routines", limits.routine_limit)

    profile = _validate_without_echoing(prepared)
    profile = _with_ordered_collections(profile)

    if profile.consent.version not in limits.consent_versions:
        raise _consent_error()

    # Requirement 22.4: a threshold must name a species the Breakpoint_Table defines, since
    # Requirement 22.5 converts it through that table. Checked here rather than on the model
    # because the permitted set is configuration the model must not reach for (§1).
    for species in sorted(profile.personal_thresholds):
        if species not in limits.threshold_species:
            raise _threshold_species_error(species, limits.threshold_species)
        _check_threshold_unit(species, profile.personal_thresholds[species])

    # Requirement 23.6's upper bound. Configuration, so it lives here rather than on the model.
    duration = profile.activity_duration_hours
    if duration is not None and duration > limits.max_activity_duration_hours:
        raise _duration_range_error(limits.max_activity_duration_hours)
    return profile


def _check_collection_limit(
    prepared: Mapping[str, object], field: str, limit: int
) -> None:
    """Reject an over-long medication or routine list naming the LIMIT (Requirements 30.4/30.6).

    Names the field and the cap and never an entry, since a medication name is a profile field
    under Requirement 30.9 and so may not appear in an error message.
    """
    value = prepared.get(field)
    if isinstance(value, (list, tuple)) and len(value) > limit:
        from pydantic_core import InitErrorDetails, ValidationError

        raise ValidationError.from_exception_data(
            "UserProfile",
            [
                InitErrorDetails(
                    type="value_error",
                    loc=(field,),
                    input=None,
                    ctx={
                        "error": ValueError(
                            f"at most {limit} {field} entries are accepted, "
                            f"received {len(value)}"
                        )
                    },
                )
            ],
        )


def _with_ordered_collections(profile: UserProfile) -> UserProfile:
    """Impose a defined order on the routine and medication lists.

    Requirement 30.8 for routines — day of week then start time — and practice §2 for
    medications, which names only "any iteration reaching output" and does not privilege one
    collection. Ordering happens once, HERE, rather than at each read site, so two readers
    cannot disagree about what "the first routine" means.
    """
    ordered_routines = tuple(sorted(profile.routines, key=lambda entry: entry.sort_key()))
    ordered_medications = tuple(
        sorted(profile.medications, key=lambda entry: (entry.name, entry.role.value))
    )
    if ordered_routines == profile.routines and ordered_medications == profile.medications:
        return profile
    return profile.model_copy(
        update={"routines": ordered_routines, "medications": ordered_medications}
    )


def _validate_without_echoing(prepared: Mapping[str, object]) -> UserProfile:
    """Validate, re-raising any failure with the submitted VALUES stripped.

    Requirement 17.9 covers error messages, and pydantic's own ``extra_forbidden`` error
    includes the offending input — so a rejected medication name or diagnosis code would
    otherwise travel in the very error that rejected it, which is the opposite of what the
    allowlist is for. This catches the original and rebuilds it from the field LOCATIONS
    alone.

    One exception, and it is what the ``value_error`` branch below is for. A ``value_error`` is
    raised by a validator in THIS module, whose messages are written to Requirement 17.9 and
    name a field and a permitted range rather than a submitted value. Discarding those too would
    satisfy 17.9 while violating Requirement 30.7 and §5, which require the rejection to name
    the field and the range — the caller cannot fix a routine window from "failed validation".
    Every other error type is pydantic's own and carries the input, so it still gets the generic
    text. ``input`` is set to None either way, since the rendered form of an error appends it.
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
                ctx={"error": ValueError(_safe_message(entry))},
            )
            for entry in error.errors()
        ]
        raise CoreValidationError.from_exception_data("UserProfile", details) from None


def _safe_message(entry: ErrorDetails) -> str:
    """The message for one rejected field, keeping only what cannot echo a value."""
    location = ".".join(str(part) for part in entry["loc"])
    if entry["type"] == "value_error":
        # Raised by a validator in this module; see _validate_without_echoing.
        return f"field {location!r}: {entry['msg']}"
    return (
        f"field {location!r} is not accepted by the User_Profile allowlist "
        "or failed validation"
    )


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


def _duration_range_error(maximum: float) -> Exception:
    """A Requirement 23.6 rejection naming the field and the permitted RANGE.

    Names the range, not the submitted duration: §5 wants the permitted range in the message,
    while Requirement 17.9 still covers the submitted value, which is a profile field.
    """
    from pydantic_core import InitErrorDetails, ValidationError

    return ValidationError.from_exception_data(
        "UserProfile",
        [
            InitErrorDetails(
                type="value_error",
                loc=("activity_duration_hours",),
                input=None,
                ctx={
                    "error": ValueError(
                        f"activity_duration_hours must be greater than 0 and no more than "
                        f"{maximum:g} hours"
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
