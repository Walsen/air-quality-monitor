"""Unit tests for PM2.5 composition (task 9.7).

Requirement 9.1: PM2.5 dry concentration = shared Regional_Field + per-site local
modifier from the site's own stream.
Requirement 4.5: clean-scenario hourly PM2.5 within 3..35 µg/m³.
Requirement 4.7: never negative after rounding.
Requirement 4.11: a value outside 0..max (default 500) is clamped to the nearer
bound and the clamp event recorded with SiteCode/Species/timestamp.
"""

from __future__ import annotations

import datetime as dt

from aqm_simulator.geography.profiles import get_profile
from aqm_simulator.rng.streams import RandomStreamFactory
from aqm_simulator.signal.pollutants import PM25Signal
from aqm_simulator.signal.regional_field import RegionalField


def _pm25(site: str = "CB0001", seed: int = 7) -> PM25Signal:
    profile = get_profile("cochabamba")
    factory = RandomStreamFactory(seed=seed)
    field = RegionalField.from_profile(profile, factory)
    return PM25Signal(regional=field, site_code=site, factory=factory)


def _series(sig: PM25Signal, start: dt.datetime, hours: int) -> list[float]:
    return [sig.dry_value(start + dt.timedelta(hours=h)) for h in range(hours)]


def test_pm25_non_negative() -> None:
    series = _series(_pm25(), dt.datetime(2026, 7, 1, tzinfo=dt.UTC), 72)
    assert all(v >= 0 for v in series)


def test_pm25_clean_range_3_to_35() -> None:
    # over a wet-season window (no dry-season lift) values stay in the clean band
    series = _series(_pm25(), dt.datetime(2026, 1, 15, tzinfo=dt.UTC), 72)
    assert all(3.0 <= v <= 35.0 for v in series), (min(series), max(series))


def test_pm25_deterministic() -> None:
    ts = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
    assert _pm25().dry_value(ts) == _pm25().dry_value(ts)


def test_two_sites_share_regional_but_differ_locally() -> None:
    ts = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
    a = _pm25("CB0001").dry_value(ts)
    b = _pm25("CB0002").dry_value(ts)
    assert a != b  # per-site modifier differs


def test_clamp_records_event() -> None:
    sig = _pm25()
    ts = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
    clamped = sig.clamp("PM25", 999.0, site_code="CB0001", when=ts)
    assert clamped == 500.0
    events = sig.clamp_events()
    assert len(events) == 1
    assert events[0].site_code == "CB0001"
    assert events[0].species == "PM25"


def test_clamp_low_to_zero_recorded() -> None:
    sig = _pm25()
    ts = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
    assert sig.clamp("PM25", -5.0, site_code="CB0001", when=ts) == 0.0
    assert len(sig.clamp_events()) == 1
