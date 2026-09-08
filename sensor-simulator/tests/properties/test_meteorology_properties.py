"""Meteorology property tests (tasks 8.2, 8.3, 8.4).

Feature: sensor-simulator-service
- Property 16: temperature diurnal shape and range (Req 5.1, 5.3)
- Property 17: meteorology value ranges (Req 5.2, 5.5, 5.6, 5.13)
- Property 18: RH-temperature anticorrelation (Req 5.4)
"""

from __future__ import annotations

import datetime as dt

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from aqm_simulator.geography.profiles import get_profile
from aqm_simulator.rng.streams import Purpose, RandomStreamFactory
from aqm_simulator.signal.meteorology import MeteorologyEngine, MeteorologyRanges

_PROFILES = ["cochabamba", "reference"]


def _engine(profile_name: str, site: str, seed: int) -> MeteorologyEngine:
    profile = get_profile(profile_name)
    return MeteorologyEngine(
        ranges=MeteorologyRanges.from_profile(profile),
        timezone=profile.timezone,
        rng=RandomStreamFactory(seed=seed).stream(site, Purpose.METEOROLOGY),
    )


def _day_series(
    engine: MeteorologyEngine, start: dt.datetime, hours: int
) -> tuple[list[float], list[float]]:
    temps, rhs = [], []
    for h in range(hours):
        r = engine.reading(start + dt.timedelta(hours=h))
        temps.append(r.temperature_c)
        rhs.append(r.relative_humidity_pct)
    return temps, rhs


@given(
    profile_name=st.sampled_from(_PROFILES),
    seed=st.integers(min_value=0, max_value=10_000),
)
@settings(max_examples=100)
def test_property_16_temperature_diurnal_shape_and_range(profile_name: str, seed: int) -> None:
    """Feature: sensor-simulator-service, Property 16."""
    engine = _engine(profile_name, "CB0001", seed)
    start = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    temps, _ = _day_series(engine, start, 24)
    diurnal = max(temps) - min(temps)
    assert 10.0 <= diurnal <= 20.0 + 1e-9


@given(
    profile_name=st.sampled_from(_PROFILES),
    seed=st.integers(min_value=0, max_value=10_000),
    hour=st.integers(min_value=0, max_value=23),
)
@settings(max_examples=100)
def test_property_17_meteorology_value_ranges(profile_name: str, seed: int, hour: int) -> None:
    """Feature: sensor-simulator-service, Property 17."""
    profile = get_profile(profile_name)
    engine = _engine(profile_name, "CB0001", seed)
    r = engine.reading(dt.datetime(2026, 7, 1, hour, tzinfo=dt.UTC))
    assert profile.temp_min_c <= r.temperature_c <= profile.temp_max_c
    assert profile.pressure_min_hpa <= r.pressure_hpa <= profile.pressure_max_hpa
    assert 0.0 <= r.relative_humidity_pct <= 100.0


@given(
    profile_name=st.sampled_from(_PROFILES),
    seed=st.integers(min_value=0, max_value=10_000),
)
@settings(max_examples=100)
def test_property_18_rh_temp_anticorrelation(profile_name: str, seed: int) -> None:
    """Feature: sensor-simulator-service, Property 18 (Pearson <= -0.5 over 72h)."""
    engine = _engine(profile_name, "CB0001", seed)
    start = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    temps, rhs = _day_series(engine, start, 72)
    corr = float(np.corrcoef(temps, rhs)[0, 1])
    assert corr <= -0.5
