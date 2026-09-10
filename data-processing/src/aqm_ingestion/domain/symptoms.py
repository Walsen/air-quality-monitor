"""The Symptom_Log: what the user reports about themselves (Requirement 31).

This is the most sensitive data the service holds — the profile implies a condition, but a diary
records a course of illness — so §7's minimisation governs every decision here, and two of them
are worth stating because a looser choice would look equally reasonable.

**The Symptom_Note computes nothing, and that is enforced by a type rather than a rule**
(Requirement 31.6). The note exists for the user's own recall. It is prose, which makes it
simultaneously a clinical narrative and an injection vector, and keeping it out of every
derivation is what lets it exist at all. So the Requirement 32 association does not consume a
``SymptomEntry``: it consumes a :class:`SeverityObservation`, which has a date and a severity
and
no field a note or a marker could occupy. "The note reaches no computation" is therefore a
property of a type, checkable by a test, rather than a rule every future call site must
remember.

**One entry per calendar date, and a second write REPLACES** (Requirement 31.7). Accumulating
would let one day contribute twice to the association, which is a wrong number rather than
untidy storage. A pleasant side effect, noted because it was not the reason: a replacing write
is
naturally idempotent, so a redelivered request cannot corrupt the series.

Retention (Requirement 31.8) is applied at QUERY time against the injected Clock, mirroring the
Readings store's reasoning: the exclusion then moves with the clock, so stored data ages out
correctly without anything having to run on a timer.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import ErrorDetails, InitErrorDetails, ValidationError

MIN_SEVERITY = 1
MAX_SEVERITY = 5
"""Requirement 31.3's inclusive Symptom_Severity range."""

DEFAULT_NOTE_MAX_LENGTH = 280
"""Requirement 31.5's default Symptom_Note bound."""

DEFAULT_SYMPTOM_RETENTION_DAYS = 365
"""Requirement 31.8's default retention window.

Longer than the Readings default of 90 days, which is deliberate and has a consequence
Requirement 32.5 handles: an association can only pair a diary entry with exposure data that
still exists, so its effective reach is the SHORTER of the two windows, not this one.
"""


class SymptomMarker(StrEnum):
    """Requirement 31.4's default closed marker set.

    Every member is something a person can observe about themselves without a clinician. There
    is
    deliberately no `exacerbation` or `attack`: those are clinical characterisations, and
    Requirement 31 stores observations rather than assessments.
    """

    COUGH = "cough"
    WHEEZE = "wheeze"
    BREATHLESSNESS = "breathlessness"
    CHEST_TIGHTNESS = "chest_tightness"
    NASAL_CONGESTION = "nasal_congestion"
    SLEEP_DISTURBANCE = "sleep_disturbance"


MARKER_ORDER: tuple[SymptomMarker, ...] = (
    SymptomMarker.COUGH,
    SymptomMarker.WHEEZE,
    SymptomMarker.BREATHLESSNESS,
    SymptomMarker.CHEST_TIGHTNESS,
    SymptomMarker.NASAL_CONGESTION,
    SymptomMarker.SLEEP_DISTURBANCE,
)
"""The canonical marker order, so a stored set has a defined iteration order (§2).

Declared explicitly for the same reason :data:`WEEKDAY_ORDER` is: alphabetical order over these
names is not the order anyone means, and relying on it would make output depend on spelling.
"""

_MARKER_INDEX: Mapping[SymptomMarker, int] = {
    marker: index for index, marker in enumerate(MARKER_ORDER)
}

DEFAULT_SYMPTOM_MARKERS: frozenset[SymptomMarker] = frozenset(SymptomMarker)


@dataclass(frozen=True, slots=True)
class SymptomLogLimits:
    """The configured bounds a Symptom_Entry write is validated against (§1)."""

    note_max_length: int = DEFAULT_NOTE_MAX_LENGTH
    markers: frozenset[SymptomMarker] = DEFAULT_SYMPTOM_MARKERS
    retention_days: int = DEFAULT_SYMPTOM_RETENTION_DAYS


DEFAULT_SYMPTOM_LIMITS = SymptomLogLimits()


