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
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import numpy as np

from aqm_simulator.observability.logging import get_logger
from aqm_simulator.rng.streams import Purpose, RandomStreamFactory
from aqm_simulator.signal.regional_field import RegionalField
from aqm_simulator.signal.spatial_field import SpatialField

_logger = get_logger("signal.pollutants")

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


# Maximum plausible concentrations (Requirement 4.11).
_MAX_PLAUSIBLE = {"PM25": 500.0, "NO2": 400.0}

# Per-site local PM2.5 modifier amplitude — small relative to the regional
# baseline so the shared Regional_Field accounts for >=60% of hourly variance
# (Requirement 9.1 / Property 27), and a weak diurnal term so PM2.5's relative
# diurnal amplitude stays <=0.5x NO2's (Requirement 4.4).
_LOCAL_MODIFIER_MAX = 2.0  # µg/m³ bounded per-site offset magnitude
_PM_DIURNAL_AMPLITUDE = 1.0  # µg/m³
# Shared city-wide diurnal phase (radians). City-wide, not per site, so the
# diurnal term stays part of the shared component of Requirement 9.1.
_PM_DIURNAL_PHASE = 0.0


@dataclass(frozen=True, slots=True)
class ClampEvent:
    """A recorded clamp of an out-of-range value (diagnostic output)."""

    site_code: str
    species: str
    when: dt.datetime
    raw: float
    clamped: float


class PM25Signal:
    """PM2.5 dry concentration = shared regional baseline + per-site local part."""

    def __init__(
        self,
        regional: RegionalField,
        site_code: str,
        factory: RandomStreamFactory,
        latitude: float | None = None,
        longitude: float | None = None,
        spatial: SpatialField | None = None,
    ) -> None:
        self._regional = regional
        self._site = site_code
        rng = factory.stream(site_code, Purpose.SIGNAL)
        # Local PM2.5 component. When a shared SpatialField and this sensor's
        # location are supplied, the local part is the field sampled at (lat,lon)
        # — SMOOTH in space, so nearby sensors share it and correlate (Req 9.2)
        # while distant ones decorrelate (Req 9.3). Otherwise fall back to a
        # BOUNDED independent offset (keeps the clean-range guarantee, Req 4.5).
        self._spatial = spatial
        self._lat = latitude
        self._lon = longitude
        self._local_offset = float(rng.uniform(-_LOCAL_MODIFIER_MAX, _LOCAL_MODIFIER_MAX))
        self._clamp_events: list[ClampEvent] = []

    def _local_component(self, when: dt.datetime) -> float:
        if self._spatial is not None and self._lat is not None and self._lon is not None:
            return self._spatial.sample(self._lat, self._lon, when)
        return self._local_offset

    def dry_value(self, when: dt.datetime) -> float:
        """PM2.5 dry concentration (µg/m³) before the humidity artifact."""
        baseline = self._regional.baseline(when)
        hour = when.hour + when.minute / 60.0
        # The diurnal cycle (traffic and boundary-layer driven) is CITY-WIDE, so
        # its phase is shared by the whole swarm. Drawing it per site put a large
        # distance-independent term in the signal, which broke the shared/local
        # split of Requirement 9.1 and the spatial decay of 9.3: two sensors then
        # correlated by how close their random phases fell, not by separation.
        diurnal = _PM_DIURNAL_AMPLITUDE * math.sin(
            2 * math.pi * hour / 24.0 + _PM_DIURNAL_PHASE
        )
        return max(0.0, baseline + self._local_component(when) + diurnal)

    def clamp(self, species: str, value: float, site_code: str, when: dt.datetime) -> float:
        """Clamp a value to 0..max_plausible, recording+logging any clamp (Req 4.11)."""
        upper = _MAX_PLAUSIBLE[species]
        clamped = max(0.0, min(upper, value))
        if clamped != value:
            self._clamp_events.append(
                ClampEvent(
                    site_code=site_code, species=species, when=when, raw=value, clamped=clamped
                )
            )
            _logger.warning(
                "value_clamped",
                site_code=site_code,
                species=species,
                timestamp=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                raw=value,
                clamped=clamped,
            )
        return clamped

    def clamp_events(self) -> list[ClampEvent]:
        """Return the recorded clamp events (diagnostic output only)."""
        return list(self._clamp_events)
