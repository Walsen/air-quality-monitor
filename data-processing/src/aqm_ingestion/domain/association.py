"""The Exposure_Association and the Learned_Threshold (Requirement 32).

A pure function of stored entries, stored Readings and configuration: no randomness, no wall
clock
(Requirement 32.10), so the same stored data always yields the same report and a derivation is
reproducible from an audit. The "as of" instant a caller may pass is a PARAMETER rather than a
clock read, which is what keeps that true.

Four decisions here would look equally reasonable made differently, and are not.

**A lag of L pairs the symptom on day D with the exposure on day D-L**, because the
exposure comes first. Requirement 32.2 evaluates lag 3 by default since the evidence base
places the gaseous effect on the same day and the particulate effect about three days later
(``docs/research/FINDINGS.md`` cycle 2). Getting this direction backwards would look
plausible and
would inverted the whole feature, so a test asserts a deliberately delayed relationship scores
higher at lag 3 than at lag 0.

**A refusal is a result** (Requirement 32.4). Below the minimum paired observation count this
reports a :class:`AssociationShortfall` rather than a number: a threshold learned from
four
days is worse than no learned threshold: it is stated with the same confidence and acted on the
same way.

**The reach is the SHORTER of the two retention windows** (Requirement 32.5). A diary
entry whose
exposure data has already aged out cannot be paired, so counting it as an observation would
inflate
the sample the association claims to rest on.

**A sub-floor threshold is DROPPED, not clamped** (Requirement 32.8). Clamping a
derivation of 20 up
to 51 would report a learned threshold the data does not support. Learning an escalation point
inside the Good band would alert continuously and teach the user to ignore the alerts, so the
honest
outcome is no learned threshold at all.

One thing this module deliberately does NOT do: name a cause. Requirement 32.9 permits reporting
an
association and forbids reporting, typing or naming it as a cause. The vocabulary here is
``strength`` and ``observations`` throughout — there is no ``trigger``, ``causes`` or
``because``
field for a caller to surface.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from aqm_ingestion.domain.aqi.overall import DEFAULT_SPECIES_PRECEDENCE
from aqm_ingestion.domain.symptoms import (
    DEFAULT_SYMPTOM_RETENTION_DAYS,
    SeverityObservation,
    effective_reach_days,
)

DEFAULT_ASSOCIATION_LAGS: tuple[int, ...] = (0, 3)
"""Requirement 32.1/32.2's default lags, in whole days.

Zero for the gaseous same-day effect and three for the particulate effect that peaks about three
days later. A same-day-only configuration would systematically miss the PM signal, which is the
signal that matters most for the PM-sensitive user this feature exists to serve.
"""

DEFAULT_MIN_OBSERVATIONS = 14
"""Requirement 32.4's default minimum paired observation count."""

DEFAULT_MIN_STRENGTH = 0.3
"""Requirement 32.6's default minimum association strength.

A conventional weak/moderate boundary for a correlation coefficient. Configuration rather than a
constant of nature: it trades a threshold learned from a noisy relationship against no threshold
at
all, and which side of that trade a deployment wants is not something this module can decide.
"""

DEFAULT_THRESHOLD_FLOOR = 51
"""Requirement 32.8's default floor: the bottom of the Moderate band."""

DEFAULT_ELEVATED_SEVERITY = 3
"""Which Symptom_Severity counts as "elevated" when siting a Learned_Threshold.

Three is the midpoint of Requirement 31.3's 1-5 range. Requirement 32 does not pin this,
so it is
configuration with a documented default rather than a constant presented as though the spec
required it.
"""

DEFAULT_READINGS_RETENTION_DAYS = 90
"""Mirrors the Readings retention default, for Requirement 32.5's clamp.

Duplicated from the ingestion configuration on purpose — the association is a pure function and
must
not reach for a config object (§1) — so a drift guard in the test suite pins the two together.
"""

