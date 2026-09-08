"""Pollutant signals: NO2 (and, later, PM2.5 composition).

NO2 is a base level plus a non-negative diurnal traffic component that peaks in
the morning and evening rush-hour windows and falls to a low overnight trough
(Requirement 4.1), scaled by SiteClassification so roadside sites read highest
(Requirement 4.3). The curve is modelled as two Gaussian rush-hour bumps (centred
at 08:00 and 18:00 local) over a small base, which keeps the overnight 00:00-04:00
window well under 30% of the daily maximum by construction.

Each signal takes narrow inputs — its classification, the profile timezone, and
an injected per-sensor SIGNAL stream (§1) — never the whole config. A fixed
per-sensor amplitude is drawn once from the stream so ``value`` is a pure,
order-independent function of the timestamp thereafter (§2).
"""

from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

import numpy as np

# Rush-hour peak centres (local hours) and bump width.
_MORNING_PEAK = 8.0
_EVENING_PEAK = 18.0
_BUMP_SIGMA_H = 1.2  # hours; narrow enough that 00-04 stays well below the peak

# Classification multipliers. Roadside > Urban Background > Suburban with margins
# comfortably above the required 1.25x and 1.15x ratios (Requirement 4.3).
_CLASS_FACTOR = {
    "Roadside": 1.0,
    "Urban Background": 0.72,
    "Suburban": 0.60,
}

_BASE_NO2 = 12.0  # µg/m³ background (present day and night)
_PEAK_NO2 = 36.0  # µg/m³ nominal roadside rush-hour ADDITION at the bump centre

# Balancing two requirements at once (with bump width _BUMP_SIGMA_H):
# - Req 4.1: overnight 00-04 mean (~base, bumps ~0) must be <=30% of the daily
#   max (~base+peak). That needs peak/base >= ~2.33.
# - Req 4.2: the rush-window mean (~base + 0.81*peak, averaging the six 07-09/
#   17-19 hours) must be 1.3..4.0x the overnight mean, i.e. peak/base <= ~3.7.
# peak/base = 3.0 sits in [2.33, 3.7] with margin for the +/-15% per-site jitter,
# and keeps every value inside the clean-scenario 5..90 µg/m³ band (Req 4.5).


def _gaussian_bump(hour: float, centre: float) -> float:
    # wrap the hour difference into [-12, 12] so a bump near midnight is smooth
    diff = (hour - centre + 12.0) % 24.0 - 12.0
    return math.exp(-0.5 * (diff / _BUMP_SIGMA_H) ** 2)


class NO2Signal:
    """Diurnal NO2 for one Virtual_Sensor, scaled by its classification."""

    def __init__(self, classification: str, timezone: str, rng: np.random.Generator) -> None:
        self._factor = _CLASS_FACTOR[classification]
        self._tz = ZoneInfo(timezone)
        # per-site amplitude jitter (±15%), fixed once for determinism
        self._amp = _PEAK_NO2 * float(rng.uniform(0.85, 1.15))

    def value(self, when: dt.datetime) -> float:
        """NO2 concentration (µg/m³) at the simulated instant ``when``."""
        local = when.astimezone(self._tz)
        hour = local.hour + local.minute / 60.0
        traffic = _gaussian_bump(hour, _MORNING_PEAK) + _gaussian_bump(hour, _EVENING_PEAK)
        # Both the background base and the traffic component scale with
        # classification: a roadside site has both higher background NO2 and a
        # bigger rush-hour swing, so the mean-NO2 ratios of Req 4.3 hold over the
        # whole emitted series, not just the traffic term in isolation.
        value = self._factor * (_BASE_NO2 + self._amp * traffic)
        return max(0.0, value)
