"""The Virtual_Sensor factory.

Centralizes identity assignment (engineering-practices §4, Factory): the
prefixed zero-padded ``SiteCode``, a distinct ``DeviceCode``, coordinates inside
the profile bounding box, the sub-area ``Borough``, the ``SiteClassification``
mix, and the height/kerb values are all produced here, not scattered across call
sites. Every field is deterministic from ``(seed, SiteCode)`` via the sensor's
IDENTITY stream, so a restart with the same seed reproduces the whole fleet
(Requirement 8.3).

Borough assignment: the profile carries sub-area *names* but no polygons, so the
bounding box is partitioned into equal latitude bands, one per sub-area in
declared order, and a sensor's band gives its ``Borough`` (Requirement 8.5).
Geography is a configuration concern, so this partition is a modelling choice,
not a contract one.
"""

from __future__ import annotations

from dataclasses import dataclass

from aqm_simulator.geography.profiles import GeographyProfile
from aqm_simulator.rng.streams import Purpose, RandomStreamFactory

# Default classification mix (Requirement 8.6): 30% Roadside, 50% Urban
# Background, 20% Suburban, in a fixed order so assignment is deterministic.
_DEFAULT_MIX = (("Roadside", 0.30), ("Urban Background", 0.50), ("Suburban", 0.20))


@dataclass(frozen=True, slots=True)
class VirtualSensor:
    """One virtual sensor's stable identity (Requirement 8.3 field set)."""

    site_code: str
    site_name: str
    device_code: str
    installation_code: str
    latitude: str
    longitude: str
    borough: str
    classification: str
    power_tag: str
    sensor_height_m: float
    distance_to_kerb_m: float


def _classification_quota(size: int) -> list[str]:
    """Assign classifications by exact quota so proportions track the mix.

    Independent per-sensor sampling has binomial variance that can exceed the
    5pp bound of Requirement 8.6 for some seeds; assigning floor(share*size) of
    each class and distributing the remainder by largest fractional part keeps
    every observed share within one sensor (~0.5pp at size 200) of its target.
    """
    base: list[tuple[str, int, float]] = []
    assigned = 0
    for name, share in _DEFAULT_MIX:
        exact = share * size
        count = int(exact)
        base.append((name, count, exact - count))
        assigned += count
    # distribute the leftover to the largest fractional remainders, in order
    remainder = size - assigned
    for name, _count, _frac in sorted(base, key=lambda t: t[2], reverse=True)[:remainder]:
        for i, (n, c, f) in enumerate(base):
            if n == name:
                base[i] = (n, c + 1, f)
                break
    result: list[str] = []
    for name, count, _frac in base:
        result.extend([name] * count)
    return result


def _borough_for(latitude: float, profile: GeographyProfile) -> str:
    # Partition the bbox latitude into equal bands, one per sub-area in order.
    n = len(profile.sub_areas)
    span = profile.lat_max - profile.lat_min
    idx = int((latitude - profile.lat_min) / span * n)
    return profile.sub_areas[min(idx, n - 1)]


def _make_sensor(
    index: int, classification: str, profile: GeographyProfile, factory: RandomStreamFactory
) -> VirtualSensor:
    site_code = f"{profile.site_code_prefix}{index:04d}"
    rng = factory.stream(site_code, Purpose.IDENTITY)

    lat = float(rng.uniform(profile.lat_min, profile.lat_max))
    lon = float(rng.uniform(profile.lon_min, profile.lon_max))
    lat_s = f"{lat:.7f}"
    lon_s = f"{lon:.7f}"

    return VirtualSensor(
        site_code=site_code,
        site_name=f"{profile.name.title()} Site {index:04d}",
        device_code=f"DEV-{site_code}",
        installation_code=f"INST-{site_code}",
        latitude=lat_s,
        longitude=lon_s,
        borough=_borough_for(lat, profile),
        classification=classification,
        power_tag="Solar" if rng.random() < 0.25 else "Mains",
        sensor_height_m=round(float(rng.uniform(2.0, 3.0)), 2),
        distance_to_kerb_m=round(float(rng.uniform(0.5, 30.0)), 2),
    )


