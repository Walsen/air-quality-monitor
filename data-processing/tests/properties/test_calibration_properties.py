"""Calibration property tests (tasks 7.4, 7.5, 7.6).

Feature: ingestion-and-serving-service
- Property 10: calibration humidity monotonicity (Requirement 8.8)
- Property 11: corrected values are finite and non-negative
  (Requirements 8.9, 8.3; Requirement 8.10's retain-both-values rule is asserted where
  the Calibrated_Reading is assembled, since a pure float function has nothing to
  retain)
- Property 12: out-of-domain calibration is flagged, not refused
  (Requirements 8.7, 8.9)

Property 10 generates coefficients with b <= 0 as well as the shipped defaults.
Requirement 8.8's monotonicity holds exactly while b <= 0, and Requirement 8.13 does
not list a positive b among the configuration rejections — so the property is asserted
over the coefficient family for which the requirement is satisfiable, and the
inversion a positive b causes is pinned by a unit test instead.
"""

from __future__ import annotations

import math

from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.domain.calibration import (
    DEFAULT_RH_LINEAR_COEFFICIENTS,
    CalibrationDomain,
    CalibrationRegistry,
    IdentityStrategy,
    RhLinearCoefficients,
    RhLinearStrategy,
)

# Finite, realistic magnitudes: wide enough to catch a sign or clamp error, bounded so
# the assertions test the formula rather than floating-point overflow.
_reported = st.floats(min_value=0.0, max_value=5_000.0, allow_nan=False)
_rh = st.floats(min_value=0.0, max_value=100.0, allow_nan=False)
_monotonic_coefficients = st.builds(
    RhLinearCoefficients,
    a=st.floats(min_value=0.0, max_value=5.0, allow_nan=False),
    b=st.floats(min_value=-5.0, max_value=0.0, allow_nan=False),  # Req 8.8 needs b<=0
    c=st.floats(min_value=-50.0, max_value=50.0, allow_nan=False),
)


@given(
    reported=_reported,
    rh_pair=st.tuples(_rh, _rh),
    coefficients=_monotonic_coefficients,
)
def test_property_10_calibration_humidity_monotonicity(
    reported: float, rh_pair: tuple[float, float], coefficients: RhLinearCoefficients
) -> None:
    """Feature: ingestion-and-serving-service, Property 10."""
    drier_rh, wetter_rh = sorted(rh_pair)
    strategy = RhLinearStrategy(coefficients=coefficients)

    drier = strategy.correct(reported=reported, rh=drier_rh)
    wetter = strategy.correct(reported=reported, rh=wetter_rh)

    # Req 8.8: the LOWER RH must correct to a value at least as high, so a more humid
    # reading is corrected downward at least as much
    assert drier >= wetter


@given(reported=_reported, rh_pair=st.tuples(_rh, _rh))
def test_property_10_holds_for_the_shipped_defaults(
    reported: float, rh_pair: tuple[float, float]
) -> None:
    """Feature: ingestion-and-serving-service, Property 10 (default coefficients)."""
    drier_rh, wetter_rh = sorted(rh_pair)
    strategy = RhLinearStrategy(coefficients=DEFAULT_RH_LINEAR_COEFFICIENTS)
    assert strategy.correct(reported=reported, rh=drier_rh) >= strategy.correct(
        reported=reported, rh=wetter_rh
    )


@given(
    reported=st.floats(min_value=-1_000.0, max_value=100_000.0, allow_nan=False),
    rh=_rh,
    coefficients=st.builds(
        RhLinearCoefficients,
        a=st.floats(min_value=-5.0, max_value=5.0, allow_nan=False),
        b=st.floats(min_value=-5.0, max_value=5.0, allow_nan=False),
        c=st.floats(min_value=-500.0, max_value=500.0, allow_nan=False),
    ),
)
def test_property_11_corrected_values_are_finite_and_non_negative(
    reported: float, rh: float, coefficients: RhLinearCoefficients
) -> None:
    """Feature: ingestion-and-serving-service, Property 11."""
    corrected = RhLinearStrategy(coefficients=coefficients).correct(
        reported=reported, rh=rh
    )

    # Req 8.9 holds for ANY coefficients, including the sign combinations and negative
    # reported values that drive the linear form well below zero
    assert math.isfinite(corrected)
    assert corrected >= 0.0


