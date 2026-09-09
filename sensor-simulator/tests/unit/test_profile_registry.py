"""Unit tests for config-declared profile registration (tasks 5.2, 5.3).

Requirement 8.13: a declared profile must supply the complete value set and is
validated by the same rules as a built-in.
Requirement 8.14: reject a declared profile that reuses a registered name,
declares a SiteCode prefix equal to another registered profile's prefix, or
declares one or more sub-areas that fall outside its own bounding box — naming
the offending profile and value.
Requirement 15.9: an unknown resolved profile name lists every registered name.
"""

from __future__ import annotations

import pytest

from aqm_simulator.geography.profiles import GeographyProfile
from aqm_simulator.geography.registry import ProfileRegistrationError, ProfileRegistry


def _valid_declared(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "santacruz",
        "lat_min": -17.90,
        "lat_max": -17.70,
        "lon_min": -63.25,
        "lon_max": -63.05,
        "timezone": "America/La_Paz",
        "sub_areas": {"Centro": (-17.80, -63.18), "Norte": (-17.75, -63.15)},
        "elevation_m": 416.0,
        "site_code_prefix": "SC",
        "sponsor_name": "SC Net",
        "sensor_contract": "Cellular-SC",
        "temp_min_c": 15.0,
        "temp_max_c": 35.0,
        "rh_min_pct": 40.0,
        "rh_max_pct": 95.0,
        "pressure_min_hpa": 960.0,
        "pressure_max_hpa": 1010.0,
        "seasonal_pm_multiplier": 1.3,
    }
    base.update(overrides)
    return base


def test_register_valid_declared_profile() -> None:
    reg = ProfileRegistry.with_builtins()
    reg.register_declared(_valid_declared())
    assert "santacruz" in reg.names()
    profile = reg.get("santacruz")
    assert isinstance(profile, GeographyProfile)
    assert profile.site_code_prefix == "SC"


def test_builtins_present_by_default() -> None:
    reg = ProfileRegistry.with_builtins()
    assert {"cochabamba", "reference"} <= set(reg.names())


def test_reject_incomplete_value_set() -> None:
    reg = ProfileRegistry.with_builtins()
    incomplete = _valid_declared()
    del incomplete["timezone"]
    with pytest.raises(ProfileRegistrationError) as exc:
        reg.register_declared(incomplete)
    assert "santacruz" in str(exc.value)
    assert "timezone" in str(exc.value)


def test_reject_duplicate_name() -> None:
    reg = ProfileRegistry.with_builtins()
    with pytest.raises(ProfileRegistrationError) as exc:
        reg.register_declared(_valid_declared(name="cochabamba", site_code_prefix="ZZ"))
    assert "cochabamba" in str(exc.value)


def test_reject_colliding_prefix() -> None:
    reg = ProfileRegistry.with_builtins()
    with pytest.raises(ProfileRegistrationError) as exc:
        reg.register_declared(_valid_declared(site_code_prefix="CB"))  # CB = cochabamba
    assert "CB" in str(exc.value)


def test_reject_subarea_outside_bounding_box() -> None:
    reg = ProfileRegistry.with_builtins()
    bad = _valid_declared(sub_areas={"Centro": (-10.0, -63.18)})  # lat outside box
    with pytest.raises(ProfileRegistrationError) as exc:
        reg.register_declared(bad)
    assert "santacruz" in str(exc.value)
    assert "Centro" in str(exc.value)


def test_reject_inverted_range() -> None:
    reg = ProfileRegistry.with_builtins()
    with pytest.raises(ProfileRegistrationError):
        reg.register_declared(_valid_declared(temp_min_c=40.0, temp_max_c=10.0))


def test_get_unknown_lists_registered_names() -> None:
    reg = ProfileRegistry.with_builtins()
    with pytest.raises(KeyError) as exc:
        reg.get("atlantis")
    msg = str(exc.value)
    assert "atlantis" in msg
    assert "cochabamba" in msg and "reference" in msg