class SymptomEntry(BaseModel):
    """One day's report, exactly as Requirement 31.2 declares it.

    Frozen and an allowlist: an unknown field is a rejection, not an ignored extra, for the same
    reason the User_Profile refuses one — silently dropping a submitted clinical field would
    leave
    the caller believing it was stored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str
    entry_date: dt.date
    severity: int = Field(ge=MIN_SEVERITY, le=MAX_SEVERITY, strict=True)
    markers: tuple[SymptomMarker, ...] = ()
    reliever_used: bool
    note: str | None = None
    recorded_at: dt.datetime

    @field_validator("markers")
    @classmethod
    def _ordered_and_unique(
        cls, value: tuple[SymptomMarker, ...]
    ) -> tuple[SymptomMarker, ...]:
        """Collapse repeats and impose the canonical order (§2)."""
        return tuple(sorted(set(value), key=lambda marker: _MARKER_INDEX[marker]))

    @field_validator("recorded_at")
    @classmethod
    def _must_be_aware(cls, value: dt.datetime) -> dt.datetime:
        """Refuse a naive instant (§2)."""
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("recorded_at must be a timezone-aware UTC instant")
        return value.astimezone(dt.UTC)

    def __repr__(self) -> str:
        """Reveal only the identity and the date (Requirement 31.10).

        Requirement 31.10 permits logging "at most the pseudonymous identity and the date", so
        that is exactly what this prints. Same structural guarantee the User_Profile makes: a
        careless f-string cannot leak what the object will not render.
        """
        return (
            f"SymptomEntry(user_id={self.user_id!r}, "
            f"entry_date={self.entry_date.isoformat()!r}, <redacted>)"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class SeverityObservation:
    """One day's severity, and NOTHING else — the association's only input.

    This type is the enforcement mechanism for Requirement 31.6. It has a date and a severity,
    so
    a Symptom_Note, a marker set and even the user identity cannot reach a computation through
    it.
    A test pins the field set, because adding a field here would quietly widen what the
    association can see.
    """

    on: dt.date
    severity: int


def severity_series(entries: Iterable[SymptomEntry]) -> tuple[SeverityObservation, ...]:
    """Project entries onto the association's input, ordered by date ascending.

    The ordering is Requirement 32.11's, established here rather than in the association because
    this is where the series is built — so there is one place that decides it, and the
    association
    cannot be handed an unordered series by a different caller.
    """
    return tuple(
        SeverityObservation(on=entry.entry_date, severity=entry.severity)
        for entry in sorted(entries, key=lambda entry: entry.entry_date)
    )


def build_symptom_entry(
    fields: Mapping[str, object],
    *,
    now: dt.datetime,
    limits: SymptomLogLimits = DEFAULT_SYMPTOM_LIMITS,
) -> SymptomEntry:
    """Validate a Symptom_Entry write (Requirements 31.2-31.5, 31.11).

    Args:
        fields: the submitted fields, exactly as received.
        now: the injected Clock's instant — used for ``recorded_at`` and to reject a future
        date.
        limits: the configured note bound, marker set, and retention window.

    Returns:
        The entry to store.

    Raises:
        pydantic.ValidationError: naming the offending FIELD, and the permitted range or set
        where
            there is one. Never echoing a submitted value, since Requirement 31.10 covers error
            messages as well as logs.
    """
    prepared = dict(fields)
    prepared.setdefault("recorded_at", now)

    _check_note_bound(prepared, limits.note_max_length)
    _check_markers(prepared, limits.markers)

    entry = _validate_without_echoing(prepared)

    # Requirement 31.11. Compared against the injected Clock's DATE, never a wall clock.
    if entry.entry_date > now.date():
        raise _field_error(
            "entry_date",
            "entry_date must not be in the future relative to the service clock",
        )
    return entry


def retention_floor(now: dt.datetime, retention_days: int) -> dt.date:
    """The oldest calendar date still inside the retention window (Requirement 31.8)."""
    return (now - dt.timedelta(days=retention_days)).date()


def effective_reach_days(symptom_retention: int, readings_retention: int) -> int:
    """The SHORTER of the two retention windows (Requirement 32.5).

    A diary entry with no surviving exposure data cannot be paired, so counting it as an
    observation would inflate the sample the association claims to rest on. With the shipped
    defaults — 365 days of diary, 90 of Readings — this returns 90, so the default configuration
    exercises the clamp rather than leaving it dormant.
    """
    return min(symptom_retention, readings_retention)


def _check_note_bound(prepared: Mapping[str, object], maximum: int) -> None:
    """Reject an over-long note naming the BOUND (Requirement 31.5).

    Checked here rather than on the model because the bound is configuration, which §1 keeps out
    of the model. Names the length limit and never the note, which is the most sensitive string
    the service holds.
    """
    note = prepared.get("note")
    if isinstance(note, str) and len(note) > maximum:
        raise _field_error(
            "note", f"note must be at most {maximum} characters"
        )


def _check_markers(prepared: Mapping[str, object], permitted: frozenset[SymptomMarker]) -> None:
    """Reject an unrecognised marker naming the RECOGNIZED SET (Requirement 31.4).

    Names the permitted set rather than the submitted value: a caller cannot correct a typo from
    "invalid marker", and the recognised set is not health data about this user, while their
    submitted string is.
    """
    markers = prepared.get("markers")
    if not isinstance(markers, (list, tuple, set, frozenset)):
        return
    allowed = {marker.value for marker in permitted}
    for marker in markers:
        raw = marker.value if isinstance(marker, SymptomMarker) else marker
        if not isinstance(raw, str) or raw not in allowed:
            raise _field_error(
                "markers",
                f"unrecognized Symptom_Marker; recognized: {', '.join(sorted(allowed))}",
            )


def _validate_without_echoing(prepared: Mapping[str, object]) -> SymptomEntry:
    """Validate, re-raising any failure with the submitted VALUES stripped.

    The same treatment the User_Profile gets, and for the same reason: pydantic's own errors
    embed the offending input, so a rejected note or severity would travel in the very error
    that rejected it. Our own validators' messages are preserved because they are written to
    Requirement 31.10 and name a field and a range rather than a value.
    """
    try:
        return SymptomEntry.model_validate(prepared)
    except ValidationError as error:
        details = [
            InitErrorDetails(
                type="value_error",
                loc=tuple(entry["loc"]),
                input=None,
                ctx={"error": ValueError(_safe_message(entry))},
            )
            for entry in error.errors()
        ]
        raise ValidationError.from_exception_data("SymptomEntry", details) from None


def _safe_message(entry: ErrorDetails) -> str:
    """The message for one rejected field, keeping only what cannot echo a value."""
    location = ".".join(str(part) for part in entry["loc"])
    if entry["type"] == "value_error":
        return f"field {location!r}: {entry['msg']}"
    if location == "severity":
        # Req 31.3 requires the RANGE in the message, and pydantic's own ge/le text would
        # otherwise be replaced by the generic wording below.
        return (
            f"field {location!r}: severity must be a whole number from "
            f"{MIN_SEVERITY} to {MAX_SEVERITY} inclusive"
        )
    return f"field {location!r} is not accepted by the Symptom_Entry shape or failed validation"


def _field_error(field: str, message: str) -> ValidationError:
    """A rejection naming one field, carrying no submitted value."""
    return ValidationError.from_exception_data(
        "SymptomEntry",
        [
            InitErrorDetails(
                type="value_error",
                loc=(field,),
                input=None,
                ctx={"error": ValueError(message)},
            )
        ],
    )


__all__ = [
    "DEFAULT_NOTE_MAX_LENGTH",
    "DEFAULT_SYMPTOM_LIMITS",
    "DEFAULT_SYMPTOM_MARKERS",
    "DEFAULT_SYMPTOM_RETENTION_DAYS",
    "MARKER_ORDER",
    "MAX_SEVERITY",
    "MIN_SEVERITY",
    "SeverityObservation",
    "SymptomEntry",
    "SymptomLogLimits",
    "SymptomMarker",
    "build_symptom_entry",
    "effective_reach_days",
    "retention_floor",
    "severity_series",
]
