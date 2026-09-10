"""Tests for the action registry and condition-weighted interpretation (tasks 7.1, 7.2).

Two structural rules carry most of the weight, and both are AST checks because both describe how
the code is SHAPED rather than what it returns for one input:

* Req 12.5 makes the mapping a registry keyed by condition, so a new condition is an ENTRY
  rather than a branch.
  A test asserts `actions_for` compares no condition-name literal — the moment it does, adding a
  condition means editing a conditional, which is what the requirement exists to prevent.
* Req 12.1/12.2 forbid computing a weighting and require the species order Service 2 returned,
  because that
  order is its configured clinical precedence. A test asserts nothing in the module SORTS the
  focus list. Sorting would look tidy and would silently replace clinical precedence with
  alphabetical order.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from aqm_advisor.domain.actions import (
    CONDITION_ACTIONS,
    ActionTemplate,
    actions_for,
)
from aqm_advisor.domain.interpretation import (
    ConditionView,
    interpret_personalized,
)

_CONDITIONS = ("asthma", "copd", "allergic_rhinitis", "asthma_copd_overlap", "none_declared")


def _personalized(**kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {
        "condition": "asthma",
        "sensitivity": "standard",
        "usedDefaultProfile": False,
        "weightedFocus": ["PM25", "O3"],
        "unavailableWeightedSpecies": ["O3"],
        "escalationSubIndex": 101,
        "thresholdCrossed": False,
        "thresholdSource": None,
        "crossings": [],
        "pollen": None,
        "inhaledDose": None,
        "doseBasis": "none",
        "inhaledDoseWindows": [],
        "unavailableDoseWindows": 0,
    }
    return {**base, **kwargs}


# --- Req 12.4, 12.5: the registry ---------------------------------------

def test_every_service_2_condition_has_an_entry() -> None:
    # Req 12.5's point: a condition Service 2 can serve but the registry has no entry for would
    # fall
    # through to nothing, and the user would get no actions at all.
    for condition in _CONDITIONS:
        assert condition in CONDITION_ACTIONS, f"no registry entry for {condition}"


def test_every_entry_has_at_least_one_action() -> None:
    # An empty tuple would satisfy the test above while providing nothing.
    for condition, templates in CONDITION_ACTIONS.items():
        assert templates, f"{condition} has no actions"


def test_actions_are_returned_for_a_known_condition() -> None:
    assert actions_for("asthma", driving="PM25") != ()


def test_an_unknown_condition_yields_no_actions_rather_than_raising() -> None:
    # Req 12.4 forbids inventing an action outside the mapping. A condition the registry does
    # not know
    # must therefore produce nothing — and must not break the turn, since a profile value this
    # service
    # does not recognise is a Service 2 change, not a user error.
    assert actions_for("something_new", driving="PM25") == ()


def test_resolution_compares_no_condition_name_literal() -> None:
    # Req 12.5 as a SHAPE. The moment `actions_for` contains `if condition == "asthma"`, adding
    # a
    # condition means editing a conditional rather than adding an entry — which is exactly what
    # the
    # requirement forbids, and the kind of drift a behavioural test cannot see.
    from aqm_advisor.domain import actions as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "actions_for"
    )
    literals = {
        node.value
        for node in ast.walk(target)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    offenders = literals & set(_CONDITIONS)
    assert offenders == set(), f"actions_for branches on {offenders}"


def test_the_literal_detector_would_catch_a_branch() -> None:
    # Self-check: without it, a detector looking in the wrong place would pass on a function
    # that DID
    # branch, and report a structural guarantee it was not providing.
    source = 'def actions_for(condition):\n    if condition == "asthma":\n        return ()\n'
    planted = ast.parse(source)
    target = next(n for n in ast.walk(planted) if isinstance(n, ast.FunctionDef))
    literals = {
        n.value
        for n in ast.walk(target)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert literals & set(_CONDITIONS) == {"asthma"}


def test_the_overlap_condition_is_its_own_entry() -> None:
    # Req 12.6: never assume the condition is one diagnostic category, and for an overlap apply
    # what
    # Service 2 returned rather than choosing a side. An entry of its own is how the registry
    # avoids
    # picking; deriving it by merging asthma's and copd's lists would be this service choosing.
    assert CONDITION_ACTIONS["asthma_copd_overlap"] != CONDITION_ACTIONS["asthma"]
    assert CONDITION_ACTIONS["asthma_copd_overlap"] != CONDITION_ACTIONS["copd"]


def test_a_driving_pollutant_can_select_a_species_specific_action() -> None:
    templates = actions_for("asthma", driving="PM25")
    assert any(t.species == "PM25" for t in templates)


def test_a_species_specific_action_for_another_pollutant_is_not_returned() -> None:
    # Non-vacuity for the test above: if every template came back regardless of the driver, the
    # `species` field would be decoration.
    templates = actions_for("asthma", driving="PM25")
    assert all(t.species in (None, "PM25") for t in templates)


def test_actions_with_no_driving_pollutant_are_the_general_ones() -> None:
    templates = actions_for("asthma", driving=None)
    assert templates != ()
    assert all(t.species is None for t in templates)


def test_no_action_text_is_an_instruction_about_medication_or_exercise() -> None:
    # Req 8.4 and Req 14.4. Every template ships in guidance, so a single careless one would put
    # a
    # forbidden claim into the product by construction rather than by an unlucky generation.
    from aqm_advisor.domain.forbidden import administration_near_medication, forbidden_matches

    for condition, templates in CONDITION_ACTIONS.items():
        for template in templates:
            assert forbidden_matches(template.text) == (), f"{condition}: {template.text!r}"
            assert not administration_near_medication(template.text), (
                f"{condition}: {template.text!r}"
            )


def test_no_action_text_states_a_number() -> None:
    # Every numeral in guidance must be a Retrieved_Value (Req 7.1). A template carrying its own
    # number
    # could never be grounded, because no retrieval would ever return it.
    from aqm_advisor.domain.grounding import numerals

    for condition, templates in CONDITION_ACTIONS.items():
        for template in templates:
            assert numerals(template.text) == (), f"{condition}: {template.text!r}"


# --- Req 12.1, 12.2, 12.3: interpretation -------------------------------

def test_the_weighted_focus_order_is_preserved() -> None:
    # Req 12.2: that order is Service 2's configured clinical precedence, not a set.
    view = interpret_personalized(_personalized(weightedFocus=["O3", "PM25", "NO2"]))
    assert view.weighted_focus == ("O3", "PM25", "NO2")


def test_the_focus_order_is_preserved_for_a_second_arrangement() -> None:
    # Tested in both orders, so a stable-but-wrong ordering (alphabetical, say) cannot pass by
    # luck.
    view = interpret_personalized(_personalized(weightedFocus=["NO2", "PM25", "O3"]))
    assert view.weighted_focus == ("NO2", "PM25", "O3")


def test_the_interpretation_module_never_sorts_the_focus() -> None:
    # Req 12.1's "SHALL NOT compute a weighting" as a shape. Sorting looks tidy and would
    # silently
    # replace clinical precedence with alphabetical order — a change nobody would notice in
    # review.
    from aqm_advisor.domain import interpretation as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    called = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for forbidden in ("sorted", "list.sort", "min", "max", "sum"):
        assert forbidden not in called, f"the interpretation calls {forbidden}"


def test_unavailable_weighted_species_are_reported() -> None:
    # Req 12.3: Service 2 reports them precisely so the absence is explicit rather than
    # invisible.
    view = interpret_personalized(_personalized(unavailableWeightedSpecies=["O3", "SO2"]))
    assert view.unavailable_weighted == ("O3", "SO2")


def test_an_unavailable_species_stays_in_the_focus_list() -> None:
    # It matters for the condition even though it is not measured, so dropping it from the focus
    # would
    # hide exactly what Req 12.3 exists to surface.
    view = interpret_personalized(
        _personalized(weightedFocus=["PM25", "O3"], unavailableWeightedSpecies=["O3"])
    )
    assert "O3" in view.weighted_focus
    assert "O3" in view.unavailable_weighted


def test_the_condition_and_sensitivity_are_read_not_derived() -> None:
    view = interpret_personalized(_personalized(condition="copd", sensitivity="high"))
    assert view.condition == "copd"
    assert view.sensitivity == "high"


def test_an_overlap_condition_uses_the_returned_weighting() -> None:
    # Req 12.6: apply what Service 2 returned rather than choosing a side. The view carries the
    # served
    # order untouched, so there is no place for this service to pick.
    view = interpret_personalized(
        _personalized(condition="asthma_copd_overlap", weightedFocus=["PM25", "NO2", "O3"])
    )
    assert view.condition == "asthma_copd_overlap"
    assert view.weighted_focus == ("PM25", "NO2", "O3")
    assert view.actions != ()


def test_a_missing_personalized_block_yields_an_empty_view() -> None:
    # Req 21.7 advises on what it has. A degraded retrieval must not make interpretation raise.
    view = interpret_personalized(None)
    assert view.condition is None
    assert view.weighted_focus == ()
    assert view.actions == ()


def test_a_partial_block_does_not_raise() -> None:
    view = interpret_personalized({"condition": "asthma"})
    assert view.condition == "asthma"
    assert view.weighted_focus == ()


def test_the_view_is_frozen() -> None:
    view = interpret_personalized(_personalized())
    with pytest.raises((AttributeError, TypeError)):
        view.condition = "copd"  # type: ignore[misc]


def test_the_view_carries_the_actions_for_its_condition() -> None:
    view = interpret_personalized(_personalized(condition="copd"))
    assert view.actions == actions_for("copd", driving=None)


def test_the_driving_pollutant_selects_the_actions_when_supplied() -> None:
    view = interpret_personalized(_personalized(condition="asthma"), driving="PM25")
    assert view.actions == actions_for("asthma", driving="PM25")


def test_an_action_template_is_frozen() -> None:
    template = CONDITION_ACTIONS["asthma"][0]
    assert isinstance(template, ActionTemplate)
    with pytest.raises((AttributeError, TypeError)):
        template.text = "changed"  # type: ignore[misc]


_DERIVED_WEIGHTING_TOKENS = frozenset(
    {"weight", "weights", "score", "scores", "rank", "ranking", "priority"}
)


def _weighting_offenders(field_names: set[str]) -> set[str]:
    """Field names that could hold a weighting THIS service derived.

    Matched against underscore-separated TOKENS, not as substrings. A substring sweep is wrong
    here and not hypothetically so: `weight` occurs inside `weighted_focus` and
    `unavailable_weighted`, both of which Req 12.2 and 12.3 REQUIRE. `weighted` is Service 2's
    own field name — carrying its weighting is the whole point; deriving one of our own is what
    is forbidden.

    Third occurrence of this collision in the project: Service 2's logger redacted a configured
    medication CAP as a medication, and this service's audit sweep condemned `escalated` for
    containing `lat`. The rule is now explicit: sweep tokens, and carry a near-miss guard.
    """
    offenders: set[str] = set()
    for name in field_names:
        if set(name.split("_")) & _DERIVED_WEIGHTING_TOKENS:
            offenders.add(name)
    return offenders


def test_the_condition_view_carries_no_computed_weighting() -> None:
    # Structural: there is nowhere on the view to put a score, rank or weight this service
    # derived.
    assert _weighting_offenders(set(ConditionView.__dataclass_fields__)) == set()


def test_the_weighting_sweep_would_catch_a_derived_field() -> None:
    # Non-vacuity. Without it a sweep whose token list never matched would pass on a view that
    # DID
    # carry a derived score.
    assert _weighting_offenders({"condition", "risk_score", "species_weight"}) == {
        "risk_score",
        "species_weight",
    }


def test_the_weighting_sweep_does_not_fire_on_service_2s_own_field_names() -> None:
    # The near-miss guard, and the reason the sweep matches tokens. Both of these are required
    # fields,
    # and a substring sweep would condemn them — after which the "fix" is to delete required
    # data.
    assert _weighting_offenders({"weighted_focus", "unavailable_weighted"}) == set()
