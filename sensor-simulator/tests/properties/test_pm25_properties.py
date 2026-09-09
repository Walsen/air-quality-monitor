"""PM2.5 / regional-field property tests (tasks 9.2, 9.3, 9.8-9.11).

Feature: sensor-simulator-service
- Property 9: non-negative pollutant values (Req 4.7)
- Property 10: clean-scenario pollutant ranges (Req 4.5)
- Property 13: PM2.5 diurnal amplitude bound (Req 4.4)
- Property 14: signal autocorrelation floor (Req 4.9)
- Property 15: seasonal PM2.5 multiplier (Req 4.8)
- Property 27: regional-field variance share (Req 9.1)
"""

from __future__ import annotations

import datetime as dt

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from aqm_simulator.geography.profiles import get_profile
from aqm_simulator.rng.streams import Purpose, RandomStreamFactory
from aqm_simulator.signal.pollutants import NO2Signal, PM25Signal
from aqm_simulator.signal.regional_field import RegionalField

_TZ = "America/La_Paz"
_SITES = ["CB0001", "CB0002", "CB0003", "CB0004", "CB0005"]


def _pm_series(seed: int, site: str, start: dt.datetime, hours: int) -> list[float]:
    profile = get_profile("cochabamba")
    factory = RandomStreamFactory(seed=seed)
    field = RegionalField.from_profile(profile, factory)
    sig = PM25Signal(regional=field, site_code=site, factory=factory)
    return [sig.dry_value(start + dt.timedelta(hours=h)) for h in range(hours)]


def _relative_diurnal_amplitude(series: list[float]) -> float:
    # mean-of-hour profile over 24 hours, (max-min)/mean
    by_hour: dict[int, list[float]] = {}
    for i, v in enumerate(series):
        by_hour.setdefault(i % 24, []).append(v)
    profile = [sum(vs) / len(vs) for vs in by_hour.values()]
    mean = sum(profile) / len(profile)
    return (max(profile) - min(profile)) / mean


@given(seed=st.integers(min_value=0, max_value=10_000))
@settings(max_examples=100)
def test_property_9_non_negative(seed: int) -> None:
    """Feature: sensor-simulator-service, Property 9."""
    series = _pm_series(seed, "CB0001", dt.datetime(2026, 7, 1, tzinfo=dt.UTC), 72)
    assert all(v >= 0 for v in series)


@given(seed=st.integers(min_value=0, max_value=10_000))
@settings(max_examples=100)
def test_property_10_clean_pm25_range(seed: int) -> None:
    """Feature: sensor-simulator-service, Property 10 (PM2.5 3..35 wet season)."""
    series = _pm_series(seed, "CB0001", dt.datetime(2026, 1, 15, tzinfo=dt.UTC), 72)
    assert all(3.0 <= v <= 35.0 for v in series)


@given(seed=st.integers(min_value=0, max_value=10_000))
@settings(max_examples=100)
def test_property_13_pm25_diurnal_amplitude_bound(seed: int) -> None:
    """Feature: sensor-simulator-service, Property 13 (PM2.5 amp <= 0.5 * NO2 amp)."""
    start = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    pm = _pm_series(seed, "CB0001", start, 72)
    factory = RandomStreamFactory(seed=seed)
    no2sig = NO2Signal(
        classification="Roadside", timezone=_TZ, rng=factory.stream("CB0001", Purpose.SIGNAL)
    )
    no2 = [no2sig.value(start + dt.timedelta(hours=h)) for h in range(72)]
    assert _relative_diurnal_amplitude(pm) <= 0.5 * _relative_diurnal_amplitude(no2) + 1e-9


@given(seed=st.integers(min_value=0, max_value=10_000))
@settings(max_examples=100)
def test_property_14_autocorrelation_floor(seed: int) -> None:
    """Feature: sensor-simulator-service, Property 14 (lag-1 autocorr >= 0.6)."""
    series = np.array(_pm_series(seed, "CB0001", dt.datetime(2026, 7, 1, tzinfo=dt.UTC), 72))
    lag1 = float(np.corrcoef(series[:-1], series[1:])[0, 1])
    assert lag1 >= 0.6


@given(seed=st.integers(min_value=0, max_value=10_000))
@settings(max_examples=100)
def test_property_15_seasonal_multiplier(seed: int) -> None:
    """Feature: sensor-simulator-service, Property 15 (dry/wet 1.5..3.0)."""
    dry = _pm_series(seed, "CB0001", dt.datetime(2026, 8, 1, tzinfo=dt.UTC), 24 * 7)
    wet = _pm_series(seed, "CB0001", dt.datetime(2026, 1, 15, tzinfo=dt.UTC), 24 * 7)
    ratio = (sum(dry) / len(dry)) / (sum(wet) / len(wet))
    assert 1.5 <= ratio <= 3.0


@given(seed=st.integers(min_value=0, max_value=10_000))
@settings(max_examples=100)
def test_property_27_regional_variance_share(seed: int) -> None:
    """Feature: sensor-simulator-service, Property 27 (regional >= 60% of variance)."""
    profile = get_profile("cochabamba")
    factory = RandomStreamFactory(seed=seed)
    field = RegionalField.from_profile(profile, factory)
    start = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    hours = 72
    regional = np.array([field.baseline(start + dt.timedelta(hours=h)) for h in range(hours)])
    # total signal across the swarm: mean over sites of each site's series
    per_site = []
    for site in _SITES:
        sig = PM25Signal(regional=field, site_code=site, factory=RandomStreamFactory(seed=seed))
        per_site.append([sig.dry_value(start + dt.timedelta(hours=h)) for h in range(hours)])
    total = np.array(per_site)
    # variance of the shared component vs mean per-site total variance
    regional_var = float(np.var(regional))
    mean_total_var = float(np.mean([np.var(s) for s in total]))
    assert regional_var >= 0.60 * mean_total_var
