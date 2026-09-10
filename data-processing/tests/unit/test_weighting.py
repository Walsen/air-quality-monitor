"""Unit tests for Condition_Weighting (task 18.1).

Requirements 21.1 through 21.8.

The load-bearing fact in these tests: Requirement 21.2 names `O3` for asthma, copd and
asthma_copd_overlap, but `O3` is NOT a contract Species — the network measures NO2 and PM25
only. That is not a spec slip, it is precisely what Requirements 21.4 and 21.5 exist for: the
weighting states what matters CLINICALLY for a condition, and a named species the response
cannot carry must show up in `unavailableWeightedSpecies` rather than quietly vanishing. So
several tests here pin O3's presence in the map and its appearance in the unavailable list.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import get_args

import pytest

from aqm_ingestion.contract.records import SpeciesName
from aqm_ingestion.domain.profile import Condition
from aqm_ingestion.domain.weighting import (
    DEFAULT_CONDITION_WEIGHTINGS,
    ConditionWeighting,
    ConditionWeightingRegistry,
    PollenRelevance,
    WeightedFocus,
    compute_weighted_focus,
    order_species,
)

_MEASURED = ("PM25", "NO2")
_CONTRACT_SPECIES = frozenset(get_args(SpeciesName))


# --- Req 21.2: the default map, transcribed from the requirement ---------

def test_asthma_weighting_matches_the_requirement() -> None:
    weighting = DEFAULT_CONDITION_WEIGHTINGS[Condition.ASTHMA]
    assert weighting.species == ("PM25", "O3", "NO2")
    assert weighting.pollen is PollenRelevance.RELEVANT


def test_copd_weighting_matches_the_requirement() -> None:
    weighting = DEFAULT_CONDITION_WEIGHTINGS[Condition.COPD]
    assert weighting.species == ("NO2", "O3", "PM25")
    assert weighting.pollen is PollenRelevance.NOT_RELEVANT


def test_allergic_rhinitis_weighting_matches_the_requirement() -> None:
    weighting = DEFAULT_CONDITION_WEIGHTINGS[Condition.ALLERGIC_RHINITIS]
    assert weighting.species == ("PM25", "NO2")
    # Req 21.2 says "relevant AND primary" for this condition alone, which a bare boolean
    # could not express — hence three states rather than two.
    assert weighting.pollen is PollenRelevance.PRIMARY


def test_asthma_copd_overlap_weighting_matches_the_requirement() -> None:
    weighting = DEFAULT_CONDITION_WEIGHTINGS[Condition.ASTHMA_COPD_OVERLAP]
    assert weighting.species == ("NO2", "PM25", "O3")
    assert weighting.pollen is PollenRelevance.RELEVANT


def test_none_declared_weighting_matches_the_requirement() -> None:
    weighting = DEFAULT_CONDITION_WEIGHTINGS[Condition.NONE_DECLARED]
    assert weighting.species == ("PM25", "NO2")
    assert weighting.pollen is PollenRelevance.NOT_RELEVANT


def test_every_condition_has_a_weighting() -> None:
    # A missing entry would make a lawful profile unservable, so completeness is the contract.
    assert set(DEFAULT_CONDITION_WEIGHTINGS) == set(Condition)


def test_copd_leads_with_the_gaseous_drivers_and_asthma_with_particulates() -> None:
    # Req 21.2 grounds the orders in a specific finding; this pins the DISTINCTION rather than
    # the literal tuples above, so a copy-paste that made both maps identical fails here.
    assert DEFAULT_CONDITION_WEIGHTINGS[Condition.COPD].species[0] == "NO2"
    assert DEFAULT_CONDITION_WEIGHTINGS[Condition.ASTHMA].species[0] == "PM25"


# --- The O3 point -------------------------------------------------------

def test_the_map_names_a_species_the_network_does_not_measure() -> None:
    # Guards the decision itself: typing the weighting on the contract's SpeciesName would make
    # O3 INEXPRESSIBLE and silently delete the clinical fact Req 21.2 records.
    assert "O3" not in _CONTRACT_SPECIES
    assert "O3" in DEFAULT_CONDITION_WEIGHTINGS[Condition.ASTHMA].species


def test_an_unmeasured_weighted_species_is_reported_not_dropped() -> None:
    focus = compute_weighted_focus(
        DEFAULT_CONDITION_WEIGHTINGS[Condition.ASTHMA], available=_MEASURED
    )
    assert focus.focus == ("PM25", "NO2")
    assert focus.unavailable == ("O3",)


def test_no_proxy_is_substituted_for_an_unavailable_species() -> None:
    # Req 21.5: the focus holds only species genuinely present, never a stand-in.
    focus = compute_weighted_focus(
        DEFAULT_CONDITION_WEIGHTINGS[Condition.COPD], available=("NO2",)
    )
    assert focus.focus == ("NO2",)
    assert focus.unavailable == ("O3", "PM25")
    assert all(species in ("NO2",) for species in focus.focus)


# --- Req 21.3: the focus preserves the weighting's order ----------------

def test_the_focus_preserves_the_weightings_order_not_the_available_order() -> None:
    # The available set is deliberately given in the OPPOSITE order, so an implementation that
    # iterated the available species instead of the weighting fails.
    focus = compute_weighted_focus(
        DEFAULT_CONDITION_WEIGHTINGS[Condition.COPD], available=("PM25", "NO2")
    )
    assert focus.focus == ("NO2", "PM25")


def test_the_focus_is_empty_when_nothing_is_available() -> None:
    focus = compute_weighted_focus(
        DEFAULT_CONDITION_WEIGHTINGS[Condition.ASTHMA], available=()
    )
    assert focus.focus == ()
    assert focus.unavailable == ("PM25", "O3", "NO2")


def test_an_available_species_the_weighting_does_not_name_is_not_in_the_focus() -> None:
    # The focus is the WEIGHTING restricted by availability, not the intersection presented in
    # availability order — an extra measured species is ordered by precedence (Req 21.7).
    focus = compute_weighted_focus(
        DEFAULT_CONDITION_WEIGHTINGS[Condition.NONE_DECLARED],
        available=("PM25", "NO2", "SO2"),
    )
    assert focus.focus == ("PM25", "NO2")
    assert "SO2" not in focus.focus


def test_availability_is_order_insensitive_for_the_unavailable_list() -> None:
    weighting = DEFAULT_CONDITION_WEIGHTINGS[Condition.ASTHMA]
    first = compute_weighted_focus(weighting, available=("NO2", "PM25"))
    second = compute_weighted_focus(weighting, available=("PM25", "NO2"))
    assert first == second


def test_the_focus_is_the_same_on_every_evaluation() -> None:
    # Req 21.8. Pure function, so this is a regression guard against a future cache or clock.
    weighting = DEFAULT_CONDITION_WEIGHTINGS[Condition.ASTHMA]
    results = {compute_weighted_focus(weighting, available=_MEASURED) for _ in range(5)}
    assert len(results) == 1


# --- Req 21.7: focus first, configured precedence second ----------------

def test_ordering_puts_the_focus_first_then_precedence() -> None:
    ordered = order_species(
        ("SO2", "NO2", "PM25"), focus=("NO2", "PM25"), precedence=("PM25", "NO2", "SO2")
    )
    assert ordered == ("NO2", "PM25", "SO2")


def test_ordering_falls_back_to_precedence_for_unfocused_species() -> None:
    # With an empty focus the result must be exactly the configured precedence order, which is
    # the reverse of alphabetical for PM25/NO2 — the same trap task 13 hit in the store.
    ordered = order_species(
        ("NO2", "PM25"), focus=(), precedence=("PM25", "NO2")
    )
    assert ordered == ("PM25", "NO2")


def test_ordering_is_tested_in_both_input_orders() -> None:
    # A stable sort can pass for real precedence if only one input order is tried.
    for given in (("NO2", "PM25", "SO2"), ("SO2", "PM25", "NO2")):
        assert order_species(
            given, focus=("NO2",), precedence=("PM25", "NO2", "SO2")
        ) == ("NO2", "PM25", "SO2")


def test_a_species_in_neither_focus_nor_precedence_sorts_last_by_name() -> None:
    ordered = order_species(
        ("ZZ", "AA", "PM25"), focus=(), precedence=("PM25",)
    )
    assert ordered == ("PM25", "AA", "ZZ")


def test_ordering_returns_every_species_it_was_given() -> None:
    given = ("SO2", "NO2", "PM25", "O3")
    ordered = order_species(given, focus=("NO2",), precedence=("PM25", "NO2"))
    assert sorted(ordered) == sorted(given)


def test_ordering_never_invents_a_species_absent_from_the_input() -> None:
    # The counterpart: the focus names PM25 but the site does not carry it, and ordering must
    # not conjure it. Without this, "focus first" could be read as "focus, then the rest".
    ordered = order_species(("NO2",), focus=("PM25", "NO2"), precedence=("PM25", "NO2"))
    assert ordered == ("NO2",)


# --- Req 21.9: weighting cannot alter a value ---------------------------

def test_the_weighting_module_returns_only_species_names() -> None:
    # Structural reading of Req 21.9: these functions deal in NAMES and ORDER, so there is no
    # return path by which a Sub_Index, Band or corrected value could be changed.
    focus_return = inspect.signature(compute_weighted_focus).return_annotation
    order_return = inspect.signature(order_species).return_annotation
    assert focus_return is WeightedFocus or focus_return == "WeightedFocus"
    assert "str" in str(order_return)


def test_weighted_focus_carries_no_value_field() -> None:
    assert set(WeightedFocus.__dataclass_fields__) == {"focus", "unavailable"}


def test_condition_weighting_carries_no_value_field() -> None:
    assert set(ConditionWeighting.__dataclass_fields__) == {"species", "pollen"}


# --- Req 21.1: the registry is open/closed ------------------------------

def test_a_new_condition_resolves_without_changing_resolution() -> None:
    registry = ConditionWeightingRegistry.with_defaults()
    registry.register(
        "wildcard_condition",
        ConditionWeighting(species=("NO2",), pollen=PollenRelevance.NOT_RELEVANT),
    )
    assert registry.resolve("wildcard_condition").species == ("NO2",)


def test_resolution_compares_no_condition_literal() -> None:
    # §1 open/closed ASSERTED not claimed, the same AST check the calibration registry carries:
    # a chain of `if condition == "asthma"` would pass every behavioural test above.
    source = Path(inspect.getfile(ConditionWeightingRegistry)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    known = {member.value for member in Condition}
    inspected = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "resolve":
            continue
        inspected += 1
        for inner in ast.walk(node):
            if isinstance(inner, ast.Constant) and inner.value in known:
                pytest.fail(
                    f"resolve() compares the condition literal {inner.value!r}; "
                    "a new condition must be a registry entry, not a branch"
                )

    # Without this the check passes happily if resolve is ever renamed — the rule would be
    # silently unenforced while still appearing green.
    assert inspected == 1, f"expected one resolve() to inspect, found {inspected}"


def test_duplicate_registration_is_refused() -> None:
    # Silent replacement would make the resolved weighting depend on import order (§2).
    registry = ConditionWeightingRegistry.with_defaults()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            Condition.ASTHMA,
            ConditionWeighting(species=("NO2",), pollen=PollenRelevance.NOT_RELEVANT),
        )


def test_an_unknown_condition_is_refused_naming_the_known_ones() -> None:
    registry = ConditionWeightingRegistry.with_defaults()
    with pytest.raises(KeyError) as caught:
        registry.resolve("emphysema")
    message = str(caught.value)
    assert "emphysema" in message
    assert "asthma" in message


def test_the_known_names_are_sorted_so_an_error_is_reproducible() -> None:
    names = ConditionWeightingRegistry.with_defaults().names()
    assert list(names) == sorted(names)


def test_the_registry_resolves_every_condition_by_enum_or_string() -> None:
    registry = ConditionWeightingRegistry.with_defaults()
    for condition in Condition:
        assert registry.resolve(condition) == registry.resolve(condition.value)


def test_an_empty_species_list_is_refused() -> None:
    # A weighting that names nothing could not order anything; it is a config error, not a
    # neutral default — none_declared already covers "no condition declared".
    with pytest.raises(ValueError, match="at least one species"):
        ConditionWeighting(species=(), pollen=PollenRelevance.NOT_RELEVANT)


def test_a_duplicate_species_in_a_weighting_is_refused() -> None:
    # Otherwise the focus order would depend on which occurrence was consulted.
    with pytest.raises(ValueError, match="duplicate"):
        ConditionWeighting(
            species=("PM25", "PM25"), pollen=PollenRelevance.NOT_RELEVANT
        )


# --- Req 21.6: pollen relevance is readable off the weighting -----------

def test_pollen_relevance_has_a_primary_state_distinct_from_relevant() -> None:
    # Three distinct states, both of the upper two counting as relevant. Asserting
    # `PRIMARY is not RELEVANT` would be a tautology mypy rightly rejects; what carries content
    # is that the count is three and that is_relevant groups two of them.
    assert len(PollenRelevance) == 3
    assert {m for m in PollenRelevance if m.is_relevant} == {
        PollenRelevance.RELEVANT,
        PollenRelevance.PRIMARY,
    }
    assert not PollenRelevance.NOT_RELEVANT.is_relevant


def test_exactly_the_requirements_conditions_mark_pollen_relevant() -> None:
    relevant = {
        condition
        for condition, weighting in DEFAULT_CONDITION_WEIGHTINGS.items()
        if weighting.pollen.is_relevant
    }
    assert relevant == {
        Condition.ASTHMA,
        Condition.ALLERGIC_RHINITIS,
        Condition.ASTHMA_COPD_OVERLAP,
    }
