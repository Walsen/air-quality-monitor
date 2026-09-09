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
@settings(deadline=None)  # example count comes from the profile (ci=100)
def test_property_29_spatial_decay(seed: int) -> None:
    """Feature: sensor-simulator-service, Property 29.

    Requirement 9.3 states the property over BAND MEANS, not single pairs: for
    all ordered band pairs drawn from 0-2 km, 2-5 km, 5-10 km and >10 km, the
    mean correlation of the nearer band must be at least the farther band's mean
    minus 0.05. Comparing one pair per band instead would measure per-pair
    sampling noise rather than the spatial structure, so each band mean here is
    taken over several equal-SiteClassification pairs.
    """
    base = _pm_at(seed, _LAT0, _LON0, 72)

    # latitude offsets whose great-circle separation falls inside each band
    bands: dict[str, tuple[float, ...]] = {
        "0-2km": (0.009, 0.013, 0.016),
        "2-5km": (0.027, 0.036, 0.040),
        "5-10km": (0.054, 0.063, 0.081),
        ">10km": (0.108, 0.135, 0.180),
    }
    limits = {"0-2km": (0.0, 2.0), "2-5km": (2.0, 5.0), "5-10km": (5.0, 10.0),
              ">10km": (10.0, 1e9)}

    band_means: list[float] = []
    for name, offsets in bands.items():
        low, high = limits[name]
        correlations: list[float] = []
        for offset in offsets:
            distance = great_circle_km(_LAT0, _LON0, _LAT0 + offset, _LON0)
            assert low <= distance <= high, f"{name} offset {offset} is {distance:.2f}km"
            correlations.append(_corr(base, _pm_at(seed, _LAT0 + offset, _LON0, 72)))
        band_means.append(sum(correlations) / len(correlations))

    # every nearer band's mean >= each farther band's mean - 0.05
    for nearer in range(len(band_means)):
        for farther in range(nearer + 1, len(band_means)):
            assert band_means[nearer] >= band_means[farther] - 0.05, (
                f"band {nearer} mean {band_means[nearer]:.3f} < "
                f"band {farther} mean {band_means[farther]:.3f} - 0.05"
            )
