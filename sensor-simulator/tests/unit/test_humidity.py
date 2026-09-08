"""Unit tests for the hygroscopic humidity growth factor (task 8.5).

Requirement 5.7: growth factor g(RH) is monotonically non-decreasing, strictly
increasing for 50<RH<85, and never exceeds the configured maximum (default 2.0).
Requirement 5.8: g(RH)=1.0 exactly for RH<=50.
Requirement 5.9: g(RH) in [1.5, max] for RH>=85.
Requirement 5.10/5.11: the diagnostic recorder keeps Dry/Reported/RH/growth
keyed by SiteCode+timestamp, and these never enter the emitted contract.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aqm_simulator.signal.humidity import HumidityArtifact, growth_factor


def test_growth_is_one_at_or_below_50() -> None:
    for rh in (0.0, 25.0, 50.0):
        assert growth_factor(rh) == 1.0


def test_growth_at_least_1_5_above_85() -> None:
    for rh in (85.0, 90.0, 100.0):
        assert 1.5 <= growth_factor(rh) <= 2.0


def test_growth_capped_at_configured_max() -> None:
    for rh in (85.0, 95.0, 100.0):
        assert growth_factor(rh, max_factor=1.8) <= 1.8


def test_growth_strictly_increasing_between_50_and_85() -> None:
    prev = growth_factor(50.0)
    for rh in (55.0, 60.0, 70.0, 80.0, 84.9):
        cur = growth_factor(rh)
        assert cur > prev
        prev = cur


def test_reported_equals_dry_when_dry_and_rh_low() -> None:
    artifact = HumidityArtifact()
    reported = artifact.apply(dry=12.0, rh=40.0, site_code="CB0001", when=_ts())
    assert reported == pytest.approx(12.0)


def test_diagnostic_recorder_captures_and_excludes_from_contract() -> None:
    artifact = HumidityArtifact()
    when = _ts()
    artifact.apply(dry=10.0, rh=90.0, site_code="CB0001", when=when)
    diag = artifact.diagnostics()
    assert len(diag) == 1
    entry = diag[0]
    assert entry.site_code == "CB0001"
    assert entry.dry == 10.0
    assert entry.reported >= 10.0  # grown
    assert 1.5 <= entry.growth_factor <= 2.0
    # the diagnostic fields are not contract fields
    assert not hasattr(entry, "ScaledValue")


def _ts() -> dt.datetime:
    return dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
