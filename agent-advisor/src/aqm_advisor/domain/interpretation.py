"""Condition-weighted interpretation of the retrieved `personalized` block (Requirement 12).

This module READS. Req 12.1 forbids computing a weighting of its own, and Req 12.2 requires the
species in `weightedFocus` in the order Service 2 returned them, because that order is Service
2's configured clinical precedence rather than an arbitrary listing.

**Nothing here sorts.** A test asserts the module calls no `sorted`, `min`, `max` or `sum`,
because sorting the focus list would look tidy and would silently replace clinical precedence
with alphabetical order — a change nobody would question in review, and one that would reorder
which pollutant the guidance leads with.

**An unavailable weighted species stays in the focus list.** Req 12.3 has Service 2 report those
precisely so the absence is explicit rather than invisible: the species matters for the user's
condition and this network does not measure it. Dropping it from the focus would hide exactly
what the requirement exists to surface.

A missing or partial block yields an empty view rather than raising. Req 21.7 advises on what it
has, and a degraded retrieval must not turn into an exception on the composition path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from aqm_advisor.domain.actions import ActionTemplate, actions_for


@dataclass(frozen=True, slots=True)
class ConditionView:
    """What the retrieved `personalized` block says, plus the actions its condition maps to.

    There is deliberately nowhere to put a weight, score, rank or priority. Req 12.1 forbids
    computing a weighting, and a field able to hold one is an invitation to derive it — so the
    prohibition is structural and a test asserts over the field names.
    """

    condition: str | None
    sensitivity: str | None
    weighted_focus: tuple[str, ...] = ()
    unavailable_weighted: tuple[str, ...] = ()
    used_default_profile: bool = False
    actions: tuple[ActionTemplate, ...] = field(default_factory=tuple)


def _string_tuple(value: object) -> tuple[str, ...]:
    """Read a served list of species names, preserving ORDER and dropping nothing.

    Order is the whole point (Req 12.2). A non-list arrives as empty rather than raising: a
    shape this service does not expect is a Service 2 change, and the honest response is to
    carry no focus rather than to guess at one.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def interpret_personalized(
    personalized: Mapping[str, object] | None, driving: str | None = None
) -> ConditionView:
    """Read the retrieved `personalized` block into a view, computing no weighting.

    `driving` narrows the actions to the pollutant actually driving the index. Omitted, only the
    general actions come back, because a species-specific action asserts which pollutant is
    doing the damage and that assertion needs a basis.
    """
    if personalized is None:
        return ConditionView(condition=None, sensitivity=None)

    condition = _optional_string(personalized.get("condition"))
    return ConditionView(
        condition=condition,
        sensitivity=_optional_string(personalized.get("sensitivity")),
        weighted_focus=_string_tuple(personalized.get("weightedFocus")),
        unavailable_weighted=_string_tuple(personalized.get("unavailableWeightedSpecies")),
        used_default_profile=bool(personalized.get("usedDefaultProfile", False)),
        actions=actions_for(condition, driving=driving),
    )


__all__ = ["ConditionView", "interpret_personalized"]
