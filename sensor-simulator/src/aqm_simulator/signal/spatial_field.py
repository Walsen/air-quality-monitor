"""The shared spatial local field.

The Regional_Field gives one city-wide signal; the :class:`SpatialField` adds
the *local* PM2.5 structure that varies across the city but SMOOTHLY, so two
sensors close together see nearly the same local component and two far apart see
different ones. That is what produces the spatial correlation and distance decay
the spec requires (Requirements 9.2, 9.3).

Construction — why random Fourier features rather than a couple of harmonics.
A single sinusoid's spatial covariance between two points separated by ``d`` is
``cos(k·d)``, which OSCILLATES with distance: it falls, bottoms out at half a
wavelength, then rises again. A field built from two such harmonics therefore
lets a farther band correlate MORE strongly than a nearer one, which violates the
spatial-decay property of Requirement 9.3 (mean correlation must be
non-increasing across the 0-2, 2-5, 5-10 and >10 km bands within a 0.05
tolerance).

Sampling many frequency vectors from an isotropic Gaussian spectrum instead makes
the covariance the spectral average ``mean(cos(k·d))``, which for a Gaussian
spectrum of length scale ``L`` is ``exp(-|d|^2 / 2L^2)`` — strictly decreasing in
distance. Each harmonic also carries its own temporal frequency so every site has
a real hour-to-hour series; cross-harmonic terms average out over the window and
leave the spatial covariance intact.

Seeded from the shared ``_SPATIAL`` stream, the field is deterministic and
identical for the whole swarm (engineering-practices §2).
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np

from aqm_simulator.rng.streams import Purpose, RandomStreamFactory

_SPATIAL_SITE = "_SPATIAL"
_EPOCH = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)

# Correlation length in DEGREES (~0.06 deg ~ 6.7 km). Chosen so a <2 km pair
# stays above the 0.6 floor of Requirement 9.2 (corr ~ 0.96 from the spatial
# term alone) while a >10 km pair decorrelates strongly (corr ~ 0.01).
_LENGTH_SCALE_DEG = 0.06
# Number of sampled harmonics. The covariance approaches the exponential-square
# form as this grows; 32 is enough for the band ordering to hold while staying
# cheap because every harmonic is evaluated in one vectorised numpy call.
# Number of sampled harmonics. Each takes a DISTINCT integer multiple of the
# window fundamental below, and hourly sampling over 72 h resolves multiples up
# to 36, so this must stay at or below that Nyquist limit.
_HARMONICS = 32
# The correlation window of Requirements 9.2/9.3 is 72 simulated hours. Giving
# every harmonic a temporal frequency that is an exact multiple of 2*pi/72 makes
# the harmonics orthogonal over that window, so the cross-harmonic terms cancel
# EXACTLY and the measured correlation equals the spectral average. Periods drawn
# at random instead span only 1-4 cycles in 72 h, leaving large seed-dependent
# leakage that swamps the distance signal and inverts the band ordering.
_WINDOW_HOURS = 72.0


class SpatialField:
    """A smooth, time-evolving 2D local PM2.5 field shared by the swarm."""

    def __init__(self, rng: np.random.Generator, amplitude: float) -> None:
        # Halving keeps the field's standard deviation equal to the two-harmonic
        # construction this replaced, so the tuned PM2.5 range properties (P10,
        # P13, P27) see the same typical local magnitude.
        self._scale = (amplitude / 2.0) * math.sqrt(2.0 / _HARMONICS)
        # STRATIFIED isotropic Gaussian spectrum. Drawing the frequency vectors
        # iid leaves the realized covariance mean(cos(k·d)) with sampling error
        # that is fixed for the run and dips NEGATIVE at mid separations for some
        # seeds, inverting the 5-10 km and >10 km band means. Stratifying both the
        # radius (inverse Rayleigh CDF on a regular grid) and the angle makes the
        # realized covariance track exp(-d^2/2L^2) closely for every seed, so the
        # decay of Requirement 9.3 holds by construction rather than by luck.
        sigma_k = 1.0 / _LENGTH_SCALE_DEG
        strata = (np.arange(_HARMONICS) + 0.5) / _HARMONICS
        radii = sigma_k * np.sqrt(-2.0 * np.log1p(-strata))  # Rayleigh quantiles
        # Angles follow a golden-angle sequence rather than the SAME stratum index
        # as the radii: indexing both by i would pair the largest frequencies with
        # related directions (a spiral), biasing the projection k·d and leaving the
        # mid-distance covariance negative. The golden angle spreads directions
        # evenly while decoupling them from radius.
        golden = (math.sqrt(5.0) - 1.0) / 2.0
        rotation = float(rng.uniform(0.0, 2 * math.pi))
        angles = 2 * math.pi * np.mod(np.arange(_HARMONICS) * golden, 1.0) + rotation
        self._kx = radii * np.cos(angles)
        self._ky = radii * np.sin(angles)
        self._phase = rng.uniform(0.0, 2 * math.pi, _HARMONICS)
        # distinct integer multiples of the window fundamental -> orthogonal in time
        multiples = np.arange(1, _HARMONICS + 1, dtype=float)
        self._omega = 2 * math.pi * multiples / _WINDOW_HOURS

    @classmethod
    def build(cls, factory: RandomStreamFactory, amplitude: float) -> SpatialField:
        return cls(factory.stream(_SPATIAL_SITE, Purpose.SIGNAL), amplitude)

    def sample(self, lat: float, lon: float, when: dt.datetime) -> float:
        """Local PM2.5 component (µg/m³) at a point and simulated instant."""
        hours = (when - _EPOCH).total_seconds() / 3600.0
        angles = self._kx * lat + self._ky * lon + self._phase + self._omega * hours
        return float(self._scale * np.cos(angles).sum())