_SUB_INDEX_MIN = 1
_SUB_INDEX_MAX = 500
"""Requirement 32.8's permitted Sub_Index range for a Learned_Threshold."""

_PERCENTILE = 0.25
"""Which percentile of elevated-symptom-day exposure sites the threshold.

The escalation point wants to be where symptoms START, so a low percentile rather than the
median.
Not the minimum, which a single unusual day would drag down. Nearest-rank, with no
interpolation,
so the result is exactly reproducible rather than dependent on a floating-point division.
"""


@dataclass(frozen=True, slots=True)
class ExposureObservation:
    """One day's Sub_Index for one species, as the caller aggregated it.

    The caller — the scheduled job of Requirement 32.12 — reduces the stored per-site,
    per-interval
    Readings to one value per day per species before calling in. That keeps this module a pure
    function over two series and free of any store dependency.
    """

    on: dt.date
    sub_index: int


@dataclass(frozen=True, slots=True)
class AssociationLimits:
    """The configuration an association is computed under (§1)."""

    lags: tuple[int, ...] = DEFAULT_ASSOCIATION_LAGS
    min_observations: int = DEFAULT_MIN_OBSERVATIONS
    min_strength: float = DEFAULT_MIN_STRENGTH
    threshold_floor: int = DEFAULT_THRESHOLD_FLOOR
    elevated_severity: int = DEFAULT_ELEVATED_SEVERITY
    symptom_retention_days: int = DEFAULT_SYMPTOM_RETENTION_DAYS
    readings_retention_days: int = DEFAULT_READINGS_RETENTION_DAYS
    species_precedence: tuple[str, ...] = DEFAULT_SPECIES_PRECEDENCE


DEFAULT_ASSOCIATION_LIMITS = AssociationLimits()


@dataclass(frozen=True, slots=True)
class ExposureAssociation:
    """One species at one lag: what was found, and what it rests on (Requirement 32.3).

    ``strength`` is a correlation coefficient in [-1, 1]. It is called strength rather than
    ``correlation`` because Requirement 32.9 forbids presenting this as a causal finding and the
    surrounding vocabulary should not invite one.
    """

    species: str
    lag_days: int
    observations: int
    strength: float


@dataclass(frozen=True, slots=True)
class AssociationShortfall:
    """Why no association was computed for a species at a lag (Requirement 32.4).

    A first-class result rather than an omission: the caller must distinguish
    "no relationship"
    from "not enough data yet", and a missing entry cannot say which.
    """

    species: str
    lag_days: int
    observations: int
    required: int


@dataclass(frozen=True, slots=True)
class LearnedThreshold:
    """An escalation Sub_Index derived from the user's own diary (Requirement 32.6).

    Carries the derivation with it — species, lag and observation count — so Requirement 30's
    reporting obligation can be met without a second lookup, and so a threshold can never be
    surfaced without the basis it rests on.
    """

    species: str
    sub_index: int
    lag_days: int
    observations: int


@dataclass(frozen=True, slots=True)
class AssociationReport:
    """Everything one evaluation produced."""

    associations: tuple[ExposureAssociation, ...] = ()
    shortfalls: tuple[AssociationShortfall, ...] = ()
    learned_thresholds: tuple[LearnedThreshold, ...] = ()
    effective_reach_days: int = 0
    considered_species: tuple[str, ...] = field(default=())


