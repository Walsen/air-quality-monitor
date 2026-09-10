"""Converting between mass concentration and mixing ratio.

The NO2 breakpoint table is expressed in ppb while the contract reports µg/m³, so a
conversion has to happen before the table is applied (Requirement 9.1). It is
temperature- and pressure-aware because the relation depends on both, and the default
deployment geography sits at roughly 2,560 m: at 740 hPa the factor is about 24 percent
smaller than at sea level, so a conversion that assumed standard pressure would
overstate every NO2 sub-index by about that much. That is the error this module exists
to prevent, and Requirement 9.6 pins both reference points by test so it cannot creep
back in.

Temperature and pressure are EXPLICIT PARAMETERS (Requirement 9.3), never read from
ambient state — the same rule §2 applies to clocks and random streams, for the same
reason: a conversion has to be reproducible from its inputs alone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MOLAR_GAS_CONSTANT = 8.314462618
"""The molar gas constant in J/(mol·K) (Requirement 9.2)."""

NO2_MOLAR_MASS_G_MOL = 46.0055
"""NO2 molar mass in g/mol (Requirement 9.2)."""

_G_PER_MOL_TO_UG_PER_M3 = 1e-3
"""The scale factor in Requirement 9.2's relation, reconciling g/mol with µg/m³."""


class ConversionUnavailableError(ValueError):
    """A resolved temperature or pressure cannot support a conversion (Req 9.9).

    Raised rather than returning a sentinel because there is no meaningful conversion
    to return: Requirement 9.9 says compute NO NO2 sub-index for such a reading, so the
    caller must branch, and an exception makes that unavoidable rather than optional.
    """

    def __init__(self, field: str, value: float) -> None:
        """Keep the offending field and value addressable for the Req 9.9 log (§5)."""
        self.field = field
        self.value = value
        super().__init__(
            f"conversion unavailable: {field} must be a finite value greater than 0, "
            f"got {value}"
        )


def _require_positive(field: str, value: float) -> float:
    if not math.isfinite(value) or value <= 0.0:
        raise ConversionUnavailableError(field, value)
    return value


@dataclass(frozen=True, slots=True)
class SiteConditions:
    """The temperature and absolute pressure a conversion was performed at.

    Validated on construction, so an unusable pair cannot be carried around and
    discovered later at the point of division (Requirement 9.9).
    """

    temperature_k: float
    pressure_pa: float

    def __post_init__(self) -> None:
        """Refuse a non-positive or non-finite value up front."""
        _require_positive("temperature_k", self.temperature_k)
        _require_positive("pressure_pa", self.pressure_pa)


DEFAULT_SITE_CONDITIONS = SiteConditions(temperature_k=288.15, pressure_pa=74_000.0)
"""Requirement 9.3's configured defaults: 15 °C and 740 hPa.

Deliberately NOT standard temperature and pressure. These reflect the approximately
2,560 m elevation of the default deployment geography, and using sea-level values here
would reintroduce exactly the error Requirement 9 exists to remove.
"""


def conversion_factor(*, temperature_k: float, pressure_pa: float) -> float:
    """Return µg/m³ per ppb at the given conditions (Requirement 9.2).

    Strictly increasing in pressure and strictly decreasing in absolute temperature
    (Requirement 9.5), which follows from pressure sitting in the numerator and
    temperature in the denominator.

    Raises:
        ConversionUnavailableError: if either input is non-finite or not above zero.
    """
    temperature = _require_positive("temperature_k", temperature_k)
    pressure = _require_positive("pressure_pa", pressure_pa)
    return (
        NO2_MOLAR_MASS_G_MOL
        * pressure
        / (MOLAR_GAS_CONSTANT * temperature)
        * _G_PER_MOL_TO_UG_PER_M3
    )


def ug_m3_from_ppb(
    mixing_ratio_ppb: float, *, temperature_k: float, pressure_pa: float
) -> float:
    """Convert a mixing ratio in ppb to a mass concentration in µg/m³."""
    return mixing_ratio_ppb * conversion_factor(
        temperature_k=temperature_k, pressure_pa=pressure_pa
    )


def ppb_from_ug_m3(
    mass_concentration_ug_m3: float, *, temperature_k: float, pressure_pa: float
) -> float:
    """Convert a mass concentration in µg/m³ to a mixing ratio in ppb.

    The exact inverse of :func:`ug_m3_from_ppb` under identical conditions
    (Requirement 9.4): it divides by the same factor rather than by a separately
    written expression, so the two cannot drift apart.
    """
    return mass_concentration_ug_m3 / conversion_factor(
        temperature_k=temperature_k, pressure_pa=pressure_pa
    )
