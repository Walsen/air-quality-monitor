"""Inhaled_Dose estimation: what the user actually breathed in.

Requirement 23. Pure domain logic — no clock, no adapters.

THE UNITS GUARD IS THE POINT OF THIS MODULE'S SHAPE. Requirement 23.1 writes the formula as
``dose_ug = corrected_concentration_ug_m3 * breathing_rate_m3_per_h * duration_h``, naming the
unit inside the variable. NO2 is stored and indexed in ppb, so feeding an NO2 corrected value
straight into that formula gives a number wrong by roughly the 1.88 conversion factor — and
crucially, a number that still looks plausible. So ``compute_inhaled_dose`` takes a
concentration already in µg/m³, and ``dose_concentration_from`` is the only sanctioned way
to get one off a reading: it REFUSES a ppb reading rather than converting silently, because
the conversion needs site temperature and pressure (Requirement 9) that this module has no
business guessing at.

REQUIREMENTS 23.5 AND 23.6 LOOK CONTRADICTORY AND ARE NOT — they have different subjects. 23.5
constrains the FORMULA, which is total over its mathematical domain and must return zero for a
zero duration. 23.6 constrains what a PROFILE WRITE may store, where a duration must exceed
zero. So a zero duration is computable but not storable, and each rule lives with its own
subject.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from aqm_ingestion.domain.models import CalibratedReading, Confidence
from aqm_ingestion.domain.profile import ActivityLevel, UserProfile

DOSE_UNIT = "ug"
"""Requirement 23.1's dose unit, fixed by the formula's own name (``dose_ug``)."""

MASS_CONCENTRATION_UNIT = "ug.m-3"
"""The only unit a dose may be computed from.

Spelled as the CONTRACT spells it — Requirement 1.8's Units field and the shipped
Breakpoint_Table both write `ug.m-3`, not `ug/m3`.
"""

DEFAULT_MAX_DURATION_HOURS = 24.0
"""Requirement 23.6's default maximum activity duration."""

DEFAULT_BREATHING_RATES: Mapping[ActivityLevel, float] = {
    ActivityLevel.REST: 0.5,
    ActivityLevel.LIGHT: 1.0,
    ActivityLevel.MODERATE: 2.0,
    ActivityLevel.VIGOROUS: 3.2,
}
"""Requirement 23.2's default rates in m³/h. Configurable — passed in, never read from here."""


@dataclass(frozen=True, slots=True)
class ActivityInputs:
    """The activity level and duration a dose needs (Requirement 23.1).

    BOTH are required, which is what makes Requirement 23.3 structural: there is no way to
    represent half the inputs, so a missing duration cannot be quietly defaulted.
    """

    level: ActivityLevel
    duration_hours: float

    def __post_init__(self) -> None:
        """Refuse a negative duration, which no elapsed time can be."""
        if self.duration_hours < 0:
            raise ValueError(
                f"activity duration cannot be negative: {self.duration_hours}"
            )


@dataclass(frozen=True, slots=True)
class InhaledDose:
    """An exposure quantity, and the two inputs that explain it.

    Carries NO band, severity, advice or risk field. Requirement 23.8 frames the dose as an
    exposure quantity with no clinical interpretation, and the absence of anywhere to put one is
    what enforces that — a test asserts the field set so a later addition fails loudly.
    """

    micrograms: float
    unit: str
    concentration_ug_m3: float
    breathing_rate_m3_per_h: float
    duration_hours: float
    confidence: Confidence


def compute_inhaled_dose(
    concentration_ug_m3: float,
    concentration_confidence: Confidence,
    activity: ActivityInputs | None,
    rates: Mapping[ActivityLevel, float],
) -> InhaledDose | None:
    """Compute the Inhaled_Dose, or None when the activity inputs are absent.

    Requirements 23.1, 23.3, 23.4, 23.7.

    Returns None rather than a dose at some assumed rate: Requirement 23.3 gives the reason —
    an assumed dose is not a measured one — and returning None means no object exists that could
    carry a fabricated number.

    Raises:
        ValueError: for a negative concentration. Calibration clamps at 0 (Requirement 8.9), so
            a negative value here is a caller bug, and a negative dose is meaningless (§5).
    """
    if activity is None:
        return None
    if concentration_ug_m3 < 0:
        raise ValueError(
            f"cannot compute a dose from a negative concentration: {concentration_ug_m3}"
        )

    rate = rates[activity.level]
    return InhaledDose(
        micrograms=concentration_ug_m3 * rate * activity.duration_hours,
        unit=DOSE_UNIT,
        concentration_ug_m3=concentration_ug_m3,
        breathing_rate_m3_per_h=rate,
        duration_hours=activity.duration_hours,
        # Requirement 23.7: a product cannot be more trustworthy than its input, so the dose
        # simply takes the concentration's Confidence — already the cap.
        confidence=concentration_confidence,
    )


def dose_concentration_from(reading: CalibratedReading) -> float:
    """Take the corrected concentration a dose may be computed from.

    Requirement 23.4 requires the CORRECTED value, never the reported one — the two sit side by
    side on the reading (Requirement 8.10 keeps reported for audit), so this function exists
    partly to make that choice happen in exactly one place.

    Raises:
        ValueError: if the reading is not in µg/m³. A ppb value would need Requirement 9's
            temperature- and pressure-aware conversion, which depends on site conditions this
            module does not have — so it refuses rather than producing a plausible wrong number.
    """
    if reading.units != MASS_CONCENTRATION_UNIT:
        raise ValueError(
            f"cannot compute a dose from a reading in {reading.units!r}: Requirement 23.1's "
            f"formula needs {MASS_CONCENTRATION_UNIT}, so convert first (a ppb value needs "
            "site temperature and pressure)"
        )
    return reading.corrected_value


def activity_inputs_from(profile: UserProfile | None) -> ActivityInputs | None:
    """Read a profile's activity inputs, or None when either is missing.

    Requirement 23.1 needs BOTH, and Requirement 23.3 forbids assuming either, so half the
    inputs yields none of them rather than a defaulted pair.
    """
    if profile is None:
        return None
    if profile.activity_level is None or profile.activity_duration_hours is None:
        return None
    return ActivityInputs(
        level=profile.activity_level, duration_hours=profile.activity_duration_hours
    )


__all__ = [
    "DEFAULT_BREATHING_RATES",
    "DEFAULT_MAX_DURATION_HOURS",
    "DOSE_UNIT",
    "MASS_CONCENTRATION_UNIT",
    "ActivityInputs",
    "InhaledDose",
    "activity_inputs_from",
    "compute_inhaled_dose",
    "dose_concentration_from",
]
