"""Unit tests for index-band derivation (tasks 10.1, 10.3).

Requirement 6.3: derive the index from the concentration ScaledValue using the
per-Species breakpoint table; lower bound inclusive, upper exclusive, so a
concentration equal to a band boundary yields the HIGHER band.
Requirement 6.5: index is an integer within the table's bounds (default 1..10).
Requirement 6.6: a concentration >= the highest band's upper bound clamps to the
highest index and records the clamp event.
Requirement 6.7: an empty / overlapping / gapped / non-increasing table is
rejected, naming the offending band.
"""

from __future__ import annotations

import pytest

from aqm_simulator.signal.index_bands import (
    Band,
    BreakpointTable,
    BreakpointTableError,
    default_breakpoint_table,
    derive_index_band,
)


def test_default_table_has_ten_bands_1_to_10() -> None:
    table = default_breakpoint_table()
    for species in ("PM25", "NO2"):
        bands = table.bands(species)
        assert len(bands) == 10
        assert [b.index for b in bands] == list(range(1, 11))


def test_lower_inclusive_upper_exclusive_boundary_goes_higher() -> None:
    # Two bands: [0,10)->1, [10,20)->2. A value of exactly 10 lands in band 2.
    table = BreakpointTable({"PM25": [Band(0.0, 10.0, 1), Band(10.0, 20.0, 2)]})
    assert derive_index_band(table, "PM25", 9.99)[0] == 1
    assert derive_index_band(table, "PM25", 10.0)[0] == 2


def test_clamp_above_top_records_event() -> None:
    table = BreakpointTable({"PM25": [Band(0.0, 10.0, 1), Band(10.0, 20.0, 2)]})
    band, clamped = derive_index_band(table, "PM25", 999.0)
    assert band == 2  # highest index
    assert clamped is True


def test_within_range_not_clamped() -> None:
    table = BreakpointTable({"PM25": [Band(0.0, 10.0, 1), Band(10.0, 20.0, 2)]})
    _, clamped = derive_index_band(table, "PM25", 5.0)
    assert clamped is False


def test_reject_empty_table() -> None:
    with pytest.raises(BreakpointTableError):
        BreakpointTable({"PM25": []})


def test_reject_overlapping_bands() -> None:
    with pytest.raises(BreakpointTableError) as exc:
        BreakpointTable({"PM25": [Band(0.0, 12.0, 1), Band(10.0, 20.0, 2)]})
    assert "overlap" in str(exc.value).lower()


def test_reject_gapped_bands() -> None:
    with pytest.raises(BreakpointTableError) as exc:
        BreakpointTable({"PM25": [Band(0.0, 10.0, 1), Band(12.0, 20.0, 2)]})
    assert "gap" in str(exc.value).lower()


def test_reject_non_increasing_index() -> None:
    with pytest.raises(BreakpointTableError):
        BreakpointTable({"PM25": [Band(0.0, 10.0, 2), Band(10.0, 20.0, 1)]})


def test_reject_table_not_starting_at_zero() -> None:
    with pytest.raises(BreakpointTableError):
        BreakpointTable({"PM25": [Band(5.0, 10.0, 1), Band(10.0, 20.0, 2)]})