def compute_association_report(
    severities: Sequence[SeverityObservation],
    exposures: Mapping[str, Sequence[ExposureObservation]],
    limits: AssociationLimits = DEFAULT_ASSOCIATION_LIMITS,
    *,
    as_of: dt.date | None = None,
) -> AssociationReport:
    """Evaluate every species at every configured lag (Requirements 32.1-32.11).

    Args:
        severities: the user's Symptom_Severity series. Deliberately typed on
            :class:`SeverityObservation`, which has no note and no marker field, so a
            Symptom_Note cannot reach this computation (Requirement 31.6).
        exposures: per-species daily Sub_Index series, already aggregated by the caller.
        limits: the lags, minimums, floor, retention windows and species precedence.
        as_of: the date the retention reach is measured back from. A PARAMETER rather than a
        clock
            read, which is what keeps this function pure (Requirement 32.10). Defaults to the
            latest date present in the data, so an audit replay needs no extra input.

    Returns:
        The associations found, the shortfalls that prevented others, and any
        Learned_Thresholds.
    """
    reach = effective_reach_days(
        limits.symptom_retention_days, limits.readings_retention_days
    )
    anchor = as_of if as_of is not None else _latest_date(severities, exposures)
    floor_date = None if anchor is None else anchor - dt.timedelta(days=reach)

    in_reach = tuple(
        observation
        for observation in sorted(severities, key=lambda o: o.on)
        if floor_date is None or observation.on >= floor_date
    )
    severity_by_date = {observation.on: observation.severity for observation in in_reach}

    associations: list[ExposureAssociation] = []
    shortfalls: list[AssociationShortfall] = []
    learned: list[LearnedThreshold] = []

    # Requirement 32.11: species by the configured PRECEDENCE, not by name.
    for species in sorted(exposures, key=lambda name: _species_rank(name, limits)):
        exposure_by_date = {
            observation.on: observation.sub_index
            for observation in exposures[species]
            if floor_date is None or observation.on >= floor_date
        }
        best: tuple[float, LearnedThreshold] | None = None

        for lag in limits.lags:
            pairs = _paired(severity_by_date, exposure_by_date, lag)
            if len(pairs) < limits.min_observations:
                shortfalls.append(
                    AssociationShortfall(
                        species=species,
                        lag_days=lag,
                        observations=len(pairs),
                        required=limits.min_observations,
                    )
                )
                continue

            strength = _correlation(
                [float(severity) for severity, _ in pairs],
                [float(exposure) for _, exposure in pairs],
            )
            if strength is None:
                # Undefined rather than zero: one of the series has no variance, so there is
                # nothing to correlate. Reported as a shortfall of usable data rather
                # than as strength 0.
                shortfalls.append(
                    AssociationShortfall(
                        species=species,
                        lag_days=lag,
                        observations=len(pairs),
                        required=limits.min_observations,
                    )
                )
                continue

            associations.append(
                ExposureAssociation(
                    species=species,
                    lag_days=lag,
                    observations=len(pairs),
                    strength=strength,
                )
            )

            if strength < limits.min_strength:
                continue
            candidate = _site_threshold(pairs, lag, species, limits)
            if candidate is None:
                continue
            # Req 32.6 does not say which lag wins when several qualify, and emitting
            # two
            # thresholds for one species would make Requirement 22.1's precedence ambiguous. The
            # strongest association wins, with the smaller lag breaking a tie so the choice is
            # deterministic rather than dependent on iteration order.
            if best is None or (strength, -lag) > (best[0], -best[1].lag_days):
                best = (strength, candidate)

        if best is not None:
            learned.append(best[1])

    return AssociationReport(
        associations=tuple(associations),
        shortfalls=tuple(shortfalls),
        learned_thresholds=tuple(learned),
        effective_reach_days=reach,
        considered_species=tuple(
            sorted(exposures, key=lambda name: _species_rank(name, limits))
        ),
    )


def _species_rank(species: str, limits: AssociationLimits) -> tuple[int, str]:
    """Rank by the configured precedence, unlisted species last then by name.

    Requirement 32.11 defers to Requirement 14.3, whose default precedence is PM25 before NO2 —
    the reverse of alphabetical. Sorting by name would look correct and be wrong.
    """
    try:
        return (limits.species_precedence.index(species), species)
    except ValueError:
        return (len(limits.species_precedence), species)


