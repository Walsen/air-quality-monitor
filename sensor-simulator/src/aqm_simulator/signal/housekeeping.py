"""Housekeeping telemetry.

One housekeeping record per Virtual_Sensor per Publish_Interval carries a
signal-quality value and the active-fault list (Requirement 7.6), plus a battery
state of charge for solar-powered devices only (Requirements 7.7, 7.8). It is
published to a topic DISTINCT from the measurement topic and its payload carries
none of the nine Sensor_Data_Record fields, so the measurement contract stays
unchanged and the REST_API can exclude it (Requirement 7.9).

Values are drawn from the sensor's injected ARTIFACTS stream so the record is
deterministic (§2).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import numpy as np

_HOUSEKEEPING_TOPIC = "aqm/sensors/{SiteCode}/housekeeping"


def housekeeping_topic(site_code: str) -> str:
    """The housekeeping topic for a sensor — distinct from its data topic."""
    return _HOUSEKEEPING_TOPIC.replace("{SiteCode}", site_code)


@dataclass(frozen=True, slots=True)
class HousekeepingRecord:
    """A housekeeping telemetry record (not a Sensor_Data_Record)."""

    site_code: str
    timestamp: str
    signal_quality: int
    active_faults: list[str]
    battery_soc: int | None  # present iff PowerTag == Solar

    def to_payload(self) -> dict[str, Any]:
        """Serialisable payload carrying no Sensor_Data_Record fields (Req 7.9).

        The nine measurement fields (SiteCode, Species, ... — Requirement 2.1)
        are all avoided here: the sensor is identified by ``Device`` and the
        instant by ``Timestamp``, so a consumer can never confuse a housekeeping
        payload with a measurement one.
        """
        payload: dict[str, Any] = {
            "Device": self.site_code,
            "Timestamp": self.timestamp,
            "SignalQuality": self.signal_quality,
            "ActiveFaults": list(self.active_faults),
        }
        if self.battery_soc is not None:
            payload["BatterySoC"] = self.battery_soc
        return payload


def build_housekeeping(
    site_code: str,
    power_tag: str,
    active_faults: list[str],
    when: dt.datetime,
    rng: np.random.Generator,
) -> HousekeepingRecord:
    """Build one housekeeping record for a sensor and interval."""
    # signal quality degrades when faults are active — still 0..100.
    base_quality = int(rng.integers(85, 101))
    quality = max(0, base_quality - 30 * len(active_faults))
    battery = int(rng.integers(40, 101)) if power_tag == "Solar" else None
    return HousekeepingRecord(
        site_code=site_code,
        timestamp=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        signal_quality=quality,
        active_faults=list(active_faults),
        battery_soc=battery,
    )
