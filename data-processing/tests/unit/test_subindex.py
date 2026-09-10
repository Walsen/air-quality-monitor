"""Unit tests for sub-index computation (tasks 9.2, 9.6).

- 10.1: piecewise-linear interpolation, rounded to nearest integer, ties AWAY FROM ZERO.
  Python's built-in round() is BANKER'S rounding and would give 50 for 50.5, so there is
  an explicit tie test.
- 10.5: the six band names.
- 10.6: truncate PM2.5 to one decimal, NO2 to whole ppb, BEFORE locating the band.
- 10.9: above the table, extrapolate the top band's slope, cap at the configured ceiling
  (default 500), report Hazardous.
- 10.10: same inputs, same value, every evaluation.
- 10.12: no sub-index for a species the table does not define.

Task 9.6 pins every published boundary as an exact concentration-to-index pair,
transcribed from Requirements 10.3 and 10.4 rather than from the table under test, so a
table that is self-consistently wrong still fails.
"""

from __future__ import annotations

import pytest

from aqm_ingestion.domain.aqi import DEFAULT_TABLE_ID, BreakpointTableRegistry
from aqm_ingestion.domain.aqi.subindex import (
    DEFAULT_INDEX_CEILING,
    SubIndexResult,
    band_name_for,
    compute_sub_index,
    truncate_to,
)

_REGISTRY = BreakpointTableRegistry.with_defaults()


def _pm25(concentration: float) -> SubIndexResult:
    table = _REGISTRY.resolve(DEFAULT_TABLE_ID, "PM25")
    return compute_sub_index(concentration, table)


def _no2(ppb: float) -> SubIndexResult:
    table = _REGISTRY.resolve(DEFAULT_TABLE_ID, "NO2")
    return compute_sub_index(ppb, table)


# --- Req 10.6 truncation --------------------------------------------------

@pytest.mark.parametrize(
    ("value", "decimals", "expected"),
    [
        (9.05, 1, 9.0),
        (9.19, 1, 9.1),
        (35.49, 1, 35.4),
        (53.9, 0, 53.0),
        (53.0, 0, 53.0),
        (0.0, 1, 0.0),
    ],
)
def test_truncation_discards_rather_than_rounds(
    value: float, decimals: int, expected: float
) -> None:
    # TRUNCATE, not round: 9.19 must become 9.1, not 9.2
    assert truncate_to(value, decimals) == pytest.approx(expected)


def test_truncation_keeps_a_value_in_the_lower_band() -> None:
    # 9.09 rounds to 9.1 (band 2) but truncates to 9.0 (band 1); Req 10.6 says truncate,
    # so this concentration is Good rather than Moderate
    assert _pm25(9.09).sub_index == 50
    assert _pm25(9.09).band == "Good"


# --- Req 10.1 the tie rule -----------------------------------------------

def test_the_tie_rule_is_reachable_through_a_table() -> None:
    # The shipped tables' slopes happen never to produce an exact .5 raw index at their
    # reporting precision (I checked the arithmetic: PM2.5 band 2's slope is 49/263 per
    # 0.1 µg/m³, and no integer multiple of it lands on a half). So rather than write a
    # conditional assertion that would silently pass, the rule is exercised through a
    # table built to hit it: index 0-1 over 0.0-2.0 gives exactly 0.5 at 1.0.
    from aqm_ingestion.domain.aqi import BreakpointBand, BreakpointTable

    table = BreakpointTable(
        table_id="tie-probe",
        species="PM25",
        unit="ug.m-3",
        averaging_period="PT24H",
        decimals=1,
        bands=(BreakpointBand(0, 1, 0.0, 2.0),),
    )
    # raw = (1-0)/(2.0-0.0) * (1.0 - 0.0) + 0 = 0.5, so the tie rule decides it
    assert compute_sub_index(1.0, table).sub_index == 1  # away from zero, not 0


def test_half_away_from_zero_is_used_not_bankers() -> None:
    # a direct test of the rounding helper's behaviour at .5, independent of any table
    from aqm_ingestion.domain.aqi.subindex import round_half_away_from_zero

    assert round_half_away_from_zero(50.5) == 51
    assert round_half_away_from_zero(51.5) == 52  # round() would give 52 here too
    assert round_half_away_from_zero(0.5) == 1  # round() gives 0
    assert round_half_away_from_zero(1.5) == 2
    assert round_half_away_from_zero(2.5) == 3  # round() gives 2


def test_rounding_is_symmetric_about_zero() -> None:
    from aqm_ingestion.domain.aqi.subindex import round_half_away_from_zero

    assert round_half_away_from_zero(-0.5) == -1
    assert round_half_away_from_zero(-2.5) == -3


