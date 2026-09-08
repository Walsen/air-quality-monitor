"""The shared spatial local field.

The Regional_Field gives one city-wide signal; the :class:`SpatialField` adds
the *local* PM2.5 structure that varies across the city but SMOOTHLY, so two
sensors close together see nearly the same local component and two far apart see
different ones. That is what produces the spatial correlation and distance decay
the spec requires (Requirements 9.2, 9.3): near pairs correlate strongly, and
correlation falls with separation.

It is modelled as a small sum of low-spatial-frequency 2D harmonics with
time-varying phase — smooth in space (wavelengths far larger than the 2 km
"near" threshold, so a <2 km pair is almost perfectly correlated) and evolving
hour to hour so each site has a real time series. Seeded from the shared
``_SPATIAL`` stream, it is deterministic and identical for the whole swarm.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np

from aqm_simulator.rng.streams import Purpose, RandomStreamFactory

_SPATIAL_SITE = "_SPATIAL"
_EPOCH = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)

# Spatial wavelengths in DEGREES. The Kanata bbox spans ~0.2° lat / 0.4° lon
# (~23 km / ~42 km). Wavelengths of ~0.4-0.8° keep the field near-constant over
# a 2 km (~0.018°) separation, so near pairs correlate >= 0.6 while 10 km+ pairs
# decorrelate — giving the distance decay of Requirement 9.3.
_SPATIAL_WAVELENGTHS_DEG = (0.8, 0.5)
# Temporal periods (hours) so each site's local component is a real time series.
_TEMPORAL_PERIODS_H = (30.0, 42.0)


class SpatialField:
    """A smooth, time-evolving 2D local PM2.5 field shared by the swarm."""

    def __init__(self, rng: np.random.Generator, amplitude: float) -> None:
        self._amplitude = amplitude
        # random spatial orientation + phase and temporal phase per harmonic
        self._components = []
        for wl_deg, period_h in zip(_SPATIAL_WAVELENGTHS_DEG, _TEMPORAL_PERIODS_H, strict=True):
            theta = float(rng.uniform(0, 2 * math.pi))  # spatial direction
            kx = math.cos(theta) * 2 * math.pi / wl_deg
            ky = math.sin(theta) * 2 * math.pi / wl_deg
            spatial_phase = float(rng.uniform(0, 2 * math.pi))
            omega = 2 * math.pi / period_h
            self._components.append((kx, ky, spatial_phase, omega))

    @classmethod
    def build(cls, factory: RandomStreamFactory, amplitude: float) -> SpatialField:
        return cls(factory.stream(_SPATIAL_SITE, Purpose.SIGNAL), amplitude)

    def sample(self, lat: float, lon: float, when: dt.datetime) -> float:
        """Local PM2.5 component (µg/m³) at a point and simulated instant."""
        hours = (when - _EPOCH).total_seconds() / 3600.0
        total = 0.0
        n = len(self._components)
        for kx, ky, phase, omega in self._components:
            total += math.sin(kx * lat + ky * lon + phase + omega * hours)
        # normalise by component count so the amplitude is the peak magnitude
        return self._amplitude * total / n
