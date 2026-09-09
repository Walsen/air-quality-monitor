"""Unit tests for the `rh_linear` calibration strategy (task 7.2, Requirement 8.3).

`corrected = a * reported + b * RH + c`, clamped below at 0, with defaults a = 0.524,
b = -0.0862, c = 5.75, and a Calibration_Domain of 0-250 µg/m³ and 20-90 percent RH.

The coefficients are CONFIGURATION, not constants: the spec adopts a published
US-wide humidity-compensated correction as a documented starting point rather than a
validated fit for this fleet, so a test pins the defaults and separate tests prove the
formula tracks whatever coefficients it is given.
"""

from __future__ import annotations

import math

import pytest

from aqm_ingestion.domain.calibration import CalibrationRegistry
from aqm_ingestion.domain.calibration.strategies import (
    DEFAULT_RH_LINEAR_COEFFICIENTS,
    RhLinearCoefficients,
    RhLinearStrategy,
)

_A = 0.524
_B = -0.0862
_C = 5.75


def _strategy(**overrides: float) -> RhLinearStrategy:
    coefficients = RhLinearCoefficients(
        **{"a": _A, "b": _B, "c": _C} | overrides
    )
    return RhLinearStrategy(coefficients=coefficients)


# --- Req 8.3 the published defaults --------------------------------------

def test_default_coefficients_are_the_documented_ones() -> None:
    assert DEFAULT_RH_LINEAR_COEFFICIENTS.a == _A
    assert DEFAULT_RH_LINEAR_COEFFICIENTS.b == _B
    assert DEFAULT_RH_LINEAR_COEFFICIENTS.c == _C


def test_strategy_is_registered_under_its_documented_name() -> None:
    assert CalibrationRegistry.with_defaults().resolve("rh_linear").name == "rh_linear"


def test_default_domain_is_the_documented_range() -> None:
    domain = CalibrationRegistry.with_defaults().resolve("rh_linear").domain
    assert domain.reported_min == 0.0
    assert domain.reported_max == 250.0
    assert domain.rh_min == 20.0
    assert domain.rh_max == 90.0


# --- the formula ---------------------------------------------------------

def test_correction_applies_the_linear_form() -> None:
    expected = _A * 100.0 + _B * 50.0 + _C
    assert _strategy().correct(reported=100.0, rh=50.0) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("reported", "rh"), [(0.0, 20.0), (250.0, 90.0), (12.5, 55.0), (1.0, 21.0)]
)
def test_correction_tracks_the_formula_across_the_domain(
    reported: float, rh: float
) -> None:
    expected = max(0.0, _A * reported + _B * rh + _C)
    assert _strategy().correct(reported=reported, rh=rh) == pytest.approx(expected)


def test_coefficients_are_honoured_rather_than_hardcoded() -> None:
    # the whole point of them being configuration
    strategy = _strategy(a=2.0, b=0.0, c=1.0)
    assert strategy.correct(reported=10.0, rh=50.0) == pytest.approx(21.0)


# --- Req 8.3 the clamp ---------------------------------------------------

def test_correction_clamps_below_at_zero() -> None:
    # a small concentration at high RH drives the linear form negative, which is not
    # a physical concentration
    assert _strategy().correct(reported=0.0, rh=90.0) == 0.0


def test_clamp_engages_rather_than_returning_a_negative() -> None:
    raw = _A * 0.0 + _B * 90.0 + _C
    assert raw < 0  # the formula really does go negative here
    assert _strategy().correct(reported=0.0, rh=90.0) == 0.0


def test_a_clamped_result_is_still_finite() -> None:
    assert math.isfinite(_strategy().correct(reported=0.0, rh=100.0))


# --- Req 8.6 no RH -------------------------------------------------------

def test_absent_rh_is_refused_by_this_strategy() -> None:
    # Req 8.6 routes a reading with no RH to the fallback strategy instead; letting
    # rh_linear substitute a zero would fabricate a correction from data it lacks
    with pytest.raises(ValueError, match="requires an RH"):
        _strategy().correct(reported=10.0, rh=None)


# --- Req 8.8 humidity monotonicity, at the unit level --------------------

def test_more_humid_is_corrected_at_least_as_far_down() -> None:
    strategy = _strategy()
    drier = strategy.correct(reported=100.0, rh=30.0)
    wetter = strategy.correct(reported=100.0, rh=80.0)
    assert drier >= wetter


def test_equal_rh_gives_equal_correction() -> None:
    strategy = _strategy()
    assert strategy.correct(reported=100.0, rh=40.0) == strategy.correct(
        reported=100.0, rh=40.0
    )


# --- Req 8.13 coefficient validation ------------------------------------

@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan])
@pytest.mark.parametrize("field", ["a", "b", "c"])
def test_non_finite_coefficient_is_refused(field: str, bad: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        RhLinearCoefficients(**{"a": _A, "b": _B, "c": _C} | {field: bad})


def test_coefficients_are_frozen() -> None:
    coefficients = RhLinearCoefficients(a=_A, b=_B, c=_C)
    with pytest.raises((AttributeError, TypeError)):
        coefficients.a = 1.0  # type: ignore[misc]


def test_a_positive_b_is_documented_as_breaking_monotonicity() -> None:
    # Req 8.8's monotonicity holds only while b <= 0, but Req 8.13 does not list a
    # positive b among the rejections. This test records the consequence explicitly
    # rather than leaving it to be discovered: with b > 0 a MORE humid reading is
    # corrected UPWARD, which Req 8.8 forbids.
    strategy = _strategy(b=0.5)
    drier = strategy.correct(reported=100.0, rh=30.0)
    wetter = strategy.correct(reported=100.0, rh=80.0)
    assert wetter > drier  # exactly the inversion Req 8.8 rules out
    assert RhLinearCoefficients(a=_A, b=0.5, c=_C).is_humidity_monotonic is False
    assert DEFAULT_RH_LINEAR_COEFFICIENTS.is_humidity_monotonic is True