@given(reported=st.floats(min_value=-1_000.0, max_value=100_000.0, allow_nan=False))
def test_property_11_holds_for_every_registered_strategy(reported: float) -> None:
    """Feature: ingestion-and-serving-service, Property 11 (all strategies).

    Requirement 8.9 says "FOR ALL Readings", not "for rh_linear": the non-negative
    bound is a property of a corrected concentration, so every strategy owes it.
    """
    registry = CalibrationRegistry.with_defaults()
    for name in registry.names():
        strategy = registry.resolve(name)
        # rh_linear refuses an absent RH by design (Req 8.6), so give every strategy
        # an RH it can accept
        corrected = strategy.correct(reported=reported, rh=50.0)
        assert math.isfinite(corrected)
        assert corrected >= 0.0, f"{name} produced a negative corrected value"


@given(
    reported=st.floats(min_value=-500.0, max_value=10_000.0, allow_nan=False),
    rh=st.floats(min_value=-50.0, max_value=200.0, allow_nan=False),
)
def test_property_12_out_of_domain_calibration_is_flagged_not_refused(
    reported: float, rh: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 12."""
    strategy = RhLinearStrategy()
    domain = strategy.domain

    # Req 8.7: the strategy is STILL APPLIED out of domain — a refused reading serves
    # nothing, while an extrapolated one carries a value and a caveat
    corrected = strategy.correct(reported=reported, rh=rh)
    assert math.isfinite(corrected)
    assert corrected >= 0.0

    breaches = domain.breaches(reported=reported, rh=rh)
    outside = not (
        domain.reported_min <= reported <= domain.reported_max
        and domain.rh_min <= rh <= domain.rh_max
    )
    # the breach report and the bounds must agree, or a reading could be corrected
    # out of domain and reported as calibrated
    assert bool(breaches) == outside

    # Req 8.7 wants the warning to name the input and its bound, so every breach
    # description carries both numbers
    for breach in breaches:
        assert any(character.isdigit() for character in breach)


@given(
    reported=st.floats(min_value=0.0, max_value=250.0, allow_nan=False),
    rh=st.floats(min_value=20.0, max_value=90.0, allow_nan=False),
)
def test_property_12_in_domain_input_is_never_flagged(
    reported: float, rh: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 12 (no false positives).

    A false extrapolation flag would erode the flag's meaning, so an input inside the
    declared domain must never produce one.
    """
    domain = RhLinearStrategy().domain
    assert domain.contains(reported=reported, rh=rh)
    assert domain.breaches(reported=reported, rh=rh) == ()


@given(reported=_reported, rh=st.one_of(st.none(), _rh))
def test_property_12_identity_is_never_extrapolating(
    reported: float, rh: float | None
) -> None:
    """Feature: ingestion-and-serving-service, Property 12 (identity).

    Applying no humidity correction cannot be an extrapolation of one, so identity
    never reports a breach — flagging one would claim a correction never made.
    """
    assert IdentityStrategy().domain.breaches(reported=reported, rh=rh) == ()


@given(
    reported_bounds=st.tuples(
        st.floats(min_value=-100.0, max_value=100.0, allow_nan=False),
        st.floats(min_value=101.0, max_value=1_000.0, allow_nan=False),
    ),
    rh_bounds=st.tuples(
        st.floats(min_value=0.0, max_value=40.0, allow_nan=False),
        st.floats(min_value=41.0, max_value=100.0, allow_nan=False),
    ),
    reported=st.floats(min_value=-500.0, max_value=2_000.0, allow_nan=False),
    rh=st.floats(min_value=-10.0, max_value=150.0, allow_nan=False),
)
def test_property_12_breach_detection_matches_any_configured_domain(
    reported_bounds: tuple[float, float],
    rh_bounds: tuple[float, float],
    reported: float,
    rh: float,
) -> None:
    """Feature: ingestion-and-serving-service, Property 12 (configured domains).

    The domain is configuration (Requirement 8.3), so breach detection must track
    whatever bounds it is given rather than the shipped ones.
    """
    domain = CalibrationDomain(
        reported_min=reported_bounds[0],
        reported_max=reported_bounds[1],
        rh_min=rh_bounds[0],
        rh_max=rh_bounds[1],
    )
    expected_outside = not (
        domain.reported_min <= reported <= domain.reported_max
        and domain.rh_min <= rh <= domain.rh_max
    )
    assert bool(domain.breaches(reported=reported, rh=rh)) == expected_outside
