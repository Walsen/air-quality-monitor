"""The profile registry.

Merges the built-in :class:`GeographyProfile` set with profiles declared in
configuration, validating each declared profile before it is registered
(Requirements 8.13, 8.14, 15.11) so retargeting the swarm to a new region needs
no source change. Registration is refused, naming the offending profile and
value, when a declared profile:

- omits any required value (incomplete value set);
- reuses the name of an already-registered profile;
- declares a ``SiteCode`` prefix equal to another registered profile's prefix;
- declares a sub-area whose representative point falls outside its own bounding
  box;
- fails the same range/RH/multiplier rules a built-in must satisfy.

A declared profile supplies ``sub_areas`` as a mapping of sub-area name to a
representative ``(lat, lon)`` point, so the bounding-box check has coordinates
to test; the stored :class:`GeographyProfile` keeps the sub-area names.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from aqm_simulator.geography.profiles import (
    _BUILTINS,
    GeographyProfile,
)

# The value keys a declared profile must supply (Requirement 8.13). Every
# GeographyProfile field except the derived tuple form of sub_areas, which a
# declared profile supplies as a name->point mapping.
_REQUIRED_KEYS = {f.name for f in fields(GeographyProfile)}


class ProfileRegistrationError(ValueError):
    """Raised when a config-declared profile fails validation."""


class ProfileRegistry:
    """A registry of geography profiles: built-ins plus declared ones."""

    def __init__(self) -> None:
        self._profiles: dict[str, GeographyProfile] = {}
        self._prefixes: dict[str, str] = {}  # prefix -> profile name

    @classmethod
    def with_builtins(cls) -> ProfileRegistry:
        reg = cls()
        for profile in _BUILTINS.values():
            reg._insert(profile)
        return reg

    def _insert(self, profile: GeographyProfile) -> None:
        self._profiles[profile.name] = profile
        self._prefixes[profile.site_code_prefix] = profile.name

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def get(self, name: str) -> GeographyProfile:
        try:
            return self._profiles[name]
        except KeyError:
            available = ", ".join(self.names())
            raise KeyError(
                f"unknown Geography_Profile {name!r}; registered profiles: {available}"
            ) from None

    def register_declared(self, declared: dict[str, Any]) -> GeographyProfile:
        """Validate and register a config-declared profile."""
        name = declared.get("name", "<unnamed>")

        missing = _REQUIRED_KEYS - declared.keys()
        if missing:
            raise ProfileRegistrationError(
                f"declared profile {name!r} is missing required values: "
                f"{', '.join(sorted(missing))}"
            )

        if name in self._profiles:
            raise ProfileRegistrationError(
                f"declared profile {name!r} reuses the name of a registered profile"
            )

        prefix = declared["site_code_prefix"]
        if prefix in self._prefixes:
            raise ProfileRegistrationError(
                f"declared profile {name!r} SiteCode prefix {prefix!r} collides with "
                f"registered profile {self._prefixes[prefix]!r}"
            )

        sub_areas_map = declared["sub_areas"]
        self._check_sub_areas_in_box(name, declared, sub_areas_map)

        profile = GeographyProfile(
            name=name,
            lat_min=declared["lat_min"],
            lat_max=declared["lat_max"],
            lon_min=declared["lon_min"],
            lon_max=declared["lon_max"],
            timezone=declared["timezone"],
            sub_areas=tuple(sub_areas_map),
            elevation_m=declared["elevation_m"],
            site_code_prefix=prefix,
            sponsor_name=declared["sponsor_name"],
            sensor_contract=declared["sensor_contract"],
            temp_min_c=declared["temp_min_c"],
            temp_max_c=declared["temp_max_c"],
            rh_min_pct=declared["rh_min_pct"],
            rh_max_pct=declared["rh_max_pct"],
            pressure_min_hpa=declared["pressure_min_hpa"],
            pressure_max_hpa=declared["pressure_max_hpa"],
            seasonal_pm_multiplier=declared["seasonal_pm_multiplier"],
        )

        problems = profile.validate()
        if problems:
            raise ProfileRegistrationError(
                f"declared profile {name!r} is invalid: {'; '.join(problems)}"
            )

        self._insert(profile)
        return profile

    @staticmethod
    def _check_sub_areas_in_box(
        name: str, declared: dict[str, Any], sub_areas_map: dict[str, tuple[float, float]]
    ) -> None:
        lat_min, lat_max = declared["lat_min"], declared["lat_max"]
        lon_min, lon_max = declared["lon_min"], declared["lon_max"]
        for area_name, (lat, lon) in sub_areas_map.items():
            if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
                raise ProfileRegistrationError(
                    f"declared profile {name!r} sub-area {area_name!r} at "
                    f"({lat}, {lon}) falls outside its own bounding box"
                )
