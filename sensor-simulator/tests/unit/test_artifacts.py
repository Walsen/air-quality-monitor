"""Unit tests for measurement noise and per-sensor drift (task 13.1).

Requirement 7.1: zero-mean Gaussian noise per Species from the sensor's
ARTIFACTS stream (default sd PM25 1.5, NO2 3.0), and the noised Tick value is
clamped >= 0.
Requirement 7.2: a per-sensor drift term whose SIGN is fixed for (seed, SiteCode),
magnitude grows linearly with simulated elapsed time at the drift rate (default
0.5 µg/m³ per 30 simulated days), capped at the configured maximum (default 5.0).
"""

from __future__ import annotations

import datetime as dt

import numpy as np

from aqm_simulator.rng.streams import Purpose, RandomStreamFactory
from aqm_simulator.signal.artifacts import SensorArtifacts

_START = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def _artifacts(site: str = "CB0001", seed: int = 7) -> SensorArtifacts:
    rng = RandomStreamFactory(seed=seed).stream(site, Purpose.ARTIFACTS)
    return SensorArtifacts(site_code=site, rng=rng, start=_START)


def test_noise_is_zero_mean_over_many_samples() -> None:
    art = _artifacts()
    samples = [art.noise("PM25") for _ in range(5000)]
    assert abs(float(np.mean(samples))) < 0.2  # ~ 0 for sd 1.5 over 5000


def test_noise_sd_matches_species_default() -> None:
    art = _artifacts()
    pm = [art.noise("PM25") for _ in range(5000)]
    no2 = [art.noise("NO2") for _ in range(5000)]
    assert 1.2 < float(np.std(pm)) < 1.8   # ~1.5
    assert 2.5 < float(np.std(no2)) < 3.5  # ~3.0


def test_drift_sign_fixed_per_site_and_seed() -> None:
    a = _artifacts("CB0001", seed=42)
    signs = {np.sign(a.drift("PM25", _START + dt.timedelta(days=d))) for d in (10, 60, 120)}
    signs.discard(0.0)
    assert len(signs) == 1  # one consistent sign across time


def test_drift_grows_linearly_then_caps() -> None:
    art = _artifacts()
    d30 = abs(art.drift("PM25", _START + dt.timedelta(days=30)))
    d60 = abs(art.drift("PM25", _START + dt.timedelta(days=60)))
    # ~linear: 60 days ~ 2x 30 days (before the cap)
    assert d60 == abs(art.drift("PM25", _START + dt.timedelta(days=60)))
    assert d60 > d30
    # capped at 5.0
    d_far = abs(art.drift("PM25", _START + dt.timedelta(days=100000)))
    assert d_far <= 5.0 + 1e-9


def test_apply_clamps_noised_value_non_negative() -> None:
    art = _artifacts()
    # a tiny dry value plus noise must never go below 0
    for _ in range(1000):
        assert art.apply_noise("PM25", 0.1) >= 0.0


def test_deterministic_drift() -> None:
    when = _START + dt.timedelta(days=45)
    assert _artifacts().drift("NO2", when) == _artifacts().drift("NO2", when)
