"""Humidity-growth monotonicity property test (task 8.5).

Feature: sensor-simulator-service, Property 19 — for one identical
Dry_Concentration and RH1<=RH2, Reported(RH1) <= Reported(RH2)
(Requirements 5.7, 5.8, 5.9, 5.12).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from aqm_simulator.signal.humidity import growth_factor

_rh = st.floats(min_value=0.0, max_value=100.0, allow_nan=False)
_dry = st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False)


@given(rh1=_rh, rh2=_rh, dry=_dry)
@settings(max_examples=100)
def test_property_19_humidity_monotonicity(rh1: float, rh2: float, dry: float) -> None:
    """Feature: sensor-simulator-service, Property 19."""
    lo, hi = sorted((rh1, rh2))
    reported_lo = dry * growth_factor(lo)
    reported_hi = dry * growth_factor(hi)
    assert reported_lo <= reported_hi + 1e-9


@given(rh=_rh)
@settings(max_examples=100)
def test_property_19_growth_bounds(rh: float) -> None:
    """Feature: sensor-simulator-service, Property 19 (bounds)."""
    g = growth_factor(rh)
    assert 1.0 <= g <= 2.0
    if rh <= 50.0:
        assert g == 1.0
    if rh >= 85.0:
        assert g >= 1.5
