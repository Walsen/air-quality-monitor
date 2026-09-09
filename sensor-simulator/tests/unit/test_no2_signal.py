"""Unit tests for the NO2 signal (task 9.4).

Requirement 4.1: NO2 = base + non-negative diurnal traffic peaking in the
rush-hour windows (07-09, 17-19 local), staying <=30% of the daily max in the
00-04 local window.
Requirement 4.3: classification scaling — Roadside >= 1.25x Urban Background,
Urban Background >= 1.15x Suburban (mean over a window).
Determinism (§2): same (seed, SiteCode, hour) reproduces the value.
"""

from __future__ import annotations

import datetime as dt

from aqm_simulator.rng.streams import Purpose, RandomStreamFactory
from aqm_simulator.signal.pollutants import NO2Signal

_TZ = "America/La_Paz"


def _no2(site: str, classification: str, seed: int = 7) -> NO2Signal:
    rng = RandomStreamFactory(seed=seed).stream(site, Purpose.SIGNAL)
    return NO2Signal(classification=classification, timezone=_TZ, rng=rng)


def _day(sig: NO2Signal, day: dt.date) -> dict[int, float]:
    # returns {utc_hour: value}
    return {
        h: sig.value(dt.datetime(day.year, day.month, day.day, h, tzinfo=dt.UTC))
        for h in range(24)
    }


def _local_hours(values: dict[int, float], local_hours: set[int]) -> list[float]:
    # America/La_Paz is UTC-4: local = (utc - 4) % 24
    return [v for utc_h, v in values.items() if (utc_h - 4) % 24 in local_hours]


def test_no2_non_negative() -> None:
    sig = _no2("CB0001", "Roadside")
    assert all(v >= 0 for v in _day(sig, dt.date(2026, 7, 1)).values())


def test_daily_max_in_rush_windows() -> None:
    sig = _no2("CB0001", "Roadside")
    values = _day(sig, dt.date(2026, 7, 1))
    max_utc = max(values, key=lambda h: values[h])
    local = (max_utc - 4) % 24
    assert local in {7, 8, 9, 17, 18, 19}


def test_overnight_at_most_30pct_of_daily_max() -> None:
    sig = _no2("CB0001", "Roadside")
    values = _day(sig, dt.date(2026, 7, 1))
    daily_max = max(values.values())
    overnight = _local_hours(values, {0, 1, 2, 3, 4})
    assert max(overnight) <= 0.30 * daily_max + 1e-9


def test_classification_ordering() -> None:
    # Same seed/site geometry, different classification -> ordered means.
    start = dt.date(2026, 7, 1)
    road = sum(_day(_no2("CB0001", "Roadside"), start).values())
    urban = sum(_day(_no2("CB0001", "Urban Background"), start).values())
    sub = sum(_day(_no2("CB0001", "Suburban"), start).values())
    assert road >= 1.25 * urban
    assert urban >= 1.15 * sub


def test_deterministic() -> None:
    ts = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
    assert _no2("CB0001", "Roadside").value(ts) == _no2("CB0001", "Roadside").value(ts)
