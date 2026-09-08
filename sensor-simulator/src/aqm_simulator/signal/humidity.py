"""Hygroscopic PM2.5 growth and its diagnostic recorder.

A low-cost optical PM sensor over-reads in humid air: water uptake swells the
aerosol so the *reported* concentration exceeds the *dry* concentration. The
growth factor ``g(RH)`` models that artifact (Requirement 5.7-5.9):

- ``g(RH) = 1.0`` for ``RH <= 50`` (5.8) — no correction needed;
- strictly increasing for ``50 < RH < 85`` (5.7);
- ``g(RH) >= 1.5`` for ``RH >= 85`` (5.9);
- never above the configured maximum, default 2.0.

The dry concentration, reported concentration, RH, and applied factor are kept
in a diagnostic record keyed by ``SiteCode`` and timestamp (Requirement 5.10) so
Service 2's correction step can be measured — but they are held here, NOT in the
emitted contract (Requirement 5.11), which carries only the reported value.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

_RH_LOW = 50.0  # at/below: no growth
_RH_HIGH = 85.0  # at/above: full plateau (>= 1.5)
_PLATEAU_MIN = 1.5
_DEFAULT_MAX = 2.0


def growth_factor(rh: float, max_factor: float = _DEFAULT_MAX) -> float:
    """Hygroscopic growth factor for a relative humidity ``rh`` (0..100)."""
    if rh <= _RH_LOW:
        return 1.0

    plateau = min(_PLATEAU_MIN, max_factor)
    if rh >= _RH_HIGH:
        # Continue rising past 85% toward max_factor, staying >= plateau.
        frac = (min(rh, 100.0) - _RH_HIGH) / (100.0 - _RH_HIGH)
        return plateau + (max_factor - plateau) * frac

    # 50 < rh < 85: strictly increasing 1.0 -> plateau.
    frac = (rh - _RH_LOW) / (_RH_HIGH - _RH_LOW)
    return 1.0 + (plateau - 1.0) * frac


@dataclass(frozen=True, slots=True)
class HumidityDiagnostic:
    """One recorded humidity-artifact application (diagnostic output only)."""

    site_code: str
    when: dt.datetime
    dry: float
    reported: float
    rh: float
    growth_factor: float


class HumidityArtifact:
    """Applies the growth factor and records diagnostics off the contract path."""

    def __init__(self, max_factor: float = _DEFAULT_MAX) -> None:
        self._max = max_factor
        self._diagnostics: list[HumidityDiagnostic] = []

    def apply(self, dry: float, rh: float, site_code: str, when: dt.datetime) -> float:
        """Return the reported concentration and record the diagnostic."""
        g = growth_factor(rh, self._max)
        reported = dry * g
        self._diagnostics.append(
            HumidityDiagnostic(
                site_code=site_code, when=when, dry=dry, reported=reported, rh=rh, growth_factor=g
            )
        )
        return reported

    def diagnostics(self) -> list[HumidityDiagnostic]:
        """Return the recorded diagnostics (never emitted in the contract)."""
        return list(self._diagnostics)
