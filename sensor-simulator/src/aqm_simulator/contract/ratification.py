"""Ratification-status derivation.

A Sensor_Data_Record is ``P`` (provisional) while its age is below the
Ratification_Lag and ``R`` (ratified) once its age reaches it (Requirements 2.8,
2.9). Age is ``reference_time - DateTime``; the reference time is passed in, so
this stays pure and testable — no ``datetime.now()`` in domain code
(engineering-practices §2).
"""

from __future__ import annotations

import datetime as dt

from aqm_simulator.contract.records import RatificationStatusName

# Requirement 2.8/2.9 default Ratification_Lag.
DEFAULT_RATIFICATION_LAG = dt.timedelta(days=90)


def derive_ratification_status(
    record_datetime: dt.datetime,
    reference_time: dt.datetime,
    lag: dt.timedelta = DEFAULT_RATIFICATION_LAG,
) -> RatificationStatusName:
    """Return ``'P'`` while age < lag, ``'R'`` once age >= lag."""
    age = reference_time - record_datetime
    return "R" if age >= lag else "P"
