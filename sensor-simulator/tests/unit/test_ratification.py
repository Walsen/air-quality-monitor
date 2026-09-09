"""Unit tests for ratification-status derivation (task 2.7).

Requirement 2.8: while age (reference time - DateTime) < Ratification_Lag
(default 90 days), status is 'P'.
Requirement 2.9: while age >= Ratification_Lag, status is 'R'.
The reference time is a parameter (no datetime.now() in domain code).
"""

from __future__ import annotations

import datetime as dt

from aqm_simulator.contract.ratification import DEFAULT_RATIFICATION_LAG, derive_ratification_status


def _utc(y: int, mo: int, d: int) -> dt.datetime:
    return dt.datetime(y, mo, d, tzinfo=dt.UTC)


def test_recent_record_is_provisional() -> None:
    record_dt = _utc(2026, 7, 1)
    ref = _utc(2026, 7, 2)  # 1 day old
    assert derive_ratification_status(record_dt, ref) == "P"


def test_old_record_is_ratified() -> None:
    record_dt = _utc(2026, 1, 1)
    ref = _utc(2026, 7, 1)  # ~181 days old
    assert derive_ratification_status(record_dt, ref) == "R"


def test_boundary_at_exactly_lag_is_ratified() -> None:
    # age >= lag -> 'R' (Req 2.9, boundary inclusive on the R side)
    record_dt = _utc(2026, 1, 1)
    ref = record_dt + DEFAULT_RATIFICATION_LAG
    assert derive_ratification_status(record_dt, ref) == "R"


def test_just_below_lag_is_provisional() -> None:
    record_dt = _utc(2026, 1, 1)
    ref = record_dt + DEFAULT_RATIFICATION_LAG - dt.timedelta(seconds=1)
    assert derive_ratification_status(record_dt, ref) == "P"


def test_custom_lag_respected() -> None:
    record_dt = _utc(2026, 6, 1)
    ref = _utc(2026, 6, 15)  # 14 days
    lag = dt.timedelta(days=7)
    assert derive_ratification_status(record_dt, ref, lag=lag) == "R"
