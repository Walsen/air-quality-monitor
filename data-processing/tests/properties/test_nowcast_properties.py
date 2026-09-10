"""NowCast property tests (tasks 10.3, 10.4, 10.5, 10.6).

Feature: ingestion-and-serving-service
- Property 21: NowCast is bounded by its window (Requirements 11.6, 11.3)
- Property 22: NowCast preserves a constant series (Requirements 11.7, 11.4)
- Property 23: NowCast weights recency and is deterministic
  (Requirements 11.8, 11.4, 11.11)
- Property 24: insufficient NowCast coverage falls back and downgrades confidence
  (Requirement 11.5)

Requirement 11.8 ("weight the most recent available hour no less than any older available
hour") is a statement about the WEIGHTS, not the values, so Property 23 asserts it on the
weight sequence directly as well as through an observable consequence — reordering a
series so its largest value is most recent must not lower the NowCast.
"""

from __future__ import annotations

import math
from itertools import pairwise

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.domain.aqi.nowcast import (
    MIN_RECENT_HOURS_REQUIRED,
    compute_nowcast,
    nowcast_weight_factor,
)
from aqm_ingestion.domain.models import Confidence

# Concentrations are non-negative (Req 8.9 clamps corrected values), bounded so the
# assertions test the formula rather than floating-point overflow.
_value = st.floats(min_value=0.0, max_value=1_000.0, allow_nan=False)
_slot = st.one_of(st.none(), _value)
_series = st.lists(_slot, min_size=0, max_size=12)


def _covered_series() -> st.SearchStrategy[list[float | None]]:
    """Series whose three most recent hours meet Requirement 11.5's coverage bar."""
    return st.builds(
        lambda recent, rest: [*recent, *rest],
        recent=st.lists(_value, min_size=3, max_size=3),
        rest=st.lists(_slot, min_size=0, max_size=9),
    )


@given(series=_covered_series(), hourly=_value)
def test_property_21_nowcast_is_bounded_by_its_window(
    series: list[float | None], hourly: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 21."""
    result = compute_nowcast(series, hourly_value=hourly)
    available = [value for value in series if value is not None]

    assert result.used_nowcast is True
    assert math.isfinite(result.value)

    # Req 11.6: never outside the range of the values it was computed from. A weighted
    # mean with positive weights cannot be, so a violation would mean a weight went
    # negative or a value leaked in from outside the window.
    assert min(available) - 1e-9 <= result.value <= max(available) + 1e-9


@given(series=_covered_series(), hourly=_value)
def test_property_21_the_weight_factor_stays_within_its_bounds(
    series: list[float | None], hourly: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 21 (weight bounds, Req 11.4)."""
    result = compute_nowcast(series, hourly_value=hourly)
    assert result.weight_factor is not None
    # floored at 0.5, and never above 1 since the range term is non-negative
    assert 0.5 <= result.weight_factor <= 1.0


@given(
    value=_value,
    hours=st.integers(min_value=3, max_value=12),
    hourly=_value,
)
def test_property_22_nowcast_preserves_a_constant_series(
    value: float, hours: int, hourly: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 22."""
    result = compute_nowcast([value] * hours, hourly_value=hourly)

    # Req 11.7: an unvarying window returns exactly that value, whatever the weighting
    assert result.value == pytest.approx(value, rel=1e-12, abs=1e-12)

    # Req 11.4: and the weight factor is 1, since there is no range to sharpen it
    assert result.weight_factor == 1.0


@given(value=_value, hours=st.integers(min_value=3, max_value=12), hourly=_value)
def test_property_22_a_constant_series_with_gaps_is_also_preserved(
    value: float, hours: int, hourly: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 22 (with gaps).

    Skipping absent hours in both sums means a gap changes the weights but not their
    ratio when every present value is identical, so the result must be unchanged.
    """
    series: list[float | None] = [value] * hours
    if hours > 3:
        series[3] = None  # a gap outside the coverage-critical recent hours
    result = compute_nowcast(series, hourly_value=hourly)
    assert result.value == pytest.approx(value, rel=1e-12, abs=1e-12)


@given(values=st.lists(_value, min_size=3, max_size=12), hourly=_value)
def test_property_23_nowcast_weights_recency_and_is_deterministic(
    values: list[float], hourly: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 23."""
    weight = nowcast_weight_factor(values)

    # Req 11.8, asserted on the WEIGHTS themselves: w**age is non-increasing in age, so
    # the most recent available hour is never weighted below an older one
    weights = [weight**age for age in range(len(values))]
    for newer, older in pairwise(weights):
        assert newer >= older

    # Req 11.11: same ordered series, same NowCast, every evaluation
    first = compute_nowcast(values, hourly_value=hourly)
    second = compute_nowcast(values, hourly_value=hourly)
    assert first == second


@given(values=st.lists(_value, min_size=3, max_size=12), hourly=_value)
def test_property_23_putting_the_largest_value_most_recent_never_lowers_the_nowcast(
    values: list[float], hourly: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 23 (observable consequence).

    An assertion about weights can be satisfied by weights nothing uses, so recency is
    also checked through its effect: the same multiset ordered with its maximum most
    recent must not produce a lower NowCast than with its minimum most recent.

    Both orderings share one weight factor, since that depends only on the max and min.
    """
    best_first = sorted(values, reverse=True)
    worst_first = sorted(values)
    high = compute_nowcast(best_first, hourly_value=hourly).value
    low = compute_nowcast(worst_first, hourly_value=hourly).value
    assert high >= low - 1e-9


@given(
    recent=st.lists(_slot, min_size=3, max_size=3),
    rest=st.lists(_slot, min_size=0, max_size=9),
    hourly=_value,
)
def test_property_24_insufficient_coverage_falls_back_and_downgrades_confidence(
    recent: list[float | None], rest: list[float | None], hourly: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 24."""
    series = [*recent, *rest]
    present_recent = sum(1 for value in recent if value is not None)
    result = compute_nowcast(series, hourly_value=hourly)

    if present_recent < MIN_RECENT_HOURS_REQUIRED:
        # Req 11.5: no NowCast, the reading's own value, Confidence capped at low
        assert result.used_nowcast is False
        assert result.value == pytest.approx(hourly)
        assert result.confidence_ceiling is Confidence.LOW
        # and no weight factor is claimed, because none was used
        assert result.weight_factor is None
    else:
        assert result.used_nowcast is True
        assert result.confidence_ceiling is None
        assert result.weight_factor is not None

    # either way the coverage metadata Req 11.9 records is present and consistent
    assert result.hours_available == sum(1 for value in series if value is not None)
    assert result.window_hours == len(series)


@given(hourly=_value, hours=st.integers(min_value=0, max_value=12))
def test_property_24_an_entirely_absent_window_always_falls_back(
    hourly: float, hours: int
) -> None:
    """Feature: ingestion-and-serving-service, Property 24 (empty window).

    The degenerate case matters on its own: with no values at all the weighted mean would
    divide by zero, so the fallback has to be reached before the arithmetic.
    """
    result = compute_nowcast([None] * hours, hourly_value=hourly)
    assert result.used_nowcast is False
    assert result.value == pytest.approx(hourly)
    assert result.hours_available == 0
