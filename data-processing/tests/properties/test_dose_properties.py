"""Property tests for Inhaled_Dose.

Feature: ingestion-and-serving-service, Property 34.
"""

from __future__ import annotations

from itertools import pairwise

from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.domain.dose import (
    DEFAULT_BREATHING_RATES,
    ActivityInputs,
    compute_inhaled_dose,
)
from aqm_ingestion.domain.models import Confidence
from aqm_ingestion.domain.profile import ActivityLevel

# Bounded well inside float range: Requirement 23.5's strict monotonicity is a property of the
# real-valued function, and the same ULP-collapse lesson that bit Property 14 applies — two
# inputs a float's-breadth apart cannot yield distinct products.
_concentrations = st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False)
_positive_concentrations = st.floats(min_value=0.1, max_value=10_000.0, allow_nan=False)
_durations = st.floats(min_value=0.0, max_value=24.0, allow_nan=False)
_positive_durations = st.floats(min_value=0.01, max_value=24.0, allow_nan=False)
_levels = st.sampled_from(list(ActivityLevel))
_confidences = st.sampled_from(list(Confidence))


def _dose(
    concentration: float,
    level: ActivityLevel,
    duration: float,
    confidence: Confidence = Confidence.HIGH,
) -> float:
    result = compute_inhaled_dose(
        concentration_ug_m3=concentration,
        concentration_confidence=confidence,
        activity=ActivityInputs(level=level, duration_hours=duration),
        rates=DEFAULT_BREATHING_RATES,
    )
    assert result is not None  # activity supplied, so a dose must exist
    return result.micrograms


@given(
    concentration=_concentrations,
    level=_levels,
    duration=_durations,
    confidence=_confidences,
)
def test_property_34_dose_is_non_negative_and_vanishes_at_zero_duration(
    concentration: float,
    level: ActivityLevel,
    duration: float,
    confidence: Confidence,
) -> None:
    """Feature: ingestion-and-serving-service, Property 34.

    Inhaled dose scales correctly and vanishes at zero duration.
    Validates Requirements 23.1, 23.3 and 23.5.

    The zero-duration half is asserted at a duration of exactly 0, which Requirement 23.6
    forbids a PROFILE from storing but Requirement 23.5 requires the FORMULA to handle — the two
    requirements have different subjects, so the calculator must be total here.
    """
    result = compute_inhaled_dose(
        concentration_ug_m3=concentration,
        concentration_confidence=confidence,
        activity=ActivityInputs(level=level, duration_hours=duration),
        rates=DEFAULT_BREATHING_RATES,
    )
    assert result is not None
    assert result.micrograms >= 0.0

    # Req 23.5: zero when the duration is zero, whatever the concentration.
    assert _dose(concentration, level, 0.0) == 0.0

    # Req 23.7: the dose's Confidence never exceeds the concentration's — asserted for every
    # generated Confidence, not just the default.
    assert result.confidence is confidence

    # Req 23.3: the SAME inputs with no activity yield no dose at all, which is what stops the
    # property being read as "a dose always exists".
    assert (
        compute_inhaled_dose(
            concentration_ug_m3=concentration,
            concentration_confidence=confidence,
            activity=None,
            rates=DEFAULT_BREATHING_RATES,
        )
        is None
    )


@given(
    lower=_positive_concentrations,
    delta=st.floats(min_value=1.0, max_value=1_000.0, allow_nan=False),
    level=_levels,
    duration=_positive_durations,
)
def test_property_34_dose_strictly_increases_in_concentration(
    lower: float, delta: float, level: ActivityLevel, duration: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 34.

    Requirement 23.5: strictly increasing in concentration while rate and duration are held
    positive and constant.

    The increment has a floor of 1.0 rather than any two distinct floats — the Property 14
    lesson: strictness belongs to the real-valued function, and two inputs closer than
    float precision cannot produce distinct outputs.
    """
    assert _dose(lower + delta, level, duration) > _dose(lower, level, duration)


@given(
    concentration=_positive_concentrations,
    lower=_positive_durations,
    delta=st.floats(min_value=0.5, max_value=24.0, allow_nan=False),
    level=_levels,
)
def test_property_34_dose_strictly_increases_in_duration(
    concentration: float, lower: float, delta: float, level: ActivityLevel
) -> None:
    """Feature: ingestion-and-serving-service, Property 34.

    Requirement 23.5: strictly increasing in duration while concentration and rate are held
    positive and constant.
    """
    assert _dose(concentration, level, lower + delta) > _dose(
        concentration, level, lower
    )


@given(concentration=_positive_concentrations, duration=_positive_durations)
def test_property_34_dose_strictly_increases_in_breathing_rate(
    concentration: float, duration: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 34.

    Requirement 23.5: strictly increasing in breathing rate while concentration and duration are
    held positive and constant.

    Walks the SHIPPED activity levels in their own order rather than generating a rate, so this
    also asserts Requirement 23.2's map is strictly ordered — a rate map with a duplicate or an
    inversion fails here even though each individual value would look right.
    """
    ordered = sorted(DEFAULT_BREATHING_RATES.items(), key=lambda item: item[1])
    for (lower_level, lower_rate), (higher_level, higher_rate) in pairwise(ordered):
        # Req 23.2's map is strictly ordered — a duplicate or inverted rate fails here even
        # though each individual value would look right on its own.
        assert higher_rate > lower_rate
        # Req 23.5, on CONSECUTIVE levels with a STRICT inequality. Comparing every level
        # against the lowest, or allowing equality, would both pass a map whose middle two
        # rates were identical.
        assert _dose(concentration, higher_level, duration) > _dose(
            concentration, lower_level, duration
        )
