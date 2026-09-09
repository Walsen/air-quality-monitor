"""NO2 signal property tests (tasks 9.5, 9.6).

Feature: sensor-simulator-service
- Property 11: NO2 rush-hour diurnal shape (Req 4.1, 4.2)
- Property 12: NO2 classification ordering (Req 4.3)
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given, settings
from hypothesis import strategies as st

from aqm_simulator.rng.streams import Purpose, RandomStreamFactory
from aqm_simulator.signal.pollutants import NO2Signal

_TZ = "America/La_Paz"


def _sig(classification: str, seed: int, site: str = "CB0001") -> NO2Signal:
    rng = RandomStreamFactory(seed=seed).stream(site, Purpose.SIGNAL)
    return NO2Signal(classification=classification, timezone=_TZ, rng=rng)


def _hourly_local(sig: NO2Signal, start: dt.datetime, hours: int) -> list[tuple[int, float]]:
    out = []
    for h in range(hours):
        ts = start + dt.timedelta(hours=h)
        local = ts.astimezone(__import__("zoneinfo").ZoneInfo(_TZ))
        out.append((local.hour, sig.value(ts)))
    return out


@given(seed=st.integers(min_value=0, max_value=10_000))
@settings(max_examples=100)
def test_property_11_rush_hour_diurnal_shape(seed: int) -> None:
    """Feature: sensor-simulator-service, Property 11."""
    sig = _sig("Roadside", seed)
    start = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    series = _hourly_local(sig, start, 72)
    rush = [v for lh, v in series if lh in {7, 8, 9, 17, 18, 19}]
    overnight = [v for lh, v in series if lh in {0, 1, 2, 3}]
    rush_mean = sum(rush) / len(rush)
    overnight_mean = sum(overnight) / len(overnight)
    # rush-hour mean between 1.3 and 4.0x the overnight mean (Req 4.2)
    ratio = rush_mean / overnight_mean
    assert 1.3 <= ratio <= 4.0


@given(seed=st.integers(min_value=0, max_value=10_000))
@settings(max_examples=100)
def test_property_12_classification_ordering(seed: int) -> None:
    """Feature: sensor-simulator-service, Property 12."""
    start = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)

    def mean(classification: str) -> float:
        series = _hourly_local(_sig(classification, seed), start, 72)
        return sum(v for _, v in series) / len(series)

    road, urban, sub = mean("Roadside"), mean("Urban Background"), mean("Suburban")
    assert road >= 1.25 * urban
    assert urban >= 1.15 * sub
