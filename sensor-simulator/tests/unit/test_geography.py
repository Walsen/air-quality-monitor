"""Unit tests for GeographyProfile and the registry (task 5.1).

Requirement 8.4/8.5: profiles carry a bounding box and sub-areas; coordinates
fall inside the box and each maps to a sub-area.
Requirement 1.5/1.6/1.7: SiteCode prefix, SponsorName, SensorContract per profile.
Requirement 5.2/5.5/5.13: temperature/RH/pressure ranges; a complete value set.
Requirement 8.9: every registered profile emits the same field names.
"""

from __future__ import annotations

import pytest

from aqm_simulator.geography.profiles import (
    DEFAULT_PROFILE_NAME,
    GeographyProfile,
    get_profile,
    registered_profile_names,
)


def test_default_profile_is_cochabamba() -> None:
    assert DEFAULT_PROFILE_NAME == "cochabamba"


def test_both_builtins_registered() -> None:
    names = registered_profile_names()
    assert "cochabamba" in names
    assert "reference" in names


def test_cochabamba_values() -> None:
    p = get_profile("cochabamba")
    assert isinstance(p, GeographyProfile)
    assert p.site_code_prefix == "CB"
    assert p.sensor_contract == "Cellular-BO"
    assert p.timezone == "America/La_Paz"
    # bounding box lat -17.50..-17.29, lon -66.40..-66.02
    assert p.lat_min == pytest.approx(-17.50)
    assert p.lat_max == pytest.approx(-17.29)
    assert p.lon_min == pytest.approx(-66.40)
    assert p.lon_max == pytest.approx(-66.02)
    # the seven Kanata municipalities
    assert set(p.sub_areas) == {
        "Cochabamba",
        "Sacaba",
        "Quillacollo",
        "Tiquipaya",
        "Colcapirhua",
        "Vinto",
        "Sipe Sipe",
    }
    assert 2000 <= p.elevation_m <= 3000
    assert p.temp_min_c == pytest.approx(5.0)
    assert p.temp_max_c == pytest.approx(30.0)
    assert p.rh_min_pct == pytest.approx(15.0)
    assert p.rh_max_pct == pytest.approx(90.0)
    assert p.pressure_min_hpa == pytest.approx(730.0)
    assert p.pressure_max_hpa == pytest.approx(755.0)
    assert p.seasonal_pm_multiplier >= 1.0


def test_reference_values() -> None:
    p = get_profile("reference")
    assert p.site_code_prefix == "RF"
    assert p.sensor_contract == "Cellular-REF"
    assert p.pressure_min_hpa == pytest.approx(980.0)
    assert p.pressure_max_hpa == pytest.approx(1040.0)


def test_profile_is_frozen() -> None:
    p = get_profile("cochabamba")
    with pytest.raises((AttributeError, TypeError)):
        p.site_code_prefix = "XX"  # type: ignore[misc]


def test_unknown_profile_lists_registered_names() -> None:
    with pytest.raises(KeyError) as exc:
        get_profile("atlantis")
    msg = str(exc.value)
    assert "atlantis" in msg
    assert "cochabamba" in msg and "reference" in msg


def test_ranges_are_min_less_than_max() -> None:
    for name in registered_profile_names():
        p = get_profile(name)
        assert p.lat_min < p.lat_max
        assert p.lon_min < p.lon_max
        assert p.temp_min_c < p.temp_max_c
        assert p.rh_min_pct < p.rh_max_pct
        assert p.pressure_min_hpa < p.pressure_max_hpa
