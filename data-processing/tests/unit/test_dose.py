"""Unit tests for Inhaled_Dose estimation (task 20.1).

Requirements 23.1 through 23.8.

TWO REQUIREMENTS THAT LOOK CONTRADICTORY AND ARE NOT. Requirement 23.5 says the dose is "zero
when the duration is zero"; Requirement 23.6 says a duration must be "greater than 0". They have
different SUBJECTS — 23.5 constrains the FORMULA, which must be total over its mathematical
domain, and 23.6 constrains what a PROFILE WRITE may store. So the calculator accepts a zero
duration and returns zero, while the profile write rejects one. The same shape as Requirement
3.3 versus 3.6 (the parser rejects, the service routes) and 15.9 versus 17.9 (a site's position
is logged, a user's is not).

THE UNITS TRAP, which Requirement 23.1 settles by naming it in the formula itself
(`corrected_concentration_ug_m3`): NO2 is stored and indexed in ppb, so a dose computed from an
NO2 corrected value without converting first would be wrong by a factor of about 1.9. The
calculator therefore never takes a bare number off a reading — see the refusal tests below.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from aqm_ingestion.domain.dose import (
    DEFAULT_BREATHING_RATES,
    DEFAULT_MAX_DURATION_HOURS,
    DOSE_UNIT,
    ActivityInputs,
    InhaledDose,
    activity_inputs_from,
    compute_inhaled_dose,
    dose_concentration_from,
)
from aqm_ingestion.domain.models import (
    CalibratedReading,
    Confidence,
    DedupKey,
    QualityFlag,
)
from aqm_ingestion.domain.profile import (
    RECOGNIZED_CONSENT_VERSIONS,
    ActivityLevel,
    Condition,
    ProfileLimits,
    SensitivityLevel,
    build_profile,
)

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _profile_fields(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "user_id": "user-1",
        "condition": Condition.ASTHMA,
        "sensitivity_level": SensitivityLevel.STANDARD,
        "consent": {
            "version": next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS))),
            "given_at": _NOW,
        },
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return base | overrides


def _reading(
    *,
    corrected: float = 20.0,
    reported: float = 99.0,
    units: str = "ug.m-3",
    species: str = "PM25",
    confidence: Confidence = Confidence.HIGH,
) -> CalibratedReading:

    return CalibratedReading(
        key=DedupKey(site_code="SITE1", species=species, interval_start=_NOW, duration="PT1H"),
        reported_value=reported,
        corrected_value=corrected,
        units=units,
        quality_flag=QualityFlag.CALIBRATED,
        confidence=confidence,
        calibration_strategy="rh_linear",
        breakpoint_table="epa-2024-05-06",
        ratification_status="R",
        ingested_at=_NOW,
        archive_id="archive-1",
    )


# --- Req 23.2: the rate map, transcribed from the requirement -----------

def test_the_breathing_rates_are_the_requirements_defaults() -> None:
    assert DEFAULT_BREATHING_RATES[ActivityLevel.REST] == 0.5
    assert DEFAULT_BREATHING_RATES[ActivityLevel.LIGHT] == 1.0
    assert DEFAULT_BREATHING_RATES[ActivityLevel.MODERATE] == 2.0
    assert DEFAULT_BREATHING_RATES[ActivityLevel.VIGOROUS] == 3.2


def test_every_activity_level_has_a_rate() -> None:
    assert set(DEFAULT_BREATHING_RATES) == set(ActivityLevel)


def test_a_harder_activity_breathes_more() -> None:
    # Pins the DIRECTION, so a transposed map holding the same four numbers fails.
    rates = [DEFAULT_BREATHING_RATES[level] for level in ActivityLevel]
    assert rates == sorted(rates)


def test_the_rates_are_configurable() -> None:
    # Req 23.2 says "configurable"; a hardcoded lookup would pass every test above.
    dose = compute_inhaled_dose(
        concentration_ug_m3=10.0,
        concentration_confidence=Confidence.HIGH,
        activity=ActivityInputs(level=ActivityLevel.REST, duration_hours=1.0),
        rates={**DEFAULT_BREATHING_RATES, ActivityLevel.REST: 9.0},
    )
    assert dose is not None
    assert dose.breathing_rate_m3_per_h == 9.0


def test_the_default_maximum_duration_is_24_hours() -> None:
    assert DEFAULT_MAX_DURATION_HOURS == 24.0


# --- Req 23.1: the formula ---------------------------------------------

def test_the_dose_is_concentration_times_rate_times_duration() -> None:
    dose = compute_inhaled_dose(
        concentration_ug_m3=20.0,
        concentration_confidence=Confidence.HIGH,
        activity=ActivityInputs(level=ActivityLevel.MODERATE, duration_hours=1.5),
        rates=DEFAULT_BREATHING_RATES,
    )
    assert dose is not None
    assert dose.micrograms == pytest.approx(20.0 * 2.0 * 1.5)


def test_the_dose_reports_its_unit() -> None:
    # Req 23.1 requires the unit be REPORTED, and the formula's name fixes it as micrograms.
    assert DOSE_UNIT == "ug"
    dose = compute_inhaled_dose(
        concentration_ug_m3=20.0,
        concentration_confidence=Confidence.HIGH,
        activity=ActivityInputs(level=ActivityLevel.REST, duration_hours=1.0),
        rates=DEFAULT_BREATHING_RATES,
    )
    assert dose is not None
    assert dose.unit == "ug"


def test_a_zero_duration_yields_a_zero_dose() -> None:
    # Req 23.5. The calculator is total over the formula's domain; Req 23.6's ">0" constrains
    # the profile write instead — see the module docstring.
    dose = compute_inhaled_dose(
        concentration_ug_m3=20.0,
        concentration_confidence=Confidence.HIGH,
        activity=ActivityInputs(level=ActivityLevel.VIGOROUS, duration_hours=0.0),
        rates=DEFAULT_BREATHING_RATES,
    )
    assert dose is not None
    assert dose.micrograms == 0.0


def test_a_zero_concentration_yields_a_zero_dose() -> None:
    dose = compute_inhaled_dose(
        concentration_ug_m3=0.0,
        concentration_confidence=Confidence.HIGH,
        activity=ActivityInputs(level=ActivityLevel.VIGOROUS, duration_hours=3.0),
        rates=DEFAULT_BREATHING_RATES,
    )
    assert dose is not None
    assert dose.micrograms == 0.0


def test_a_negative_concentration_is_refused() -> None:
    # Calibration clamps at 0 (Req 8.9), so a negative here is a caller bug, and a negative
    # dose would be meaningless (§5).
    with pytest.raises(ValueError, match="negative"):
        compute_inhaled_dose(
            concentration_ug_m3=-1.0,
            concentration_confidence=Confidence.HIGH,
            activity=ActivityInputs(level=ActivityLevel.REST, duration_hours=1.0),
            rates=DEFAULT_BREATHING_RATES,
        )


def test_a_negative_duration_is_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        ActivityInputs(level=ActivityLevel.REST, duration_hours=-1.0)


# --- Req 23.3: absent activity inputs mean null, not a guess -----------

def test_absent_activity_inputs_yield_no_dose() -> None:
    assert (
        compute_inhaled_dose(
            concentration_ug_m3=20.0,
            concentration_confidence=Confidence.HIGH,
            activity=None,
            rates=DEFAULT_BREATHING_RATES,
        )
        is None
    )


def test_no_activity_level_is_assumed() -> None:
    # Req 23.3 states the reason: an assumed dose is not a measured one. Returning None rather
    # than a dose computed at some default rate is what makes that structural — there is no
    # object to carry a fabricated number.
    assert (
        compute_inhaled_dose(
            concentration_ug_m3=1000.0,
            concentration_confidence=Confidence.HIGH,
            activity=None,
            rates=DEFAULT_BREATHING_RATES,
        )
        is None
    )


def test_a_profile_with_a_level_but_no_duration_supplies_no_activity() -> None:
    # Req 23.1 requires BOTH inputs; half of them is not enough, and defaulting the missing
    # half would be exactly the assumption Req 23.3 forbids.
    profile = build_profile(
        _profile_fields(activity_level=ActivityLevel.MODERATE), limits=ProfileLimits()
    )
    assert activity_inputs_from(profile) is None


def test_a_profile_with_a_duration_but_no_level_supplies_no_activity() -> None:
    profile = build_profile(
        _profile_fields(activity_duration_hours=2.0), limits=ProfileLimits()
    )
    assert activity_inputs_from(profile) is None


def test_a_profile_with_both_inputs_supplies_them() -> None:
    profile = build_profile(
        _profile_fields(
            activity_level=ActivityLevel.VIGOROUS, activity_duration_hours=0.75
        ),
        limits=ProfileLimits(),
    )
    activity = activity_inputs_from(profile)
    assert activity is not None
    assert activity.level is ActivityLevel.VIGOROUS
    assert activity.duration_hours == 0.75


# --- Req 23.4: from the corrected value, never the reported or an index -

def test_the_dose_uses_the_corrected_value_not_the_reported_one() -> None:
    # The two sit side by side on CalibratedReading (Req 8.10 keeps reported for audit), so
    # picking the wrong one is a one-word mistake. The reported value here is deliberately far
    # from the corrected one so the arithmetic tells them apart.
    concentration = dose_concentration_from(_reading(corrected=20.0, reported=99.0))
    assert concentration == 20.0


def test_the_dose_ignores_the_sub_index() -> None:
    # Req 23.4 names the Sub_Index explicitly. Structural answer: the calculator has no
    # parameter a Sub_Index could arrive through, so there is nothing to ignore at runtime.
    import inspect

    assert set(inspect.signature(compute_inhaled_dose).parameters) == {
        "concentration_ug_m3",
        "concentration_confidence",
        "activity",
        "rates",
    }


def test_the_dose_names_the_concentration_and_rate_it_used() -> None:
    # Req 23.4's Basis requirement: both inputs are readable off the result, so the response
    # assembler cannot report a dose without being able to explain it.
    dose = compute_inhaled_dose(
        concentration_ug_m3=12.5,
        concentration_confidence=Confidence.HIGH,
        activity=ActivityInputs(level=ActivityLevel.LIGHT, duration_hours=2.0),
        rates=DEFAULT_BREATHING_RATES,
    )
    assert dose is not None
    assert dose.concentration_ug_m3 == 12.5
    assert dose.breathing_rate_m3_per_h == 1.0
    assert dose.duration_hours == 2.0


# --- The units guard ---------------------------------------------------

def test_a_ppb_reading_cannot_supply_a_dose_concentration() -> None:
    # NO2 is stored and indexed in ppb. Req 23.1's formula is in µg/m³, so using a ppb value
    # directly would be wrong by roughly the 1.88 conversion factor — a plausible-looking
    # number, which is what makes it dangerous.
    with pytest.raises(ValueError, match="ppb"):
        dose_concentration_from(_reading(species="NO2", units="ppb", corrected=40.0))


def test_the_refusal_names_the_conversion_that_is_needed() -> None:
    with pytest.raises(ValueError, match=r"ug\.m-3|convert"):
        dose_concentration_from(_reading(species="NO2", units="ppb"))


def test_a_mass_concentration_reading_supplies_its_corrected_value() -> None:
    assert dose_concentration_from(_reading(units="ug.m-3", corrected=33.0)) == 33.0


def test_an_unknown_unit_is_refused_rather_than_assumed() -> None:
    with pytest.raises(ValueError):
        dose_concentration_from(_reading(units="mg/l"))


# --- Req 23.7: Confidence is capped at the concentration's -------------

def test_the_dose_confidence_matches_the_concentrations() -> None:
    for confidence in Confidence:
        dose = compute_inhaled_dose(
            concentration_ug_m3=10.0,
            concentration_confidence=confidence,
            activity=ActivityInputs(level=ActivityLevel.REST, duration_hours=1.0),
            rates=DEFAULT_BREATHING_RATES,
        )
        assert dose is not None
        assert dose.confidence is confidence


def test_the_dose_confidence_is_never_better_than_the_concentrations() -> None:
    # Req 23.7 says CAP. A dose is a product of the concentration, so it cannot be more
    # trustworthy than its input — this is the assertion that fails if a default HIGH creeps in.
    from aqm_ingestion.domain.models import confidence_rank

    dose = compute_inhaled_dose(
        concentration_ug_m3=10.0,
        concentration_confidence=Confidence.LOW,
        activity=ActivityInputs(level=ActivityLevel.VIGOROUS, duration_hours=5.0),
        rates=DEFAULT_BREATHING_RATES,
    )
    assert dose is not None
    assert confidence_rank(dose.confidence) <= confidence_rank(Confidence.LOW)


# --- Req 23.8: an exposure quantity, with no clinical framing ----------

def test_the_dose_carries_no_clinical_field() -> None:
    # Structural reading of Req 23.8: there is no band, no severity, no advice field, so no
    # clinical interpretation can be attached to a dose without changing this type.
    assert set(InhaledDose.__dataclass_fields__) == {
        "micrograms",
        "unit",
        "concentration_ug_m3",
        "breathing_rate_m3_per_h",
        "duration_hours",
        "confidence",
    }


def test_no_dose_field_names_a_band_or_severity() -> None:
    forbidden = ("band", "severity", "advice", "risk", "diagnos", "safe")
    for field in InhaledDose.__dataclass_fields__:
        assert not any(word in field.lower() for word in forbidden)


# --- Req 23.6: the duration range, rejected at the profile write -------

def test_a_zero_duration_is_rejected_at_the_profile_write() -> None:
    # Req 23.6 says "greater than 0". The task-17 model allowed 0 (ge=0.0), which would have
    # stored an activity that never happened.
    with pytest.raises(ValidationError):
        build_profile(
            _profile_fields(
                activity_level=ActivityLevel.REST, activity_duration_hours=0.0
            ),
            limits=ProfileLimits(),
        )


def test_a_duration_above_the_maximum_is_rejected() -> None:
    # The task-17 model had NO upper bound at all.
    with pytest.raises(ValidationError):
        build_profile(
            _profile_fields(
                activity_level=ActivityLevel.REST, activity_duration_hours=24.5
            ),
            limits=ProfileLimits(),
        )


def test_the_maximum_duration_is_accepted() -> None:
    # "up to the configured maximum" — inclusive, so 24 exactly is lawful.
    profile = build_profile(
        _profile_fields(
            activity_level=ActivityLevel.REST, activity_duration_hours=24.0
        ),
        limits=ProfileLimits(),
    )
    assert profile.activity_duration_hours == 24.0


def test_the_maximum_duration_is_configurable() -> None:
    profile = build_profile(
        _profile_fields(
            activity_level=ActivityLevel.REST, activity_duration_hours=40.0
        ),
        limits=ProfileLimits(max_activity_duration_hours=48.0),
    )
    assert profile.activity_duration_hours == 40.0


def test_the_duration_rejection_names_the_range() -> None:
    # §5: a documented failure names the offending parameter and the permitted range. The VALUE
    # is still withheld, since Req 17.9 covers profile fields in error messages.
    with pytest.raises(ValidationError) as caught:
        build_profile(
            _profile_fields(
                activity_level=ActivityLevel.REST, activity_duration_hours=99.0
            ),
            limits=ProfileLimits(),
        )
    message = str(caught.value)
    assert "24" in message
    assert "activity_duration_hours" in message
    assert "99" not in message
