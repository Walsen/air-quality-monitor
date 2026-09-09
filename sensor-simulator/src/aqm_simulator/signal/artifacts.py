"""Sensor artifacts: measurement noise and per-sensor drift.

Real low-cost sensors are noisy and drift over time. :class:`SensorArtifacts`
models both for one Virtual_Sensor from its injected ARTIFACTS stream, so both
are deterministic and no domain code calls module-level random (§2):

- ``noise`` draws zero-mean Gaussian noise per Species (default sd 1.5 µg/m³ for
  PM2.5, 3.0 for NO2 — Requirement 7.1);
- ``drift`` returns a slow bias whose SIGN is fixed for the ``(seed, SiteCode)``
  pair, growing linearly with simulated elapsed time at the drift rate (default
  0.5 µg/m³ per 30 days) and capped at the maximum (default 5.0 — Requirement 7.2);
- ``apply_noise`` adds noise and clamps the result to >= 0 (Requirement 7.1).
"""

from __future__ import annotations

import datetime as dt

import numpy as np

_NOISE_SD = {"PM25": 1.5, "NO2": 3.0}
_DRIFT_RATE_PER_30D = 0.5  # µg/m³ per 30 simulated days
_DRIFT_MAX = 5.0  # µg/m³ cap on accumulated drift magnitude
_THIRTY_DAYS = dt.timedelta(days=30)


class SensorArtifacts:
    """Per-sensor measurement noise and drift, deterministic from one stream."""

    def __init__(self, site_code: str, rng: np.random.Generator, start: dt.datetime) -> None:
        self._rng = rng
        self._start = start
        # The drift sign is fixed once for this (seed, SiteCode) — a given sensor
        # drifts consistently up or down for the whole run (Requirement 7.2).
        self._drift_sign = 1.0 if rng.random() < 0.5 else -1.0

    def noise(self, species: str) -> float:
        """Zero-mean Gaussian measurement noise for ``species`` (µg/m³)."""
        return float(self._rng.normal(0.0, _NOISE_SD[species]))

    def drift(self, species: str, when: dt.datetime) -> float:
        """Accumulated drift bias at ``when`` — linear in elapsed time, capped."""
        elapsed = when - self._start
        periods = elapsed / _THIRTY_DAYS
        magnitude = min(_DRIFT_RATE_PER_30D * periods, _DRIFT_MAX)
        return self._drift_sign * magnitude

    def apply_noise(self, species: str, value: float) -> float:
        """Add measurement noise to a Tick value, clamped to >= 0 (Req 7.1)."""
        return max(0.0, value + self.noise(species))
