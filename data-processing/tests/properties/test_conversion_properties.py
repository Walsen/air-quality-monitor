"""Unit conversion property tests (tasks 8.2, 8.3, 8.4).

Feature: ingestion-and-serving-service
- Property 13: unit conversion round-trip (Requirement 9.4)
- Property 14: conversion factor responds correctly to temperature and pressure
  (Requirement 9.5)
- Property 15: conversion factor is pinned at reference conditions
  (Requirements 9.6, 9.2)

Ranges are physically plausible rather than maximal: 180-330 K spans anywhere a sensor
is deployed, and 30-110 kPa spans sea level to well above the ~2,560 m default geography.
Generating 1e-300 K would only test floating-point underflow, not the requirement.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.domain.conversion import (
    conversion_factor,
    ppb_from_ug_m3,
    ug_m3_from_ppb,
)

_temperature = st.floats(min_value=180.0, max_value=330.0, allow_nan=False)
_pressure = st.floats(min_value=30_000.0, max_value=110_000.0, allow_nan=False)
_ppb = st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False)


@given(mixing_ratio=_ppb, temperature_k=_temperature, pressure_pa=_pressure)
def test_property_13_unit_conversion_round_trip(
    mixing_ratio: float, temperature_k: float, pressure_pa: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 13."""
    mass = ug_m3_from_ppb(
        mixing_ratio, temperature_k=temperature_k, pressure_pa=pressure_pa
    )
    recovered = ppb_from_ug_m3(
        mass, temperature_k=temperature_k, pressure_pa=pressure_pa
    )

    # Req 9.4: within a relative tolerance of 1e-9 under IDENTICAL conditions
    assert recovered == pytest.approx(mixing_ratio, rel=1e-9, abs=1e-12)


@given(mass=_ppb, temperature_k=_temperature, pressure_pa=_pressure)
def test_property_13_round_trip_holds_from_the_mass_side_too(
    mass: float, temperature_k: float, pressure_pa: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 13 (reverse direction).

    Requirement 9.4 names the ppb-first direction, but an inverse that only round-trips
    one way would still be wrong, so the other direction is checked as well.
    """
    ppb = ppb_from_ug_m3(mass, temperature_k=temperature_k, pressure_pa=pressure_pa)
    recovered = ug_m3_from_ppb(
        ppb, temperature_k=temperature_k, pressure_pa=pressure_pa
    )
    assert recovered == pytest.approx(mass, rel=1e-9, abs=1e-12)


# A relative separation comfortably above float64's ~2.2e-16 resolution. Requirement
# 9.5's "strictly increasing" is a statement about the real-valued function; two inputs
# one ULP apart CANNOT produce distinct float64 outputs, because the multiplication and
# division that compute the factor round to the same value. So strictness is asserted
# where the inputs differ enough for the difference to survive the arithmetic, and
# monotonicity (non-decreasing) is asserted everywhere.
_SIGNIFICANT_SEPARATION = 1e-12


def _separated(lower: float, higher: float) -> bool:
    """Whether two inputs differ by more than floating-point noise."""
    return (higher - lower) / higher > _SIGNIFICANT_SEPARATION


@given(
    temperature_k=_temperature,
    pressures=st.tuples(_pressure, _pressure),
)
def test_property_14_factor_is_strictly_increasing_in_pressure(
    temperature_k: float, pressures: tuple[float, float]
) -> None:
    """Feature: ingestion-and-serving-service, Property 14 (pressure)."""
    lower, higher = sorted(pressures)
    factor_low = conversion_factor(temperature_k=temperature_k, pressure_pa=lower)
    factor_high = conversion_factor(temperature_k=temperature_k, pressure_pa=higher)

    # never decreasing, for any pair at all
    assert factor_high >= factor_low
    if lower == higher:
        assert factor_low == factor_high
    elif _separated(lower, higher):
        # a real pressure difference really does move the factor up
        assert factor_high > factor_low


@given(
    temperatures=st.tuples(_temperature, _temperature),
    pressure_pa=_pressure,
)
def test_property_14_factor_is_strictly_decreasing_in_temperature(
    temperatures: tuple[float, float], pressure_pa: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 14 (temperature)."""
    cooler, warmer = sorted(temperatures)
    factor_cool = conversion_factor(temperature_k=cooler, pressure_pa=pressure_pa)
    factor_warm = conversion_factor(temperature_k=warmer, pressure_pa=pressure_pa)

    assert factor_cool >= factor_warm
    if cooler == warmer:
        assert factor_cool == factor_warm
    elif _separated(cooler, warmer):
        assert factor_cool > factor_warm


@given(temperature_k=_temperature, pressure_pa=_pressure)
def test_property_14_factor_is_always_positive_and_finite(
    temperature_k: float, pressure_pa: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 14 (well-formedness).

    A factor of zero or infinity would make a sub-index meaningless rather than merely
    inaccurate, so it is worth asserting separately from the ordering.
    """
    factor = conversion_factor(temperature_k=temperature_k, pressure_pa=pressure_pa)
    assert math.isfinite(factor)
    assert factor > 0.0


@given(scale=st.floats(min_value=0.5, max_value=2.0, allow_nan=False))
def test_property_15_factor_scales_linearly_with_pressure(scale: float) -> None:
    """Feature: ingestion-and-serving-service, Property 15 (linearity in pressure).

    Requirement 9.2's relation is linear in pressure, so scaling pressure must scale the
    factor by exactly the same amount. This catches a mis-placed constant that the two
    pinned reference points alone might not.
    """
    base = conversion_factor(temperature_k=288.15, pressure_pa=74_000.0)
    scaled = conversion_factor(temperature_k=288.15, pressure_pa=74_000.0 * scale)
    assert scaled == pytest.approx(base * scale, rel=1e-12)


@given(scale=st.floats(min_value=0.5, max_value=2.0, allow_nan=False))
def test_property_15_factor_scales_inversely_with_temperature(scale: float) -> None:
    """Feature: ingestion-and-serving-service, Property 15 (inverse in temperature)."""
    base = conversion_factor(temperature_k=288.15, pressure_pa=74_000.0)
    scaled = conversion_factor(temperature_k=288.15 * scale, pressure_pa=74_000.0)
    assert scaled == pytest.approx(base / scale, rel=1e-12)


def test_property_15_factor_is_pinned_at_both_reference_conditions() -> None:
    """Feature: ingestion-and-serving-service, Property 15.

    Requirement 9.6's two pinned points, which together fix the altitude sensitivity the
    conversion exists to capture. Example-based deliberately: the requirement names
    exact conditions and exact expected values, so generating inputs would test
    something weaker than what it asks.
    """
    assert conversion_factor(
        temperature_k=298.15, pressure_pa=101_325.0
    ) == pytest.approx(1.8804, rel=1e-4)
    assert conversion_factor(
        temperature_k=288.15, pressure_pa=74_000.0
    ) == pytest.approx(1.4210, rel=1e-4)
