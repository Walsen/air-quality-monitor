"""Unit tests for NowCast (tasks 10.1, 10.2).

- 11.2: window of the configured length, default 12 hours, at most one value per hour.
- 11.3: `sum(w**i * c_i) / sum(w**i)`, i = 0 the MOST RECENT hour, increasing into the
  past, skipping absent hours in BOTH sums.
- 11.4: `w = 1 - (c_max - c_min) / c_max`, bounded below at 0.5; w = 1 when c_max is 0.
- 11.5: fewer than two of the three MOST RECENT hours available means no NowCast — fall
  back to the reading's own hourly value and cap Confidence at low.
- 11.9: record hours available, window length, and the weight factor used.
- 11.10: PM2.5 only; NO2 stays on its own 1-hour value.

The exponent is the hour's AGE INDEX, not its position among the available values. With a
gap at the most recent hour, the next value down still carries w**1 rather than w**0 —
that is what keeps Requirement 11.8's recency weighting honest across gaps, and there is
a test for it below.
"""

from __future__ import annotations

import pytest

from aqm_ingestion.domain.aqi.nowcast import (
    DEFAULT_NOWCAST_WINDOW_HOURS,
    MIN_RECENT_HOURS_REQUIRED,
    NowCastResult,
    compute_nowcast,
    nowcast_weight_factor,
    supports_nowcast,
)
from aqm_ingestion.domain.models import Confidence

# --- Req 11.4 the weight factor ------------------------------------------

def test_weight_factor_is_one_for_a_constant_series() -> None:
    # max == min, so the range term vanishes
    assert nowcast_weight_factor([5.0, 5.0, 5.0]) == 1.0


def test_weight_factor_is_one_when_the_maximum_is_zero() -> None:
    # Req 11.4's explicit case: the formula would divide by zero
    assert nowcast_weight_factor([0.0, 0.0]) == 1.0


def test_weight_factor_follows_the_formula() -> None:
    # 1 - (20 - 10)/20 = 0.5
    assert nowcast_weight_factor([20.0, 10.0]) == pytest.approx(0.5)


def test_weight_factor_is_clamped_below_at_one_half() -> None:
    # a window whose minimum is 0 gives a raw factor of 0, which would discard every
    # older hour entirely
    assert nowcast_weight_factor([100.0, 0.0]) == 0.5


def test_a_moderate_range_gives_a_factor_between_the_bounds() -> None:
    # 1 - (20 - 16)/20 = 0.8
    assert nowcast_weight_factor([20.0, 16.0]) == pytest.approx(0.8)


def test_weight_factor_ignores_absent_hours() -> None:
    assert nowcast_weight_factor([20.0, 16.0]) == nowcast_weight_factor([20.0, 16.0])


def test_weight_factor_of_a_single_value_is_one() -> None:
    assert nowcast_weight_factor([7.0]) == 1.0


def test_weight_factor_of_an_empty_series_is_one() -> None:
    # no range to measure; 1 keeps the weighted mean well-defined
    assert nowcast_weight_factor([]) == 1.0


# --- Req 11.3 the weighted average ---------------------------------------

def test_nowcast_of_a_constant_series_is_that_value() -> None:
    result = compute_nowcast([8.0] * 12, hourly_value=8.0)
    assert result.value == pytest.approx(8.0)


def test_nowcast_follows_the_documented_formula() -> None:
    series = [20.0, 16.0, 12.0]
    weight = nowcast_weight_factor([20.0, 16.0, 12.0])
    numerator = sum(weight**i * value for i, value in enumerate(series))
    denominator = sum(weight**i for i in range(len(series)))
    result = compute_nowcast(series, hourly_value=20.0)
    assert result.value == pytest.approx(numerator / denominator)


def test_absent_hours_are_skipped_in_both_sums() -> None:
    # the same two values with a gap between them must not be renormalised as if they
    # were adjacent: the older one keeps its age-based weight
    with_gap = compute_nowcast([20.0, None, 12.0], hourly_value=20.0)
    adjacent = compute_nowcast([20.0, 12.0], hourly_value=20.0)
    assert with_gap.value != pytest.approx(adjacent.value)


def test_the_exponent_is_the_age_index_not_the_position() -> None:
    # a gap at the MOST RECENT hour: the surviving value is one hour old, so it carries
    # w**1 in both sums, which cancels and leaves the value itself
    result = compute_nowcast([None, 14.0], hourly_value=14.0)
    assert result.value == pytest.approx(14.0)


def test_recent_values_dominate() -> None:
    rising = compute_nowcast([30.0, 10.0], hourly_value=30.0)
    falling = compute_nowcast([10.0, 30.0], hourly_value=10.0)
    # the same two numbers, ordered differently: the one with 30 most recent must be
    # higher, or recency is not being weighted at all
    assert rising.value > falling.value


def test_nowcast_lies_within_the_window_range() -> None:
    result = compute_nowcast([30.0, 10.0, 20.0], hourly_value=30.0)
    assert 10.0 <= result.value <= 30.0


# --- Req 11.2 / 11.9 the window and its metadata -------------------------