def build_swarm(
    size: int, profile: GeographyProfile, factory: RandomStreamFactory
) -> list[VirtualSensor]:
    """Instantiate ``size`` Virtual_Sensors, ordered by ascending SiteCode.

    Site codes run 0001..size; each sensor's identity is deterministic from its
    own IDENTITY stream, so a retained SiteCode is unaffected by swarm-size
    changes (Requirement 11.4 via the per-SiteCode stream keying).
    """
    if not (1 <= size <= 500):
        raise ValueError(f"swarm size {size} is outside the permitted range 1..500")
    classifications = _classification_quota(size)
    return [
        _make_sensor(i, classifications[i - 1], profile, factory)
        for i in range(1, size + 1)
    ]


class SiteListError(ValueError):
    """Raised when a supplied site list is invalid (names the entry and field)."""


_REQUIRED_SITE_FIELDS = ("SiteCode", "DeviceCode", "Latitude", "Longitude")


def build_swarm_from_sites(
    entries: list[dict[str, object]],
    profile: GeographyProfile,
    factory: RandomStreamFactory | None = None,
) -> list[VirtualSensor]:
    """Build a swarm from supplied entries, size = entry count (Requirement 8.8).

    Uses each entry's supplied SiteCode/DeviceCode/Latitude/Longitude in place of
    generated values; the non-supplied fields (Borough, classification, height,
    kerb, PowerTag) are derived deterministically. Rejects a duplicate SiteCode
    or DeviceCode, an entry missing a required field, or coordinates outside the
    profile bounding box, naming the offending entry and field (Requirement 8.11).
    """
    factory = factory or RandomStreamFactory(seed=0)
    seen_sites: set[str] = set()
    seen_devices: set[str] = set()
    classifications = _classification_quota(len(entries))
    swarm: list[VirtualSensor] = []

    for position, entry in enumerate(entries):
        for field_name in _REQUIRED_SITE_FIELDS:
            if field_name not in entry or entry[field_name] in (None, ""):
                raise SiteListError(
                    f"site-list entry {position} is missing required field {field_name!r}"
                )
        site_code = str(entry["SiteCode"])
        device_code = str(entry["DeviceCode"])
        if site_code in seen_sites:
            raise SiteListError(f"site-list entry {position}: duplicate SiteCode {site_code!r}")
        if device_code in seen_devices:
            raise SiteListError(
                f"site-list entry {position}: duplicate DeviceCode {device_code!r}"
            )
        seen_sites.add(site_code)
        seen_devices.add(device_code)

        lat = float(str(entry["Latitude"]))
        lon = float(str(entry["Longitude"]))
        in_box = (
            profile.lat_min <= lat <= profile.lat_max
            and profile.lon_min <= lon <= profile.lon_max
        )
        if not in_box:
            raise SiteListError(
                f"site-list entry {position} ({site_code}): coordinates ({lat}, {lon}) "
                f"fall outside the profile bounding box"
            )

        rng = factory.stream(site_code, Purpose.IDENTITY)
        swarm.append(
            VirtualSensor(
                site_code=site_code,
                site_name=str(entry.get("SiteName", f"{profile.name.title()} {site_code}")),
                device_code=device_code,
                installation_code=str(entry.get("InstallationCode", f"INST-{site_code}")),
                latitude=str(entry["Latitude"]),
                longitude=str(entry["Longitude"]),
                borough=_borough_for(lat, profile),
                classification=classifications[position],
                power_tag="Solar" if rng.random() < 0.25 else "Mains",
                sensor_height_m=round(float(rng.uniform(2.0, 3.0)), 2),
                distance_to_kerb_m=round(float(rng.uniform(0.5, 30.0)), 2),
            )
        )
    return swarm
