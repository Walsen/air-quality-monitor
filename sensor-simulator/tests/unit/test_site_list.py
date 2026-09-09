"""Unit tests for the supplied-site-list path (task 12.2).

Requirement 8.8: instantiate one Virtual_Sensor per supplied entry using the
supplied identity/location, setting swarm size to the entry count.
Requirement 8.11: reject a duplicate SiteCode, duplicate DeviceCode, an entry
missing SiteCode/DeviceCode/Latitude/Longitude, or coordinates outside the
active profile bounding box — naming the offending entry and field.
"""

from __future__ import annotations

import pytest

from aqm_simulator.geography.profiles import get_profile
from aqm_simulator.swarm.factory import SiteListError, build_swarm_from_sites


def _entry(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "SiteCode": "CB0001",
        "DeviceCode": "DEV-CB0001",
        "Latitude": "-17.3895000",
        "Longitude": "-66.1568000",
    }
    base.update(over)
    return base


def test_valid_site_list_builds_one_per_entry() -> None:
    profile = get_profile("cochabamba")
    entries = [
        _entry(SiteCode="CB0001", DeviceCode="DEV-CB0001"),
        _entry(SiteCode="CB0002", DeviceCode="DEV-CB0002", Latitude="-17.4000000"),
    ]
    swarm = build_swarm_from_sites(entries, profile)
    assert len(swarm) == 2
    assert swarm[0].site_code == "CB0001"
    assert swarm[1].latitude == "-17.4000000"


def test_duplicate_sitecode_rejected() -> None:
    profile = get_profile("cochabamba")
    with pytest.raises(SiteListError) as exc:
        build_swarm_from_sites([_entry(), _entry(DeviceCode="DEV-CB0002")], profile)
    assert "CB0001" in str(exc.value)


def test_duplicate_devicecode_rejected() -> None:
    profile = get_profile("cochabamba")
    with pytest.raises(SiteListError) as exc:
        build_swarm_from_sites([_entry(), _entry(SiteCode="CB0002")], profile)
    assert "DEV-CB0001" in str(exc.value)


@pytest.mark.parametrize("missing", ["SiteCode", "DeviceCode", "Latitude", "Longitude"])
def test_missing_field_rejected(missing: str) -> None:
    profile = get_profile("cochabamba")
    entry = _entry()
    del entry[missing]
    with pytest.raises(SiteListError) as exc:
        build_swarm_from_sites([entry], profile)
    assert missing in str(exc.value)


def test_coords_outside_bbox_rejected() -> None:
    profile = get_profile("cochabamba")
    with pytest.raises(SiteListError) as exc:
        build_swarm_from_sites([_entry(Latitude="0.0000000")], profile)
    assert "CB0001" in str(exc.value)
