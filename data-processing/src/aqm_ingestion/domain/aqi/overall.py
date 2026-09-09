"""The Overall_AQI and the pollutant driving it.

The Overall_AQI is the MAXIMUM sub-index rather than an average, because the point of an
air quality index is the worst thing in the air: averaging a hazardous NO2 reading against
clean particulates would report air that is safe on aggregate and dangerous in fact.

Three shapes here exist to make a wrong answer unrepresentable rather than merely
untested:

- **An index species cannot form a contribution at all.** Requirement 1.7 forbids
  `NO2Index` and `PM25Index` from influencing any index this service computes, and task
  11.1 asks for that STRUCTURALLY. A filter inside the maximum would satisfy today's test
  and could be dropped by a later edit; refusing the species at construction means no
  caller anywhere can build one.
- **No contributions returns None, not a zero.** Requirement 12.6 rules out a zero or a
  default band explicitly, and for good reason: reporting `Good` for "we have no data"
  would actively mislead an advisory. Returning ``None`` means there is no object in
  existence that could carry a misleading 0.
- **A duplicate species is refused.** Two sub-indices for one species at one instant is a
  caller bug, and silently taking one of them would make the answer depend on argument
  order, which §2 forbids.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from aqm_ingestion.contract.records import (
    MASS_CONCENTRATION_SPECIES,
)
from aqm_ingestion.domain.aqi.subindex import band_name_for
from aqm_ingestion.domain.models import (
    Confidence,
    IndexMethod,
    lowest_confidence,
)

DEFAULT_SPECIES_PRECEDENCE: tuple[str, ...] = ("PM25", "NO2")
"""Requirement 12.2's default tie-break order.

Only ever consulted for a TIE. A species later in this order still drives the index
whenever its own sub-index is genuinely higher.
"""

_VALID_METHODS: frozenset[str] = frozenset({"nowcast", "hourly"})


@dataclass(frozen=True, slots=True)
class SpeciesContribution:
    """One species' Sub_Index at a site and instant.

    Validated on construction, which is where Requirement 1.7's exclusion of the index
    species is enforced — see the module docstring for why it is here rather than in the
    maximum.
    """

    species: str
    sub_index: int
    method: IndexMethod
    confidence: Confidence

    def __post_init__(self) -> None:
        """Refuse anything that must never reach an index path."""
        if self.species not in MASS_CONCENTRATION_SPECIES:
            # Named separately from an unknown species, because an index species is a
            # DELIBERATE exclusion rather than a typo, and the message should say so.
            if self.species in {"NO2Index", "PM25Index"}:
                raise ValueError(
                    f"{self.species} is an index species: Requirement 1.7 forbids using "
                    "the emitting network's own index values in any index this service "
                    "computes"
                )
            raise ValueError(
                f"unknown species {self.species!r}; a Sub_Index contribution must be one "
                f"of: {', '.join(sorted(MASS_CONCENTRATION_SPECIES))}"
            )
        if self.method not in _VALID_METHODS:
            raise ValueError(
                f"unknown Sub_Index method {self.method!r}; must be one of: "
                f"{', '.join(sorted(_VALID_METHODS))}"
            )


@dataclass(frozen=True, slots=True)
class OverallAqi:
    """The Overall_AQI with everything the Basis needs to explain it."""

    value: int
    band: str
    driving_pollutant: str
    confidence: Confidence
    methods: Mapping[str, IndexMethod]
    species_without_sub_index: tuple[str, ...]

    @property
    def driving_method(self) -> IndexMethod:
        """Whether the driving Sub_Index was NowCast-derived (Requirement 12.7)."""
        return self.methods[self.driving_pollutant]


def compute_overall_aqi(
    contributions: Sequence[SpeciesContribution],
    precedence: tuple[str, ...] = DEFAULT_SPECIES_PRECEDENCE,
) -> OverallAqi | None:
    """Compute the Overall_AQI and its Driving_Pollutant.

    Args:
        contributions: one entry per species with an available Sub_Index.
        precedence: the tie-break order (Requirement 12.2). A narrow parameter rather
            than a config object (§1).

    Returns:
        The Overall_AQI, or None when no Sub_Index is available at all
        (Requirement 12.6).

    Raises:
        ValueError: if a species appears more than once — see the module docstring.
    """
    seen = [contribution.species for contribution in contributions]
    duplicates = {species for species in seen if seen.count(species) > 1}
    if duplicates:
        raise ValueError(
            f"species appear more than once in one Overall_AQI: "
            f"{', '.join(sorted(duplicates))}"
        )

    if not contributions:
        return None

    highest = max(contribution.sub_index for contribution in contributions)
    tied = [
        contribution
        for contribution in contributions
        if contribution.sub_index == highest
    ]
    # Sorted by precedence rank, so the winner does not depend on argument order. A
    # species absent from the configured precedence sorts after every listed one and then
    # by name, so an incomplete configuration still yields a defined answer (§5).
    driving = min(
        tied,
        key=lambda contribution: (
            _precedence_rank(contribution.species, precedence),
            contribution.species,
        ),
    )

    present = {contribution.species for contribution in contributions}
    missing = tuple(
        sorted(species for species in MASS_CONCENTRATION_SPECIES if species not in present)
    )

    return OverallAqi(
        value=highest,
        band=band_name_for(highest),
        driving_pollutant=driving.species,
        # Requirement 12.8: the LOWEST contributing confidence, not the driving one. A
        # doubtful reading that happens not to drive still limits what the overall value
        # can be trusted to mean.
        confidence=lowest_confidence(
            contribution.confidence for contribution in contributions
        ),
        methods={
            contribution.species: contribution.method for contribution in contributions
        },
        species_without_sub_index=missing,
    )


def _precedence_rank(species: str, precedence: tuple[str, ...]) -> int:
    """Rank a species for the tie-break, unlisted species sorting last."""
    try:
        return precedence.index(species)
    except ValueError:
        return len(precedence)
