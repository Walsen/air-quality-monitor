"""Unit tests for the Virtual_Sensor factory (task 12.1).

Requirement 8.2: distinct SiteCode/DeviceCode; SiteCode = prefix + zero-padded 4 digits.
Requirement 8.3: restart with same seed reproduces every identity field.
Requirement 8.4: Latitude/Longitude inside the profile bounding box, 7 decimals.
Requirement 8.5: Borough is one of the profile sub-area names.
Requirement 8.7: SensorHeightAboveGround 2.0-3.0, DistanceToKerb 0.5-30.0.
"""

from __future__ import annotations

import re

from aqm_simulator.geography.profiles import get_profile
from aqm_simulator.rng.streams import RandomStreamFactory
from aqm_simulator.swarm.factory import VirtualSensor, build_swarm


def _swarm(size: int = 20, seed: int = 7, profile_name: str = "cochabamba") -> list[VirtualSensor]:
    profile = get_profile(profile_name)
    return build_swarm(size=size, profile=profile, factory=RandomStreamFactory(seed=seed))


def test_sitecodes_are_prefixed_zero_padded_four_digits() -> None:
    for sensor in _swarm():
        assert re.fullmatch(r"CB\d{4}", sensor.site_code)


def test_sitecodes_and_devicecodes_distinct() -> None:
    swarm = _swarm(50)
    assert len({s.site_code for s in swarm}) == 50
    assert len({s.device_code for s in swarm}) == 50


def test_coords_inside_bounding_box_seven_decimals() -> None:
    profile = get_profile("cochabamba")
    for s in _swarm():
        lat = float(s.latitude)
        lon = float(s.longitude)
        assert profile.lat_min <= lat <= profile.lat_max
        assert profile.lon_min <= lon <= profile.lon_max
        assert len(s.latitude.split(".")[1]) == 7
        assert len(s.longitude.split(".")[1]) == 7


def test_borough_is_a_profile_subarea() -> None:
    profile = get_profile("cochabamba")
    for s in _swarm():
        assert s.borough in profile.sub_areas


def test_height_and_kerb_within_range() -> None:
    for s in _swarm():
        assert 2.0 <= s.sensor_height_m <= 3.0
        assert 0.5 <= s.distance_to_kerb_m <= 30.0


def test_classification_from_permitted_set() -> None:
    for s in _swarm():
        assert s.classification in ("Roadside", "Urban Background", "Suburban")


def test_restart_reproduces_every_field() -> None:
    a = _swarm(size=10, seed=42)
    b = _swarm(size=10, seed=42)
    for sa, sb in zip(a, b, strict=True):
        assert sa == sb


def test_swarm_ordered_by_sitecode() -> None:
    swarm = _swarm(30)
    codes = [s.site_code for s in swarm]
    assert codes == sorted(codes)
