"""Personal_Threshold resolution and Threshold_Crossing evaluation.

Requirement 22. Pure domain logic — no clock, no adapters.

Two things here are easy to get subtly wrong, so both are stated plainly:

**The escalation point is per SPECIES, not per user.** Requirement 22.1 says "a
Personal_Threshold for the species", so a PM2.5 threshold must not govern NO2. A per-profile
lookup would pass a single-species test and quietly apply the wrong trigger point the moment a
second species appeared.

**A crossing holds on greater-or-EQUAL.** Requirement 22.3 says so twice over, and Requirement
22.7 restates it as an if-and-only-if. A strict ``>`` is the natural slip and would withhold the
warning at exactly the value the user nominated.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from aqm_ingestion.domain.aqi.breakpoints import BreakpointTableRegistry
from aqm_ingestion.domain.aqi.subindex import compute_sub_index
from aqm_ingestion.domain.models import Confidence
from aqm_ingestion.domain.profile import (
    PersonalThreshold,
    SensitivityLevel,
    ThresholdKind,
    UserProfile,
)

DEFAULT_ORANGE_BAND_LOWER = 101
"""Requirement 22.1's final fallback: the Orange_Band lower bound."""

SENSITIVITY_ESCALATION: Mapping[SensitivityLevel, int] = {
    SensitivityLevel.STANDARD: 101,
    SensitivityLevel.ELEVATED: 76,
    SensitivityLevel.HIGH: 51,
}
"""Requirement 22.2's mapping.

Every value is at or below the Orange_Band bound of 101, never the public `Unhealthy` threshold
of 151 — which is the point of the mapping: a respiratory user is warned while the warning is
still actionable for them. A test pins that ceiling so raising a value fails loudly.
"""


class ThresholdSource(StrEnum):
    """Which tier of Requirement 22.1's precedence supplied the escalation point."""

    PERSONAL_THRESHOLD = "personal_threshold"
    SENSITIVITY_LEVEL = "sensitivity_level"
    DEFAULT_ORANGE_BAND = "default_orange_band"


@dataclass(frozen=True, slots=True)
class EffectiveEscalation:
    """The Sub_Index a user escalates at, and where it came from (Requirements 22.6, 22.9)."""

    sub_index: int
    source: ThresholdSource


@dataclass(frozen=True, slots=True)
class MeasuredSubIndex:
    """One site's Sub_Index for one species, with the Confidence behind it.

    Confidence travels WITH the measurement rather than being looked up later, so Requirement
    22.8's "report the Confidence alongside the crossing" cannot be satisfied by a value from a
    different reading.
    """

    site_code: str
    species: str
    sub_index: int
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class ThresholdCrossing:
    """One reported crossing (Requirement 22.6).

    ``confidence`` has no default: Requirement 22.8 forbids escalating on a low-Confidence value
    without reporting that Confidence, and a defaulted field would let a caller omit it.
    """

    site_code: str
    species: str
    sub_index: int
    threshold: int
    source: ThresholdSource
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class CrossingReport:
    """Every crossing, plus the basis on which each species was judged."""

    crossed: bool
    crossings: tuple[ThresholdCrossing, ...]
    effective: Mapping[str, EffectiveEscalation]


def resolve_escalation(
    profile: UserProfile | None,
    species: str,
    registry: BreakpointTableRegistry,
    table_id: str,
) -> EffectiveEscalation:
    """Return the escalation Sub_Index for one species (Requirements 22.1, 22.5).

    ``profile`` is optional so Requirement 22.1's third tier is reachable without building
    an unlawful profile: with no profile there is no Sensitivity_Level either, and the
    Orange_Band bound applies.
    """
    if profile is None:
        return EffectiveEscalation(
            sub_index=DEFAULT_ORANGE_BAND_LOWER,
            source=ThresholdSource.DEFAULT_ORANGE_BAND,
        )

    threshold = profile.personal_thresholds.get(species)
    if threshold is not None:
        return EffectiveEscalation(
            sub_index=_threshold_as_sub_index(threshold, species, registry, table_id),
            source=ThresholdSource.PERSONAL_THRESHOLD,
        )

    return EffectiveEscalation(
        sub_index=SENSITIVITY_ESCALATION[profile.sensitivity_level],
        source=ThresholdSource.SENSITIVITY_LEVEL,
    )


def evaluate_crossings(
    measurements: Iterable[MeasuredSubIndex],
    profile: UserProfile | None,
    registry: BreakpointTableRegistry,
    table_id: str,
) -> CrossingReport:
    """Report every Threshold_Crossing among the measurements (Requirements 22.3, 22.6-22.10).

    The effective escalation is reported for every species MEASURED, not only for those that
    crossed: Requirement 22.9 makes the basis reviewable, and that matters most when nothing
    crossed, since the user still needs to know what they were judged against.

    Results are ordered by site then species so the response order is defined (§2) rather than
    inherited from however the measurements arrived.
    """
    ordered = sorted(measurements, key=lambda m: (m.site_code, m.species))

    effective: dict[str, EffectiveEscalation] = {}
    crossings: list[ThresholdCrossing] = []

    for measurement in ordered:
        if measurement.species not in effective:
            effective[measurement.species] = resolve_escalation(
                profile, measurement.species, registry, table_id
            )
        point = effective[measurement.species]

        # Requirement 22.3/22.7: greater-or-equal, equality included.
        if measurement.sub_index >= point.sub_index:
            crossings.append(
                ThresholdCrossing(
                    site_code=measurement.site_code,
                    species=measurement.species,
                    sub_index=measurement.sub_index,
                    threshold=point.sub_index,
                    source=point.source,
                    confidence=measurement.confidence,
                )
            )

    return CrossingReport(
        crossed=bool(crossings),
        crossings=tuple(crossings),
        # Read-only: a plain dict inside a frozen dataclass leaves the reported BASIS mutable,
        # so a caller could change what a crossing claims to have been judged against.
        # Sorted so that basis has a defined order too (§2).
        effective=MappingProxyType(
            {species: effective[species] for species in sorted(effective)}
        ),
    )


def _threshold_as_sub_index(
    threshold: PersonalThreshold,
    species: str,
    registry: BreakpointTableRegistry,
    table_id: str,
) -> int:
    """Express a Personal_Threshold as a Sub_Index (Requirement 22.5).

    A concentration is converted through the SAME Breakpoint_Table the response uses, via the
    same ``compute_sub_index`` — not a parallel calculation — because a threshold judged on a
    different scale from the value it is compared against would be meaningless.
    """
    if threshold.kind is ThresholdKind.SUB_INDEX:
        return int(threshold.value)
    table = registry.resolve(table_id, species)
    return compute_sub_index(threshold.value, table).sub_index


__all__ = [
    "DEFAULT_ORANGE_BAND_LOWER",
    "SENSITIVITY_ESCALATION",
    "CrossingReport",
    "EffectiveEscalation",
    "MeasuredSubIndex",
    "ThresholdCrossing",
    "ThresholdSource",
    "evaluate_crossings",
    "resolve_escalation",
]
