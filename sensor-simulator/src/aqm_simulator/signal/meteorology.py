"""Meteorology signals: temperature, relative humidity, and station pressure.

The :class:`MeteorologyEngine` computes a per-Tick reading for one Virtual_Sensor
as a pure function of the simulated timestamp and a fixed set of per-sensor
parameters drawn once from the injected random stream. Because those parameters
are fixed at construction, ``reading`` is order-independent and deterministic —
no ``datetime.now()`` and no module-level randomness (engineering-practices §2).

Behaviour (Requirement 5):
- daily maximum temperature falls in the afternoon window, default 14:00-16:00
  in the profile's local timezone (5.1), modelled as a diurnal cosine peaking
  at 15:00 local;
- the daily min-to-max swing is 10..20 °C (5.3), and every value is clamped to
  the profile temperature range (5.2);
- relative humidity tracks temperature inversely (5.4) — high at the cold hours,
  low at the warm hours — clamped to the profile RH range and to 0..100;
- barometric pressure is absolute station pressure in the profile range (5.5,
  5.6), which for cochabamba's ~2560 m sits near 740 hPa.

The engine receives a narrow :class:`MeteorologyRanges`, not the whole config
(§1 interface segregation): it has no use for MQTT or REST settings.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import numpy as np

from aqm_simulator.geography.profiles import GeographyProfile

# Local hour at which temperature peaks (centre of the 14:00-16:00 window).
_PEAK_LOCAL_HOUR = 15.0


@dataclass(frozen=True, slots=True)
class MeteorologyRanges:
    """The narrow meteorology inputs the engine needs from a profile."""

    temp_min_c: float
    temp_max_c: float
    rh_min_pct: float
    rh_max_pct: float
    pressure_min_hpa: float
    pressure_max_hpa: float
    max_diurnal_range_c: float = 20.0

    @classmethod
    def from_profile(cls, profile: GeographyProfile) -> MeteorologyRanges:
        return cls(
            temp_min_c=profile.temp_min_c,
            temp_max_c=profile.temp_max_c,
            rh_min_pct=profile.rh_min_pct,
            rh_max_pct=profile.rh_max_pct,
            pressure_min_hpa=profile.pressure_min_hpa,
            pressure_max_hpa=profile.pressure_max_hpa,
        )


@dataclass(frozen=True, slots=True)
class MeteorologyReading:
    """One meteorology sample for a Virtual_Sensor at a simulated instant."""

    temperature_c: float
    relative_humidity_pct: float
    pressure_hpa: float


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class MeteorologyEngine:
    """Computes temperature / RH / pressure for one sensor, deterministically."""

    def __init__(
        self,
        ranges: MeteorologyRanges,
        timezone: str,
        rng: np.random.Generator,
    ) -> None:
        self._ranges = ranges
        self._tz = ZoneInfo(timezone)

        # Fixed per-sensor parameters drawn once, so reading() is a pure
        # function of the timestamp thereafter.
        span = ranges.temp_max_c - ranges.temp_min_c
        # diurnal amplitude gives a peak-to-trough swing in [10, min(20, span)]
        max_swing = min(ranges.max_diurnal_range_c, span)
        low_swing = min(10.0, max_swing)
        swing = float(rng.uniform(low_swing, max_swing))
        self._amplitude = swing / 2.0
        # centre the daily mean so peak and trough stay within the range
        headroom_lo = ranges.temp_min_c + self._amplitude
        headroom_hi = ranges.temp_max_c - self._amplitude
        self._mean_temp = float(rng.uniform(headroom_lo, headroom_hi))
        # pressure: a fixed offset within the range (station pressure varies little)
        self._pressure = float(rng.uniform(ranges.pressure_min_hpa, ranges.pressure_max_hpa))

    def _local_hour(self, when: dt.datetime) -> float:
        local = when.astimezone(self._tz)
        return local.hour + local.minute / 60.0

    def reading(self, when: dt.datetime) -> MeteorologyReading:
        """Compute the reading at ``when`` (a timezone-aware UTC instant)."""
        hour = self._local_hour(when)
        # cosine peaking at _PEAK_LOCAL_HOUR: phase 0 at the peak.
        phase = 2.0 * math.pi * (hour - _PEAK_LOCAL_HOUR) / 24.0
        diurnal = math.cos(phase)  # +1 at peak (15:00), -1 twelve hours later

        temp = self._mean_temp + self._amplitude * diurnal
        temp = _clamp(temp, self._ranges.temp_min_c, self._ranges.temp_max_c)

        # RH inversely tracks temperature: high when diurnal is low.
        rh_span = self._ranges.rh_max_pct - self._ranges.rh_min_pct
        rh = self._ranges.rh_min_pct + rh_span * (1.0 - (diurnal + 1.0) / 2.0)
        rh = _clamp(rh, max(0.0, self._ranges.rh_min_pct), min(100.0, self._ranges.rh_max_pct))

        return MeteorologyReading(
            temperature_c=temp,
            relative_humidity_pct=rh,
            pressure_hpa=_clamp(
                self._pressure, self._ranges.pressure_min_hpa, self._ranges.pressure_max_hpa
            ),
        )
