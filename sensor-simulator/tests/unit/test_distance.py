"""Unit tests for the great-circle distance helper."""

from __future__ import annotations

import pytest

from aqm_simulator.geography.distance import great_circle_km


def test_zero_distance() -> None:
    assert great_circle_km(-17.39, -66.15, -17.39, -66.15) == pytest.approx(0.0)


def test_known_short_distance() -> None:
    # ~0.01 degree latitude ~ 1.11 km
    d = great_circle_km(-17.39, -66.15, -17.40, -66.15)
    assert d == pytest.approx(1.11, abs=0.05)


def test_symmetric() -> None:
    a = great_circle_km(-17.39, -66.15, -17.45, -66.20)
    b = great_circle_km(-17.45, -66.20, -17.39, -66.15)
    assert a == pytest.approx(b)
