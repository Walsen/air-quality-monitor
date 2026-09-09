"""Unit tests for housekeeping telemetry (task 13.4).

Requirement 7.6: one record per sensor per interval with signal_quality int
0-100 and an active-fault list (subset of stuck value/drift/dropout, empty = none).
Requirement 7.7: battery state of charge int 0-100 present iff PowerTag == Solar.
Requirement 7.8: excluded iff PowerTag == Mains.
Requirement 7.9: distinct topic; payload carries none of the 9 Sensor_Data_Record
fields.
"""

from __future__ import annotations

import datetime as dt

import numpy as np

from aqm_simulator.rng.streams import Purpose, RandomStreamFactory
from aqm_simulator.signal.housekeeping import build_housekeeping, housekeeping_topic

_TS = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_DATA_FIELDS = {
    "Species", "Source", "Units", "SiteCode", "DateTime",
    "Duration", "ScaledValue", "RatificationStatus", "SensorContract",
}


def _rng(site: str = "CB0001", seed: int = 7) -> np.random.Generator:
    return RandomStreamFactory(seed=seed).stream(site, Purpose.ARTIFACTS)


def test_signal_quality_is_int_0_100() -> None:
    rec = build_housekeeping("CB0001", "Mains", [], _TS, _rng())
    assert isinstance(rec.signal_quality, int)
    assert 0 <= rec.signal_quality <= 100


def test_active_faults_empty_when_none() -> None:
    rec = build_housekeeping("CB0001", "Mains", [], _TS, _rng())
    assert rec.active_faults == []


def test_active_faults_listed() -> None:
    rec = build_housekeeping("CB0001", "Mains", ["dropout", "drift"], _TS, _rng())
    assert set(rec.active_faults) == {"dropout", "drift"}


def test_battery_present_iff_solar() -> None:
    solar = build_housekeeping("CB0001", "Solar", [], _TS, _rng())
    mains = build_housekeeping("CB0002", "Mains", [], _TS, _rng("CB0002"))
    assert solar.battery_soc is not None
    assert 0 <= solar.battery_soc <= 100
    assert mains.battery_soc is None


def test_payload_has_no_contract_fields() -> None:
    rec = build_housekeeping("CB0001", "Solar", ["stuck value"], _TS, _rng())
    payload = rec.to_payload()
    assert _DATA_FIELDS.isdisjoint(payload.keys())


def test_topic_distinct_from_measurement() -> None:
    assert housekeeping_topic("CB0001") == "aqm/sensors/CB0001/housekeeping"
    assert housekeeping_topic("CB0001") != "aqm/sensors/CB0001/data"


def test_deterministic() -> None:
    a = build_housekeeping("CB0001", "Solar", [], _TS, _rng())
    b = build_housekeeping("CB0001", "Solar", [], _TS, _rng())
    assert a == b
