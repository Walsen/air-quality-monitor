"""Housekeeping telemetry property test (task 13.5).

Feature: sensor-simulator-service, Property 22 — housekeeping telemetry shape
(Req 7.6, 7.7, 7.8).
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given, settings
from hypothesis import strategies as st

from aqm_simulator.rng.streams import Purpose, RandomStreamFactory
from aqm_simulator.signal.housekeeping import build_housekeeping

_TS = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_FAULTS = ["stuck value", "drift", "dropout"]
_DATA_FIELDS = {
    "Species", "Source", "Units", "SiteCode", "DateTime",
    "Duration", "ScaledValue", "RatificationStatus", "SensorContract",
}


@given(
    seed=st.integers(min_value=0, max_value=10_000),
    power_tag=st.sampled_from(["Solar", "Mains"]),
    faults=st.lists(st.sampled_from(_FAULTS), unique=True, max_size=3),
)
@settings(max_examples=100)
def test_property_22_housekeeping_shape(seed: int, power_tag: str, faults: list[str]) -> None:
    """Feature: sensor-simulator-service, Property 22."""
    rng = RandomStreamFactory(seed=seed).stream("CB0001", Purpose.ARTIFACTS)
    rec = build_housekeeping("CB0001", power_tag, faults, _TS, rng)
    assert isinstance(rec.signal_quality, int) and 0 <= rec.signal_quality <= 100
    assert set(rec.active_faults) <= set(_FAULTS)
    if power_tag == "Solar":
        assert rec.battery_soc is not None and 0 <= rec.battery_soc <= 100
    else:
        assert rec.battery_soc is None
    assert _DATA_FIELDS.isdisjoint(rec.to_payload().keys())
