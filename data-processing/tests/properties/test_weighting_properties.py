"""Property tests for Condition_Weighting.

Feature: ingestion-and-serving-service, Property 32.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.domain.profile import Condition
from aqm_ingestion.domain.weighting import (
    DEFAULT_CONDITION_WEIGHTINGS,
    ConditionWeightingRegistry,
    compute_weighted_focus,
    order_species,
)

# A pool wider than the contract's species, so the property also covers a weighting naming
# something the network does not measure (Requirement 21.2's O3) and a measured species no
# weighting names.
_SPECIES_POOL = ("PM25", "NO2", "O3", "SO2", "CO")

_species_sets = st.lists(
    st.sampled_from(_SPECIES_POOL), min_size=0, max_size=len(_SPECIES_POOL), unique=True
)
_conditions = st.sampled_from(list(Condition))


@given(condition=_conditions, available=_species_sets)
def test_property_32_weighting_orders_without_altering_values(
    condition: Condition, available: list[str]
) -> None:
    """Feature: ingestion-and-serving-service, Property 32.

    Weighting orders the response without altering any value.
    Validates Requirements 21.3, 21.4, 21.7 and 21.9.

    Requirement 21.9's "alters no value" is asserted here as a PERMUTATION property: ordering
    returns exactly the multiset it was given. That is the strongest statement available at this
    layer, because the module deals only in species names — and it is the assertion that would
    fail if ordering ever dropped, duplicated or invented an entry, each of which would change
    what a response reports even without touching a number.
    """
    weighting = ConditionWeightingRegistry.with_defaults().resolve(condition)
    focus = compute_weighted_focus(weighting, available=available)
    ordered = order_species(available, focus=focus.focus, precedence=("PM25", "NO2"))

    # Req 21.9: nothing added, nothing lost, nothing duplicated.
    assert sorted(ordered) == sorted(available)

    # Req 21.3: the focus is the weighting's order restricted to what is present.
    assert list(focus.focus) == [s for s in weighting.species if s in set(available)]

    # Req 21.4: focus and unavailable together account for every species the weighting names,
    # and they never overlap — either half alone is trivially satisfiable.
    assert set(focus.focus) | set(focus.unavailable) == set(weighting.species)
    assert not set(focus.focus) & set(focus.unavailable)

    # Req 21.7: every focused species precedes every unfocused one that is present.
    positions = {name: index for index, name in enumerate(ordered)}
    unfocused = [s for s in available if s not in focus.focus]
    if focus.focus and unfocused:
        assert max(positions[s] for s in focus.focus) < min(
            positions[s] for s in unfocused
        )

    # Req 21.3 again, on the ordering rather than the focus: the focused prefix appears in the
    # weighting's own order, not merely grouped ahead of the rest.
    assert tuple(ordered[: len(focus.focus)]) == focus.focus


@given(condition=_conditions, available=_species_sets)
def test_property_32_focus_is_independent_of_availability_order(
    condition: Condition, available: list[str]
) -> None:
    """Feature: ingestion-and-serving-service, Property 32.

    The same Condition and available species always yield the same Weighted_Focus
    (Requirement 21.8), whatever order availability is presented in.

    Separate from the main property because Requirement 21.8 is about REPEATABILITY, and folding
    it into the assertion above would let a single evaluation stand for a claim about many.
    """
    weighting = DEFAULT_CONDITION_WEIGHTINGS[condition]
    forwards = compute_weighted_focus(weighting, available=available)
    backwards = compute_weighted_focus(weighting, available=list(reversed(available)))
    assert forwards == backwards
    assert compute_weighted_focus(weighting, available=available) == forwards
