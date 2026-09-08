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


def _classification_for(fraction: float) -> str:
    # Map a uniform fraction 0..1 onto the classification mix by cumulative share
    # (deterministic, so the observed proportions track the mix — Req 8.6).
    cumulative = 0.0
    for name, share in _DEFAULT_MIX:
        cumulative += share
        if fraction < cumulative:
            return name
    return _DEFAULT_MIX[-1][0]


def _borough_for(latitude: float, profile: GeographyProfile) -> str:
    # Partition the bbox latitude into equal bands, one per sub-area in order.
    n = len(profile.sub_areas)
    span = profile.lat_max - profile.lat_min
    idx = int((latitude - profile.lat_min) / span * n)
    return profile.sub_areas[min(idx, n - 1)]


def _make_sensor(
    index: int, profile: GeographyProfile, factory: RandomStreamFactory
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
        classification=_classification_for(float(rng.random())),
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
    return [_make_sensor(i, profile, factory) for i in range(1, size + 1)]
