"""Condition_Weighting: which pollutants a condition foregrounds, and in what order.

Requirement 21. Pure domain logic — no clock, no adapters, no configuration object.

THE DESIGN DECISION WORTH RECORDING: a weighting's species are plain strings, NOT the contract's
`SpeciesName`. Requirement 21.2 names `O3` for asthma, copd and asthma_copd_overlap, and this
network measures only NO2 and PM25. Typing the weighting on `SpeciesName` would look tidier and
would make `O3` INEXPRESSIBLE — silently deleting the clinical fact the requirement records, and
with it the whole purpose of Requirements 21.4 and 21.5. A weighting says what matters for a
condition; availability is a separate question, answered per response. So O3 lands in
``unavailable`` every time, which is exactly the visibility Requirement 21.4 asks for.

Requirement 21.9 — the weighting may not alter any value — is upheld STRUCTURALLY rather than by
discipline: nothing in this module accepts or returns a concentration, Sub_Index or Band. It
deals only in species names and their order, so there is no path through which a value could
change.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from aqm_ingestion.domain.profile import Condition


class PollenRelevance(StrEnum):
    """Whether pollen matters for a condition, and how much (Requirement 21.2).

    THREE states, not a boolean: Requirement 21.2 marks pollen "relevant and primary" for
    allergic_rhinitis alone, and a flag could not carry that distinction — which Requirement
    24's enrichment needs in order to know whether pollen leads the advice or accompanies it.
    """

    NOT_RELEVANT = "not_relevant"
    RELEVANT = "relevant"
    PRIMARY = "primary"

    @property
    def is_relevant(self) -> bool:
        """True when Requirement 21.6's pollen enrichment applies."""
        return self is not PollenRelevance.NOT_RELEVANT


@dataclass(frozen=True, slots=True)
class ConditionWeighting:
    """An ordered species emphasis plus pollen relevance for one Condition."""

    species: tuple[str, ...]
    pollen: PollenRelevance

    def __post_init__(self) -> None:
        """Refuse a weighting that could not order anything or whose order is ambiguous."""
        if not self.species:
            raise ValueError(
                "a Condition_Weighting must name at least one species; "
                "'none_declared' already expresses an undeclared condition"
            )
        if len(set(self.species)) != len(self.species):
            raise ValueError(
                f"duplicate species in weighting {self.species!r}: the focus order would "
                "depend on which occurrence was consulted"
            )


@dataclass(frozen=True, slots=True)
class WeightedFocus:
    """The weighted species a response can actually carry, and those it cannot.

    Both halves are required by Requirement 21.4: reporting only what IS available would make
    the absence of a weighted pollutant invisible, which is the failure the requirement names.
    """

    focus: tuple[str, ...]
    unavailable: tuple[str, ...]


# Transcribed from Requirement 21.2. The orders are grounded in that requirement's cited finding
# (NO2 and O3 lead COPD exacerbation; PM2.5 and aeroallergens dominate allergic asthma and
# rhinitis), so they are not interchangeable and must not be "tidied" into one shared order.
DEFAULT_CONDITION_WEIGHTINGS: Mapping[Condition, ConditionWeighting] = {
    Condition.ASTHMA: ConditionWeighting(
        species=("PM25", "O3", "NO2"), pollen=PollenRelevance.RELEVANT
    ),
    Condition.COPD: ConditionWeighting(
        species=("NO2", "O3", "PM25"), pollen=PollenRelevance.NOT_RELEVANT
    ),
    Condition.ALLERGIC_RHINITIS: ConditionWeighting(
        species=("PM25", "NO2"), pollen=PollenRelevance.PRIMARY
    ),
    Condition.ASTHMA_COPD_OVERLAP: ConditionWeighting(
        species=("NO2", "PM25", "O3"), pollen=PollenRelevance.RELEVANT
    ),
    Condition.NONE_DECLARED: ConditionWeighting(
        species=("PM25", "NO2"), pollen=PollenRelevance.NOT_RELEVANT
    ),
}