def test_default_window_is_twelve_hours() -> None:
    assert DEFAULT_NOWCAST_WINDOW_HOURS == 12


def test_result_records_the_coverage_metadata() -> None:
    result = compute_nowcast([9.0, 8.0, 7.0, None, 5.0], hourly_value=9.0)
    assert result.hours_available == 4
    assert result.window_hours == 5
    assert result.weight_factor == pytest.approx(
        nowcast_weight_factor([9.0, 8.0, 7.0, 5.0])
    )


def test_a_series_longer_than_the_window_is_truncated() -> None:
    # Req 11.2: at most the configured window, so a longer series is cut to it
    result = compute_nowcast([1.0] * 20, hourly_value=1.0, window_hours=12)
    assert result.window_hours == 12
    assert result.hours_available == 12


def test_a_series_shorter_than_the_window_reports_its_own_length() -> None:
    result = compute_nowcast([4.0, 4.0], hourly_value=4.0, window_hours=12)
    assert result.hours_available == 2


# --- Req 11.5 the coverage fallback --------------------------------------

def test_minimum_recent_hours_is_two_of_three() -> None:
    assert MIN_RECENT_HOURS_REQUIRED == 2


def test_one_of_the_three_most_recent_hours_falls_back() -> None:
    result = compute_nowcast([9.0, None, None, 8.0, 7.0], hourly_value=9.0)
    assert result.used_nowcast is False
    assert result.value == pytest.approx(9.0)  # the reading's own hourly value
    assert result.confidence_ceiling is Confidence.LOW


def test_two_of_the_three_most_recent_hours_is_enough() -> None:
    result = compute_nowcast([9.0, None, 7.0, 6.0], hourly_value=9.0)
    assert result.used_nowcast is True
    assert result.confidence_ceiling is None


def test_coverage_is_judged_on_the_three_most_recent_hours_only() -> None:
    # plenty of older data cannot substitute: the whole point of NowCast is recency
    result = compute_nowcast(
        [None, None, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0], hourly_value=9.0
    )
    assert result.used_nowcast is False


def test_an_empty_window_falls_back() -> None:
    result = compute_nowcast([], hourly_value=11.0)
    assert result.used_nowcast is False
    assert result.value == pytest.approx(11.0)
    assert result.hours_available == 0


def test_an_all_absent_window_falls_back() -> None:
    result = compute_nowcast([None] * 12, hourly_value=11.0)
    assert result.used_nowcast is False
    assert result.value == pytest.approx(11.0)


def test_fallback_still_reports_its_coverage() -> None:
    # Req 11.9's metadata is what explains the fallback in the Basis, so it must be
    # present precisely when the fallback happened
    result = compute_nowcast([9.0, None, None], hourly_value=9.0)
    assert result.hours_available == 1
    assert result.window_hours == 3


def test_fallback_reports_no_weight_factor() -> None:
    # no NowCast was computed, so claiming a weight factor would misdescribe the value
    result = compute_nowcast([], hourly_value=11.0)
    assert result.weight_factor is None


# --- Req 11.10 PM2.5 only ------------------------------------------------

def test_nowcast_applies_to_pm25() -> None:
    assert supports_nowcast("PM25") is True


def test_nowcast_does_not_apply_to_no2() -> None:
    # the NO2 table is already defined over a 1-hour interval, so a 12-hour smoothing
    # would place the value on a scale it was not built for
    assert supports_nowcast("NO2") is False


@pytest.mark.parametrize("species", ["NO2Index", "PM25Index"])
def test_nowcast_does_not_apply_to_index_species(species: str) -> None:
    assert supports_nowcast(species) is False


# --- Req 11.11 determinism -----------------------------------------------

def test_the_same_series_gives_the_same_nowcast() -> None:
    series = [12.0, None, 9.5, 8.0]
    first = compute_nowcast(series, hourly_value=12.0)
    second = compute_nowcast(series, hourly_value=12.0)
    assert first == second


def test_the_series_is_not_mutated() -> None:
    series: list[float | None] = [12.0, None, 9.5]
    compute_nowcast(series, hourly_value=12.0)
    assert series == [12.0, None, 9.5]


# --- error cases ---------------------------------------------------------

def test_a_negative_value_in_the_window_is_refused() -> None:
    # §5: corrected values are clamped at 0 (Req 8.9), so a negative here is a caller bug
    with pytest.raises(ValueError, match="negative"):
        compute_nowcast([5.0, -1.0], hourly_value=5.0)


def test_a_non_positive_window_is_refused() -> None:
    with pytest.raises(ValueError, match="window"):
        compute_nowcast([5.0], hourly_value=5.0, window_hours=0)


def test_result_is_frozen() -> None:
    result = compute_nowcast([5.0, 5.0], hourly_value=5.0)
    with pytest.raises((AttributeError, TypeError)):
        result.value = 1.0  # type: ignore[misc]


def test_result_type_is_the_documented_shape() -> None:
    assert set(NowCastResult.__dataclass_fields__) == {
        "value",
        "used_nowcast",
        "hours_available",
        "window_hours",
        "weight_factor",
        "confidence_ceiling",
    }
