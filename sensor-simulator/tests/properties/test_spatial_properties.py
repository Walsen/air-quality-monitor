"""Spatial correlation property tests (tasks 12.8, 12.9).

Feature: sensor-simulator-service
- Property 28: near-sensor spatial correlation (Req 9.2, 9.5)
- Property 29: spatial decay (Req 9.3)
"""

from __future__ import annotations

import datetime as dt

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from aqm_simulator.geography.distance import great_circle_km
from aqm_simulator.geography.profiles import get_profile
from aqm_simulator.rng.streams import RandomStreamFactory
from aqm_simulator.signal.pollutants import PM25Signal
from aqm_simulator.signal.regional_field import RegionalField
from aqm_simulator.signal.spatial_field import SpatialField

_LAT0, _LON0 = -17.39, -66.15  # a point inside the Kanata bbox
_SPATIAL_AMP = 4.0


def _pm_at(seed: int, lat: float, lon: float, hours: int) -> list[float]:
    profile = get_profile("cochabamba")
    factory = RandomStreamFactory(seed=seed)
    field = RegionalField.from_profile(profile, factory)
    spatial = SpatialField.build(factory, amplitude=_SPATIAL_AMP)
    sig = PM25Signal(
        regional=field,
        site_code=f"CB{int(abs(lat * lon) * 100) % 10000:04d}",
        factory=factory,
        latitude=lat,
        longitude=lon,
        spatial=spatial,
    )
    start = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    return [sig.dry_value(start + dt.timedelta(hours=h)) for h in range(hours)]


def _corr(a: list[float], b: list[float]) -> float:
    return float(np.corrcoef(a, b)[0, 1])


@given(seed=st.integers(min_value=0, max_value=5_000))
@settings(max_examples=100, deadline=None)
def test_property_28_near_pair_correlation(seed: int) -> None:
    """Feature: sensor-simulator-service, Property 28 (<2km same-class corr>=0.6)."""
    # two sensors ~1 km apart (0.009 deg latitude)
    a = _pm_at(seed, _LAT0, _LON0, 72)
    b = _pm_at(seed, _LAT0 + 0.009, _LON0, 72)
    assert great_circle_km(_LAT0, _LON0, _LAT0 + 0.009, _LON0) < 2.0
    assert _corr(a, b) >= 0.6


@given(seed=st.integers(min_value=0, max_value=5_000))
@settings(max_examples=50, deadline=None)
def test_property_29_spatial_decay(seed: int) -> None:
    """Feature: sensor-simulator-service, Property 29 (nearer band corr >= farther - 0.05)."""
    base = _pm_at(seed, _LAT0, _LON0, 72)
    near = _pm_at(seed, _LAT0 + 0.009, _LON0, 72)  # ~1 km
    far = _pm_at(seed, _LAT0 + 0.08, _LON0 + 0.12, 72)  # ~16 km
    near_corr = _corr(base, near)
    far_corr = _corr(base, far)
    assert near_corr >= far_corr - 0.05
