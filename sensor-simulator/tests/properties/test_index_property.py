"""Index derivation property test (task 10.2).

Feature: sensor-simulator-service, Property 20 — index monotonicity and
determinism (Req 6.3, 6.4, 6.5, 6.6).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from aqm_simulator.signal.index_bands import default_breakpoint_table, derive_index_band

_TABLE = default_breakpoint_table()
_conc = st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False)


@given(a=_conc, b=_conc, species=st.sampled_from(["PM25", "NO2"]))
@settings(max_examples=100)
def test_property_20_monotonic_and_deterministic(a: float, b: float, species: str) -> None:
    """Feature: sensor-simulator-service, Property 20."""
    lo, hi = sorted((a, b))
    band_lo, _ = derive_index_band(_TABLE, species, lo)
    band_hi, _ = derive_index_band(_TABLE, species, hi)
    assert band_lo <= band_hi  # monotonic
    # deterministic: same input -> same band
    assert derive_index_band(_TABLE, species, lo)[0] == band_lo


@given(conc=_conc, species=st.sampled_from(["PM25", "NO2"]))
@settings(max_examples=100)
def test_property_20_band_within_bounds(conc: float, species: str) -> None:
    """Feature: sensor-simulator-service, Property 20 (bounds 1..10)."""
    band, _ = derive_index_band(_TABLE, species, conc)
    assert 1 <= band <= 10
