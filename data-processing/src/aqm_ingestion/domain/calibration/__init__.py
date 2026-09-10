"""Humidity-aware calibration (Requirement 8).

The correction is applied before any Sub_Index, NowCast, Overall_AQI,
Threshold_Crossing or Inhaled_Dose is computed (Requirement 8.1), so nothing
downstream ever sees the uncorrected reported value.
"""

from aqm_ingestion.domain.calibration.strategies import (
    DEFAULT_NO_HUMIDITY_STRATEGY,
    DEFAULT_RH_LINEAR_COEFFICIENTS,
    DEFAULT_RH_LINEAR_DOMAIN,
    DEFAULT_SPECIES_STRATEGY,
    CalibrationDomain,
    CalibrationRegistry,
    CalibrationStrategy,
    IdentityStrategy,
    RhLinearCoefficients,
    RhLinearStrategy,
    UnknownStrategyError,
)

__all__ = [
    "DEFAULT_NO_HUMIDITY_STRATEGY",
    "DEFAULT_RH_LINEAR_COEFFICIENTS",
    "DEFAULT_RH_LINEAR_DOMAIN",
    "DEFAULT_SPECIES_STRATEGY",
    "CalibrationDomain",
    "CalibrationRegistry",
    "CalibrationStrategy",
    "IdentityStrategy",
    "RhLinearCoefficients",
    "RhLinearStrategy",
    "UnknownStrategyError",
]
