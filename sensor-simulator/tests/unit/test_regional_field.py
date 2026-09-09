"""Unit tests for the Regional_Field (task 9.1).

Requirement 9.1: one shared Regional_Field PM2.5 component identical for every
Virtual_Sensor at a given simulated hour.
Requirement 4.4: the baseline changes by at most 5 µg/m³ between consecutive
simulated hours.
Requirement 4.8: a seasonal PM2.5 multiplier from the profile scales the
baseline (dry-season Jul-Oct higher than wet-season Dec-Mar).
Determinism (§2): same seed reproduces the same baseline series.
"""

from __future__ import annotations

import datetime as dt
from itertools import pairwise

from aqm_simulator.geography.profiles import get_profile
from aqm_simulator.rng.streams import RandomStreamFactory
from aqm_simulator.signal.regional_field import RegionalField


def _field(seed: int = 7, profile_name: str = "cochabamba") -> RegionalField:
    profile = get_profile(profile_name)
    return RegionalField.from_profile(profile, RandomStreamFactory(seed=seed))


def _hourly(field: RegionalField, start: dt.datetime, hours: int) -> list[float]:
    return [field.baseline(start + dt.timedelta(hours=h)) for h in range(hours)]


def test_baseline_changes_at_most_5_per_hour() -> None:
    series = _hourly(_field(), dt.datetime(2026, 7, 1, tzinfo=dt.UTC), 72)
    for a, b in pairwise(series):
        assert abs(b - a) <= 5.0 + 1e-9


def test_baseline_deterministic_for_seed() -> None:
    start = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    assert _hourly(_field(seed=3), start, 24) == _hourly(_field(seed=3), start, 24)


def test_baseline_is_non_negative() -> None:
    series = _hourly(_field(), dt.datetime(2026, 7, 1, tzinfo=dt.UTC), 72)
    assert all(v >= 0 for v in series)


def test_dry_season_baseline_exceeds_wet_season() -> None:
    # Req 4.8: Jul-Oct (dry) mean baseline higher than Dec-Mar (wet).
    field = _field()
    dry = _hourly(field, dt.datetime(2026, 8, 1, tzinfo=dt.UTC), 24 * 7)
    wet = _hourly(field, dt.datetime(2026, 1, 15, tzinfo=dt.UTC), 24 * 7)
    dry_mean = sum(dry) / len(dry)
    wet_mean = sum(wet) / len(wet)
    ratio = dry_mean / wet_mean
    assert 1.5 <= ratio <= 3.0


def test_baseline_identical_regardless_of_caller() -> None:
    # The Regional_Field is shared: two references to the same field at the same
    # hour give the same value (it does not depend on a SiteCode).
    field = _field(seed=11)
    ts = dt.datetime(2026, 7, 1, 5, tzinfo=dt.UTC)
    assert field.baseline(ts) == field.baseline(ts)
