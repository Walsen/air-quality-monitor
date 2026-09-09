"""Unit tests for Breakpoint_Table loading and validation (task 9.1).

- 10.2: tables are DATA identified by a table identifier, default `epa-2024-05-06`.
- 10.3/10.4: the shipped PM2.5 and NO2 bands, exactly as the requirements state.
- 10.11: an overlapping band, a gap, a non-increasing breakpoint or index sequence, or a
  unit mismatch is rejected ON LOAD, naming the offending band and the violated
  constraint.

The subtlety that shapes the whole validator: the published bands step 9.0 -> 9.1 and
53 -> 54, so consecutive bands are adjacent AT THE TABLE'S REPORTING PRECISION rather
than sharing a boundary value. A naive gap check (`next.lower == prev.upper`) would
reject the shipped table with its own validator, so precision is part of the table and
adjacency is judged against it. Requirement 10.6's truncation is what makes this sound:
every value truncates onto a band, so nothing can fall between two of them.
"""

from __future__ import annotations

import pytest

from aqm_ingestion.domain.aqi import (
    DEFAULT_TABLE_ID,
    BreakpointBand,
    BreakpointTable,
    BreakpointTableError,
    BreakpointTableRegistry,
)

# Requirement 10.3, transcribed from the requirement rather than from the code under
# test, so a table that is wrong in the same way as its loader still fails.
_PM25_BANDS = [
    (0, 50, 0.0, 9.0),
    (51, 100, 9.1, 35.4),
    (101, 150, 35.5, 55.4),
    (151, 200, 55.5, 125.4),
    (201, 300, 125.5, 225.4),
    (301, 500, 225.5, 325.4),
]

# Requirement 10.4.
_NO2_BANDS = [
    (0, 50, 0.0, 53.0),
    (51, 100, 54.0, 100.0),
    (101, 150, 101.0, 360.0),
    (151, 200, 361.0, 649.0),
    (201, 300, 650.0, 1249.0),
    (301, 500, 1250.0, 2049.0),
]


def _band(
    index_low: int = 0,
    index_high: int = 50,
    bp_low: float = 0.0,
    bp_high: float = 9.0,
) -> BreakpointBand:
    return BreakpointBand(
        index_low=index_low, index_high=index_high, bp_low=bp_low, bp_high=bp_high
    )


def _table(bands: list[BreakpointBand], decimals: int = 1) -> BreakpointTable:
    return BreakpointTable(
        table_id="test",
        species="PM25",
        unit="ug.m-3",
        averaging_period="PT24H",
        decimals=decimals,
        bands=tuple(bands),
    )


# --- Req 10.2 identity and registry --------------------------------------

def test_default_table_identifier_is_the_documented_one() -> None:
    assert DEFAULT_TABLE_ID == "epa-2024-05-06"


def test_default_table_resolves_by_identifier() -> None:
    registry = BreakpointTableRegistry.with_defaults()
    assert registry.resolve(DEFAULT_TABLE_ID, "PM25").table_id == DEFAULT_TABLE_ID


def test_unknown_table_identifier_is_refused_naming_the_options() -> None:
    registry = BreakpointTableRegistry.with_defaults()
    with pytest.raises(BreakpointTableError) as caught:
        registry.resolve("no-such-table", "PM25")
    assert "no-such-table" in str(caught.value)
    assert DEFAULT_TABLE_ID in str(caught.value)


def test_registered_identifiers_are_sorted() -> None:
    # §2: order that reaches an error message must be defined
    registry = BreakpointTableRegistry.with_defaults()
    assert registry.table_ids() == tuple(sorted(registry.table_ids()))


# --- Req 10.12 only defined species --------------------------------------

def test_a_species_the_table_does_not_define_is_absent() -> None:
    # Req 10.12: omit rather than approximate
    registry = BreakpointTableRegistry.with_defaults()
    assert registry.get(DEFAULT_TABLE_ID, "NO2Index") is None
    assert registry.get(DEFAULT_TABLE_ID, "PM25Index") is None


def test_resolving_an_undefined_species_is_refused() -> None:
    registry = BreakpointTableRegistry.with_defaults()
    with pytest.raises(BreakpointTableError, match="PM25Index"):
        registry.resolve(DEFAULT_TABLE_ID, "PM25Index")


def test_both_mass_species_are_defined() -> None:
    registry = BreakpointTableRegistry.with_defaults()
    assert registry.get(DEFAULT_TABLE_ID, "PM25") is not None
    assert registry.get(DEFAULT_TABLE_ID, "NO2") is not None


# --- Req 10.3 the shipped PM2.5 table ------------------------------------

def test_pm25_table_matches_the_requirement_exactly() -> None:
    table = BreakpointTableRegistry.with_defaults().resolve(DEFAULT_TABLE_ID, "PM25")
    actual = [
        (band.index_low, band.index_high, band.bp_low, band.bp_high)
        for band in table.bands
    ]
    assert actual == _PM25_BANDS


def test_pm25_table_declares_its_unit_and_period() -> None:
    table = BreakpointTableRegistry.with_defaults().resolve(DEFAULT_TABLE_ID, "PM25")
    assert table.unit == "ug.m-3"
    assert table.averaging_period == "PT24H"  # a 24-hour average (Req 10.3)
    assert table.decimals == 1  # Req 10.6 truncates PM2.5 to one decimal