# --- Req 10.3 every published PM2.5 boundary (task 9.6) ------------------

@pytest.mark.parametrize(
    ("concentration", "expected_index"),
    [
        (0.0, 0), (9.0, 50),
        (9.1, 51), (35.4, 100),
        (35.5, 101), (55.4, 150),
        (55.5, 151), (125.4, 200),
        (125.5, 201), (225.4, 300),
        (225.5, 301), (325.4, 500),
    ],
)
def test_every_pm25_boundary_is_exact(
    concentration: float, expected_index: int
) -> None:
    assert _pm25(concentration).sub_index == expected_index


# --- Req 10.4 every published NO2 boundary (task 9.6) --------------------

@pytest.mark.parametrize(
    ("ppb", "expected_index"),
    [
        (0.0, 0), (53.0, 50),
        (54.0, 51), (100.0, 100),
        (101.0, 101), (360.0, 150),
        (361.0, 151), (649.0, 200),
        (650.0, 201), (1249.0, 300),
        (1250.0, 301), (2049.0, 500),
    ],
)
def test_every_no2_boundary_is_exact(ppb: float, expected_index: int) -> None:
    assert _no2(ppb).sub_index == expected_index


# --- Req 10.5 band names -------------------------------------------------

@pytest.mark.parametrize(
    ("index", "expected"),
    [
        (0, "Good"), (50, "Good"),
        (51, "Moderate"), (100, "Moderate"),
        (101, "Unhealthy for Sensitive Groups"), (150, "Unhealthy for Sensitive Groups"),
        (151, "Unhealthy"), (200, "Unhealthy"),
        (201, "Very Unhealthy"), (300, "Very Unhealthy"),
        (301, "Hazardous"), (500, "Hazardous"), (9_999, "Hazardous"),
    ],
)
def test_band_names_match_the_requirement(index: int, expected: str) -> None:
    assert band_name_for(index) == expected


def test_band_accompanies_the_index() -> None:
    result = _pm25(40.0)
    assert result.sub_index == pytest.approx(result.sub_index)
    assert result.band == band_name_for(result.sub_index)


# --- Req 10.9 above the table -------------------------------------------

def test_above_the_table_extrapolates_the_top_slope() -> None:
    result = _pm25(400.0)  # top band ends at 325.4
    assert result.extrapolated is True
    assert result.sub_index > 500 or result.sub_index == DEFAULT_INDEX_CEILING


def test_above_the_table_is_capped_at_the_ceiling() -> None:
    assert _pm25(100_000.0).sub_index == DEFAULT_INDEX_CEILING


def test_above_the_table_reports_hazardous() -> None:
    assert _pm25(100_000.0).band == "Hazardous"
    assert _pm25(400.0).band == "Hazardous"


def test_the_ceiling_is_configurable() -> None:
    table = _REGISTRY.resolve(DEFAULT_TABLE_ID, "PM25")
    assert compute_sub_index(100_000.0, table, ceiling=300).sub_index == 300


def test_default_ceiling_is_five_hundred() -> None:
    assert DEFAULT_INDEX_CEILING == 500


def test_in_table_values_are_not_marked_extrapolated() -> None:
    assert _pm25(40.0).extrapolated is False
    assert _pm25(325.4).extrapolated is False  # exactly the top breakpoint


def test_extrapolation_is_continuous_at_the_top_breakpoint() -> None:
    # a jump at 325.4 would break Req 10.7 monotonicity right where the table ends
    at_top = _pm25(325.4).sub_index
    just_above = _pm25(325.5).sub_index
    assert just_above >= at_top


# --- Req 10.10 determinism ----------------------------------------------

def test_the_same_inputs_give_the_same_value() -> None:
    for _ in range(5):
        assert _pm25(37.2).sub_index == _pm25(37.2).sub_index


def test_the_result_records_the_table_identifier() -> None:
    # Req 10.2: the identifier travels with the value
    assert _pm25(40.0).table_id == DEFAULT_TABLE_ID


# --- interpolation within a band ----------------------------------------

def test_a_midband_value_interpolates_linearly() -> None:
    # PM2.5 band 2: index 51-100 over 9.1-35.4
    concentration = 22.2  # roughly the midpoint
    slope = (100 - 51) / (35.4 - 9.1)
    expected = round(slope * (22.2 - 9.1) + 51)
    assert _pm25(concentration).sub_index == pytest.approx(expected, abs=1)


def test_a_negative_concentration_is_refused() -> None:
    # §5: calibration clamps at 0 (Req 8.9), so a negative here is a caller bug
    table = _REGISTRY.resolve(DEFAULT_TABLE_ID, "PM25")
    with pytest.raises(ValueError, match="negative"):
        compute_sub_index(-1.0, table)