class ConditionWeightingRegistry:
    """Resolves a Condition to its weighting (Requirement 21.1).

    A registry rather than a conditional so that adding a condition is one entry and no change
    to resolution (§1 open/closed) — a test AST-parses ``resolve`` and fails if it compares any
    condition literal, so the property is asserted rather than merely intended.
    """

    def __init__(self) -> None:
        """Start empty; use ``with_defaults`` for Requirement 21.2's map."""
        self._weightings: dict[str, ConditionWeighting] = {}

    @classmethod
    def with_defaults(cls) -> ConditionWeightingRegistry:
        """A registry carrying Requirement 21.2's default map."""
        registry = cls()
        for condition, weighting in DEFAULT_CONDITION_WEIGHTINGS.items():
            registry.register(condition, weighting)
        return registry

    def register(self, condition: Condition | str, weighting: ConditionWeighting) -> None:
        """Add a weighting.

        Raises:
            ValueError: if the condition is already registered. Silent replacement would make
                the resolved weighting depend on import order (§2 determinism).
        """
        key = str(condition)
        if key in self._weightings:
            raise ValueError(f"condition {key!r} is already registered")
        self._weightings[key] = weighting

    def resolve(self, condition: Condition | str) -> ConditionWeighting:
        """Return the weighting for a condition.

        Raises:
            KeyError: naming the condition and the registered ones, so an operator can see the
                difference between a typo and a genuinely unsupported condition.
        """
        key = str(condition)
        try:
            return self._weightings[key]
        except KeyError:
            raise KeyError(
                f"no Condition_Weighting registered for {key!r}; "
                f"registered: {', '.join(self.names())}"
            ) from None

    def names(self) -> tuple[str, ...]:
        """Registered condition names, sorted so any error message is reproducible."""
        return tuple(sorted(self._weightings))


def compute_weighted_focus(
    weighting: ConditionWeighting, available: Collection[str]
) -> WeightedFocus:
    """Restrict a weighting to the species a response carries (Requirements 21.3, 21.4).

    Iterates the WEIGHTING, not the available species, so the weighting's order is preserved
    whatever order availability arrives in — and so an available species the weighting does not
    name stays out of the focus, to be ordered by configured precedence instead (Requirement
    21.7).

    No proxy is ever substituted and no value inferred (Requirement 21.5): a named species that
    is absent appears in ``unavailable`` and nowhere else.
    """
    present = set(available)
    return WeightedFocus(
        focus=tuple(species for species in weighting.species if species in present),
        unavailable=tuple(
            species for species in weighting.species if species not in present
        ),
    )


def order_species(
    species: Iterable[str], focus: tuple[str, ...], precedence: tuple[str, ...]
) -> tuple[str, ...]:
    """Order a site's species by Weighted_Focus first, configured precedence second.

    Requirement 21.7. Only species actually given are returned — the focus is an ORDERING, not a
    demand that its species be present, so a focus naming an absent species must not conjure it.

    A species in neither the focus nor the precedence sorts last by name, which keeps the order
    total and defined (§2) rather than dependent on the caller's iteration order.
    """
    focus_rank = {name: index for index, name in enumerate(focus)}
    precedence_rank = {name: index for index, name in enumerate(precedence)}
    unranked = len(precedence_rank)

    def sort_key(name: str) -> tuple[int, int, str]:
        # Three tiers: in the focus (by focus order), else by configured precedence, else by
        # name. The name is the final tie-break so the result never depends on input order.
        return (
            focus_rank.get(name, len(focus_rank)),
            precedence_rank.get(name, unranked),
            name,
        )

    return tuple(sorted(species, key=sort_key))


__all__ = [
    "DEFAULT_CONDITION_WEIGHTINGS",
    "ConditionWeighting",
    "ConditionWeightingRegistry",
    "PollenRelevance",
    "WeightedFocus",
    "compute_weighted_focus",
    "order_species",
]
