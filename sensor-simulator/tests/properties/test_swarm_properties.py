"""Swarm identity/placement/classification property tests (tasks 12.3-12.5).

Feature: sensor-simulator-service
- Property 24: swarm identity uniqueness and form (Req 8.1, 8.2)
- Property 25: swarm placement within geography (Req 8.4, 8.5, 8.7)
- Property 26: classification mix proportions (Req 8.6)
"""

from __future__ import annotations

import re

from hypothesis import given, settings
from hypothesis import strategies as st

from aqm_simulator.geography.profiles import get_profile
from aqm_simulator.rng.streams import RandomStreamFactory
from aqm_simulator.swarm.factory import build_swarm

_PROFILES = ["cochabamba", "reference"]


@given(
    size=st.integers(min_value=1, max_value=200),
    seed=st.integers(min_value=0, max_value=10_000),
    profile_name=st.sampled_from(_PROFILES),
)
@settings(max_examples=100)
def test_property_24_identity_uniqueness_and_form(size: int, seed: int, profile_name: str) -> None:
    """Feature: sensor-simulator-service, Property 24."""
    profile = get_profile(profile_name)
    swarm = build_swarm(size=size, profile=profile, factory=RandomStreamFactory(seed=seed))
    assert len(swarm) == size
    site_codes = [s.site_code for s in swarm]
    device_codes = [s.device_code for s in swarm]
    assert len(set(site_codes)) == size  # distinct SiteCodes
    assert len(set(device_codes)) == size  # distinct DeviceCodes
    pattern = re.compile(rf"{profile.site_code_prefix}\d{{4}}")
    assert all(pattern.fullmatch(c) for c in site_codes)


@given(
    size=st.integers(min_value=1, max_value=200),
    seed=st.integers(min_value=0, max_value=10_000),
    profile_name=st.sampled_from(_PROFILES),
)
@settings(max_examples=100)
def test_property_25_placement_within_geography(size: int, seed: int, profile_name: str) -> None:
    """Feature: sensor-simulator-service, Property 25."""
    profile = get_profile(profile_name)
    swarm = build_swarm(size=size, profile=profile, factory=RandomStreamFactory(seed=seed))
    for s in swarm:
        assert profile.lat_min <= float(s.latitude) <= profile.lat_max
        assert profile.lon_min <= float(s.longitude) <= profile.lon_max
        assert s.borough in profile.sub_areas
        assert 2.0 <= s.sensor_height_m <= 3.0
        assert 0.5 <= s.distance_to_kerb_m <= 30.0


@given(seed=st.integers(min_value=0, max_value=10_000))
@settings(max_examples=100)
def test_property_26_classification_mix_within_5pp(seed: int) -> None:
    """Feature: sensor-simulator-service, Property 26 (swarm>=100 within 5pp)."""
    profile = get_profile("cochabamba")
    size = 200
    swarm = build_swarm(size=size, profile=profile, factory=RandomStreamFactory(seed=seed))
    counts = {"Roadside": 0, "Urban Background": 0, "Suburban": 0}
    for s in swarm:
        counts[s.classification] += 1
    expected = {"Roadside": 0.30, "Urban Background": 0.50, "Suburban": 0.20}
    for name, exp in expected.items():
        observed = counts[name] / size
        assert abs(observed - exp) <= 0.05, (name, observed)