def _latest_date(
    severities: Sequence[SeverityObservation],
    exposures: Mapping[str, Sequence[ExposureObservation]],
) -> dt.date | None:
    """The most recent date present anywhere in the inputs, or None if there is none."""
    dates = [observation.on for observation in severities]
    for series in exposures.values():
        dates.extend(observation.on for observation in series)
    return max(dates) if dates else None


def _paired(
    severity_by_date: Mapping[dt.date, int],
    exposure_by_date: Mapping[dt.date, int],
    lag: int,
) -> tuple[tuple[int, int], ...]:
    """Pair each symptom day with the exposure ``lag`` days EARLIER.

    The exposure comes first — that is the whole point of a lag — so a symptom on day D pairs
    with
    the exposure on D minus lag. A symptom day whose partner is missing is simply not paired,
    which
    is why a larger lag yields fewer observations.

    Ordered by symptom date ascending (Requirement 32.11), so the correlation is computed over a
    defined sequence.
    """
    offset = dt.timedelta(days=lag)
    return tuple(
        (severity_by_date[on], exposure_by_date[on - offset])
        for on in sorted(severity_by_date)
        if (on - offset) in exposure_by_date
    )


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Pearson correlation, or None where it is undefined.

    Returns None rather than 0.0 when either series has no variance. Those are different facts:
    a
    zero coefficient says "no linear relationship was found in varying data", while no variance
    says "there was nothing to compare". Collapsing them would let a diary of identical
    severities
    report a real finding of no association.
    """
    count = len(left)
    if count == 0 or count != len(right):
        return None
    mean_left = math.fsum(left) / count
    mean_right = math.fsum(right) / count
    covariance = math.fsum(
        (a - mean_left) * (b - mean_right) for a, b in zip(left, right, strict=True)
    )
    spread_left = math.sqrt(math.fsum((a - mean_left) ** 2 for a in left))
    spread_right = math.sqrt(math.fsum((b - mean_right) ** 2 for b in right))
    if spread_left == 0.0 or spread_right == 0.0:
        return None
    return covariance / (spread_left * spread_right)


def _site_threshold(
    pairs: Sequence[tuple[int, int]],
    lag: int,
    species: str,
    limits: AssociationLimits,
) -> LearnedThreshold | None:
    """Site the escalation Sub_Index from the elevated-symptom days (Requirements 32.6, 32.8).

    Takes the configured low percentile of the exposure values on days whose severity was at or
    above the elevated level — the point where this person's symptoms START, not the
    point
    where they are already bad. Nearest-rank, so the result is an actual observed Sub_Index and
    not
    an interpolated value that never occurred.

    Returns None where the derivation falls outside Requirement 32.8's range or below the
    configured floor. DROPPED rather than clamped: clamping 20 up to 51 would report a threshold
    the data does not support.
    """
    elevated = sorted(
        exposure for severity, exposure in pairs if severity >= limits.elevated_severity
    )
    if not elevated:
        return None
    index = max(0, math.ceil(_PERCENTILE * len(elevated)) - 1)
    sub_index = elevated[index]
    if not _SUB_INDEX_MIN <= sub_index <= _SUB_INDEX_MAX:
        return None
    if sub_index < limits.threshold_floor:
        return None
    return LearnedThreshold(
        species=species,
        sub_index=sub_index,
        lag_days=lag,
        observations=len(pairs),
    )


__all__ = [
    "DEFAULT_ASSOCIATION_LAGS",
    "DEFAULT_ASSOCIATION_LIMITS",
    "DEFAULT_ELEVATED_SEVERITY",
    "DEFAULT_MIN_OBSERVATIONS",
    "DEFAULT_MIN_STRENGTH",
    "DEFAULT_READINGS_RETENTION_DAYS",
    "DEFAULT_THRESHOLD_FLOOR",
    "AssociationLimits",
    "AssociationReport",
    "AssociationShortfall",
    "ExposureAssociation",
    "ExposureObservation",
    "LearnedThreshold",
    "compute_association_report",
]
