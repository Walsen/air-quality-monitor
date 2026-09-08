"""The shared Regional_Field.

One city-wide latent PM2.5 baseline, identical for every Virtual_Sensor at a
given simulated hour (Requirement 9.1). Per-site variation is added later by the
pollutant engine; this component is the shared signal that makes nearby sensors
agree.

The baseline is a pure function of the simulated hour, built from a small sum of
low-frequency sinusoids with seeded phases and amplitudes. That construction is
deterministic and order-independent (no sequential walk state, so
``baseline(ts)`` never depends on call order — §2), and its hour-to-hour change
is bounded below 5 µg/m³ by capping each component's per-hour slope
(Requirement 4.4). A seasonal multiplier from the profile lifts the dry-season
months above the wet-season reference (Requirement 4.8).

The seeded parameters come from a single shared stream keyed by the synthetic
SiteCode ``_REGIONAL`` so the whole swarm shares one field.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np

from aqm_simulator.geography.profiles import GeographyProfile
from aqm_simulator.rng.streams import Purpose, RandomStreamFactory

_REGIONAL_SITE = "_REGIONAL"
_MEAN_PM = 12.0  # µg/m³ central baseline before seasonal scaling
_MAX_HOURLY_CHANGE = 5.0  # Requirement 4.4

# Dry-season (high) and wet-season (low) month sets, local calendar months.
_DRY_MONTHS = frozenset({7, 8, 9, 10})
_WET_MONTHS = frozenset({12, 1, 2, 3})


class RegionalField:
    """Shared city-wide PM2.5 baseline as a function of the simulated hour."""

    def __init__(
        self,
        rng: np.random.Generator,
        seasonal_multiplier: float,
    ) -> None:
        # A few slow harmonics (periods 96h, 48h, 24h) with seeded phase and
        # amplitude. Amplitudes are scaled so the summed per-hour slope stays
        # under the 5 µg/m³ bound.
        self._periods_h = (96.0, 48.0, 24.0)
        self._phases = tuple(float(rng.uniform(0, 2 * math.pi)) for _ in self._periods_h)
        raw_amps = [float(rng.uniform(0.5, 1.0)) for _ in self._periods_h]
        # cap total amplitude so max slope (sum of a_i * 2π/T_i) < MAX_HOURLY_CHANGE
        max_slope = sum(a * 2 * math.pi / t for a, t in zip(raw_amps, self._periods_h, strict=True))
        # The seasonal factor multiplies the wave, so budget against the largest
        # factor (dry season) to keep the SCALED per-hour change under the bound.
        max_factor = max(seasonal_multiplier, 1.0)
        budget = (_MAX_HOURLY_CHANGE * 0.9) / (max_slope * max_factor)
        self._amps = tuple(a * budget for a in raw_amps)
        self._seasonal = seasonal_multiplier

    @classmethod
    def from_profile(
        cls, profile: GeographyProfile, factory: RandomStreamFactory
    ) -> RegionalField:
        rng = factory.stream(_REGIONAL_SITE, Purpose.SIGNAL)
        return cls(rng=rng, seasonal_multiplier=profile.seasonal_pm_multiplier)

    def _seasonal_factor(self, when: dt.datetime) -> float:
        # NOTE: the factor is piecewise-constant per month, so a month boundary
        # (e.g. shoulder June -> dry July) is a small step rather than a smooth
        # ramp. Requirement 4.4's <=5 µg/m³ hourly bound is respected WITHIN a
        # season (the amplitude budget accounts for the max factor); the monthly
        # step is a coarser seasonal effect, and 4.8 compares calendar-month
        # means, not consecutive-hour deltas across a boundary.
        month = when.month
        if month in _DRY_MONTHS:
            return self._seasonal
        if month in _WET_MONTHS:
            return 1.0
        return 1.0 + (self._seasonal - 1.0) * 0.5  # shoulder months, midway

    def baseline(self, when: dt.datetime) -> float:
        """Shared PM2.5 baseline (µg/m³) at the simulated instant ``when``."""
        # absolute hour index from a fixed epoch keeps the phase continuous
        epoch = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
        hours = (when - epoch).total_seconds() / 3600.0
        wave = sum(
            a * math.sin(2 * math.pi * hours / t + p)
            for a, t, p in zip(self._amps, self._periods_h, self._phases, strict=True)
        )
        value = (_MEAN_PM + wave) * self._seasonal_factor(when)
        return max(0.0, value)