# --- Req 10.4 the shipped NO2 table --------------------------------------

def test_no2_table_matches_the_requirement_exactly() -> None:
    table = BreakpointTableRegistry.with_defaults().resolve(DEFAULT_TABLE_ID, "NO2")
    actual = [
        (band.index_low, band.index_high, band.bp_low, band.bp_high)
        for band in table.bands
    ]
    assert actual == _NO2_BANDS


def test_no2_table_is_declared_in_ppb_not_mass() -> None:
    # the reason Req 9's conversion exists at all: this table is NOT in µg/m³
    table = BreakpointTableRegistry.with_defaults().resolve(DEFAULT_TABLE_ID, "NO2")
    assert table.unit == "ppb"
    assert table.averaging_period == "PT1H"
    assert table.decimals == 0  # Req 10.6 truncates NO2 to whole ppb


# --- Req 10.11 rejection on load -----------------------------------------

def test_overlapping_bands_are_rejected_naming_the_band() -> None:
    with pytest.raises(BreakpointTableError) as caught:
        _table([_band(bp_low=0.0, bp_high=10.0), _band(51, 100, 5.0, 20.0)])
    message = str(caught.value)
    assert "overlap" in message.lower()
    assert "5.0" in message  # the offending band is named


def test_a_gap_between_bands_is_rejected() -> None:
    # a real gap: 9.0 to 12.0 leaves 9.1 through 11.9 unclassifiable
    with pytest.raises(BreakpointTableError, match="gap"):
        _table([_band(bp_low=0.0, bp_high=9.0), _band(51, 100, 12.0, 20.0)])


def test_the_documented_one_step_offset_is_not_a_gap() -> None:
    # the case that would break the shipped table: 9.0 -> 9.1 at one decimal is
    # ADJACENT, because Req 10.6 truncates every value onto one band or the other
    table = _table([_band(bp_low=0.0, bp_high=9.0), _band(51, 100, 9.1, 35.4)])
    assert len(table.bands) == 2


def test_whole_number_adjacency_is_honoured_at_zero_decimals() -> None:
    # the NO2 case: 53 -> 54 with decimals=0
    table = _table(
        [_band(bp_low=0.0, bp_high=53.0), _band(51, 100, 54.0, 100.0)], decimals=0
    )
    assert len(table.bands) == 2


def test_a_shared_boundary_value_is_an_overlap_not_adjacency() -> None:
    # 9.0 then 9.0 means one concentration sits in two bands
    with pytest.raises(BreakpointTableError, match="overlap"):
        _table([_band(bp_low=0.0, bp_high=9.0), _band(51, 100, 9.0, 35.4)])


def test_a_non_increasing_breakpoint_within_a_band_is_rejected() -> None:
    with pytest.raises(BreakpointTableError, match="increasing"):
        _table([_band(bp_low=9.0, bp_high=9.0)])


def test_an_inverted_band_is_rejected() -> None:
    with pytest.raises(BreakpointTableError, match="increasing"):
        _table([_band(bp_low=10.0, bp_high=1.0)])


def test_a_non_increasing_index_sequence_is_rejected() -> None:
    with pytest.raises(BreakpointTableError, match="increasing"):
        _table([_band(index_low=50, index_high=0)])


def test_a_descending_index_across_bands_is_rejected() -> None:
    with pytest.raises(BreakpointTableError, match="increasing"):
        _table(
            [
                _band(0, 50, 0.0, 9.0),
                _band(index_low=20, index_high=40, bp_low=9.1, bp_high=35.4),
            ]
        )


def test_an_empty_table_is_rejected() -> None:
    # §5: an empty table would silently compute no sub-index for anything
    with pytest.raises(BreakpointTableError, match="at least one band"):
        _table([])


def test_a_unit_mismatch_is_rejected() -> None:
    # Req 10.11's fourth condition: a band whose unit differs from the species unit the
    # table declares
    with pytest.raises(BreakpointTableError, match="unit"):
        BreakpointTable(
            table_id="test",
            species="PM25",
            unit="ppb",  # PM2.5 is a mass concentration
            averaging_period="PT24H",
            decimals=1,
            bands=(_band(),),
        )


def test_a_non_finite_breakpoint_is_rejected() -> None:
    with pytest.raises(BreakpointTableError, match="finite"):
        _table([_band(bp_high=float("inf"))])


def test_rejection_names_the_violated_constraint() -> None:
    # Req 10.11 wants BOTH the band and the constraint, so a config author does not
    # have to guess which rule they broke
    with pytest.raises(BreakpointTableError) as caught:
        _table([_band(bp_low=0.0, bp_high=9.0), _band(51, 100, 12.0, 20.0)])
    assert caught.value.constraint == "gap"
    assert caught.value.band is not None


def test_the_shipped_tables_pass_their_own_validator() -> None:
    # the point of the adjacency rule: loading the defaults must not raise
    registry = BreakpointTableRegistry.with_defaults()
    for species in ("PM25", "NO2"):
        table = registry.resolve(DEFAULT_TABLE_ID, species)
        assert table.bands
