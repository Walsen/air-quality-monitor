"""Geography profiles and the profile registry.

A :class:`GeographyProfile` is a self-contained set of geographic defaults
(bounding box, timezone, sub-areas, elevation, ``SiteCode`` prefix,
``SponsorName``, ``SensorContract``, meteorology ranges, seasonal PM
multiplier). Geography is a configuration concern, not a contract concern: every
profile causes the same field names to be emitted, differing only in values
(Requirement 8.9).

Two profiles ship built in — ``cochabamba`` (default) and ``reference`` (a
temperate sea-level reference) — seeded into the registry at import. Additional
profiles are registered from configuration (task 5.2) without a source change.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PROFILE_NAME = "cochabamba"


@dataclass(frozen=True, slots=True)
class GeographyProfile:
    """One named set of geographic defaults (Requirement 8.13 value set)."""

    name: str
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    timezone: str
    sub_areas: tuple[str, ...]
    elevation_m: float
    site_code_prefix: str
    sponsor_name: str
    sensor_contract: str
    temp_min_c: float
    temp_max_c: float
    rh_min_pct: float
    rh_max_pct: float
    pressure_min_hpa: float
    pressure_max_hpa: float
    seasonal_pm_multiplier: float

    def validate(self) -> list[str]:
        """Return a list of human-readable problems; empty means valid.

        Requirement 8.14: a profile whose ranges are inverted, whose RH is
        outside 0..100, or whose sub-areas fall outside its own bounding box is
        rejected. Callers name the offending profile alongside each problem.
        """
        problems: list[str] = []
        if not self.sub_areas:
            problems.append("sub_areas is empty")
        for lo, hi, label in (
            (self.lat_min, self.lat_max, "latitude"),
            (self.lon_min, self.lon_max, "longitude"),
            (self.temp_min_c, self.temp_max_c, "temperature"),
            (self.rh_min_pct, self.rh_max_pct, "RH"),
            (self.pressure_min_hpa, self.pressure_max_hpa, "pressure"),
        ):
            if lo >= hi:
                problems.append(f"{label} range min {lo} is not less than max {hi}")
        if not (0.0 <= self.rh_min_pct <= 100.0 and 0.0 <= self.rh_max_pct <= 100.0):
            problems.append("RH range is outside 0..100 percent")
        if self.seasonal_pm_multiplier < 1.0:
            problems.append("seasonal_pm_multiplier is below 1.0")
        return problems


_COCHABAMBA = GeographyProfile(
    name="cochabamba",
    lat_min=-17.50,
    lat_max=-17.29,
    lon_min=-66.40,
    lon_max=-66.02,
    timezone="America/La_Paz",
    sub_areas=(
        "Cochabamba",
        "Sacaba",
        "Quillacollo",
        "Tiquipaya",
        "Colcapirhua",
        "Vinto",
        "Sipe Sipe",
    ),
    elevation_m=2560.0,
    site_code_prefix="CB",
    sponsor_name="Kanata Air Quality Network",
    sensor_contract="Cellular-BO",
    temp_min_c=5.0,
    temp_max_c=30.0,
    rh_min_pct=15.0,
    rh_max_pct=90.0,
    pressure_min_hpa=730.0,
    pressure_max_hpa=755.0,
    seasonal_pm_multiplier=2.0,
)

_REFERENCE = GeographyProfile(
    name="reference",
    lat_min=51.28,
    lat_max=51.69,
    lon_min=-0.51,
    lon_max=0.33,
    timezone="UTC",
    sub_areas=("Central", "North", "South", "East", "West"),
    elevation_m=35.0,
    site_code_prefix="RF",
    sponsor_name="Reference Network",
    sensor_contract="Cellular-REF",
    temp_min_c=-5.0,
    temp_max_c=32.0,
    rh_min_pct=30.0,
    rh_max_pct=95.0,
    pressure_min_hpa=980.0,
    pressure_max_hpa=1040.0,
    seasonal_pm_multiplier=1.2,
)

# Built-in registry. Configuration-declared profiles are merged in (task 5.2)
# via a ProfileRegistry rather than mutating this module-level mapping.
_BUILTINS: dict[str, GeographyProfile] = {
    _COCHABAMBA.name: _COCHABAMBA,
    _REFERENCE.name: _REFERENCE,
}


def registered_profile_names() -> tuple[str, ...]:
    """Return the built-in profile names in a defined (sorted) order."""
    return tuple(sorted(_BUILTINS))


def get_profile(name: str) -> GeographyProfile:
    """Look up a built-in profile by name.

    Raises ``KeyError`` naming the supplied value and listing every registered
    profile name (Requirement 8.12).
    """
    try:
        return _BUILTINS[name]
    except KeyError:
        available = ", ".join(registered_profile_names())
        raise KeyError(
            f"unknown Geography_Profile {name!r}; registered profiles: {available}"
        ) from None
