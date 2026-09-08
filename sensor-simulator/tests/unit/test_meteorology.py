"""Unit tests for the meteorology engine (task 8.1).

Requirement 5.1: daily max temperature falls in the afternoon window (default
14:00-16:00 local).
Requirement 5.2: temperature within the profile range, clamped to the nearer bound.
Requirement 5.3: daily min-to-max temperature difference 10..20 °C.
Requirement 5.5/5.6: absolute pressure within the profile range at site elevation.
Determinism (§2): same (seed, SiteCode, timestamp) reproduces the same reading.
"""

from __future__ import annotations

import datetime as dt

from aqm_simulator.geography.profiles import get_profile
from aqm_simulator.rng.streams import Purpose, RandomStreamFactory
from aqm_simulator.signal.meteorology import MeteorologyEngine, MeteorologyRanges


def _engine(site: str = "CB0001", seed: int = 7) -> MeteorologyEngine:
    profile = get_profile("cochabamba")
    ranges = MeteorologyRanges.from_profile(profile)
    rng = RandomStreamFactory(seed=seed).stream(site, Purpose.METEOROLOGY)
    return MeteorologyEngine(ranges=ranges, timezone=profile.timezone, rng=rng)


def _hourly_temps_for_day(engine: MeteorologyEngine, day: dt.date) -> list[float]:
    temps = []
    for hour in range(24):
        ts = dt.datetime(day.year, day.month, day.day, hour, tzinfo=dt.UTC)
        temps.append(engine.reading(ts).temperature_c)
    return temps


def test_temperature_within_profile_range() -> None:
    engine = _engine()
    for hour in range(24):
        ts = dt.datetime(2026, 7, 1, hour, tzinfo=dt.UTC)
        temp = engine.reading(ts).temperature_c
        assert 5.0 <= temp <= 30.0


def test_daily_max_in_afternoon_window_local() -> None:
    # America/La_Paz is UTC-4; 14:00-16:00 local = 18:00-20:00 UTC.
    engine = _engine()
    temps = _hourly_temps_for_day(engine, dt.date(2026, 7, 1))
    max_hour_utc = max(range(24), key=lambda h: temps[h])
    # convert the UTC hour to local (UTC-4)
    local_hour = (max_hour_utc - 4) % 24
    assert 14 <= local_hour <= 16


def test_daily_diurnal_range_10_to_20() -> None:
    engine = _engine()
    temps = _hourly_temps_for_day(engine, dt.date(2026, 7, 1))
    diurnal = max(temps) - min(temps)
    assert 10.0 <= diurnal <= 20.0


def test_pressure_within_profile_range() -> None:
    engine = _engine()
    for hour in range(24):
        ts = dt.datetime(2026, 7, 1, hour, tzinfo=dt.UTC)
        assert 730.0 <= engine.reading(ts).pressure_hpa <= 755.0


def test_rh_within_0_100() -> None:
    engine = _engine()
    for hour in range(24):
        ts = dt.datetime(2026, 7, 1, hour, tzinfo=dt.UTC)
        assert 0.0 <= engine.reading(ts).relative_humidity_pct <= 100.0


def test_reading_is_deterministic() -> None:
    ts = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
    assert _engine().reading(ts) == _engine().reading(ts)


def test_reference_profile_pressure_range() -> None:
    profile = get_profile("reference")
    ranges = MeteorologyRanges.from_profile(profile)
    rng = RandomStreamFactory(seed=1).stream("RF0001", Purpose.METEOROLOGY)
    engine = MeteorologyEngine(ranges=ranges, timezone=profile.timezone, rng=rng)
    ts = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
    assert 980.0 <= engine.reading(ts).pressure_hpa <= 1040.0
