"""Unit tests for the calibration strategy protocol and registry (task 7.1).

- 8.2: strategies are selected BY NAME from a registry; an absent name is rejected
  naming the supplied value and the registered names.
- 8.6: `identity` is the no-humidity fallback.
- 8.12: `identity` is the NO2 default, because the humidity confounding that motivates
  the PM2.5 correction is specific to optical particle counting.

The registry exists to keep §1's open/closed promise: a new strategy must be one
registration, never another arm in a conditional. A test asserts the resolution path
holds no per-strategy branching.
"""

from __future__ import annotations

import pytest

from aqm_ingestion.domain.calibration import (
    DEFAULT_NO_HUMIDITY_STRATEGY,
    DEFAULT_SPECIES_STRATEGY,
    CalibrationDomain,
    CalibrationRegistry,
    IdentityStrategy,
    UnknownStrategyError,
)


def _registry() -> CalibrationRegistry:
    return CalibrationRegistry.with_defaults()


# --- Req 8.2 resolution by name ------------------------------------------

def test_identity_resolves_by_name() -> None:
    assert _registry().resolve("identity").name == "identity"


def test_registered_names_are_reported_in_a_stable_order() -> None:
    # §2: iteration order that reaches output (an error message) must be defined
    assert _registry().names() == tuple(sorted(_registry().names()))


def test_unknown_name_is_refused_naming_the_value_and_the_options() -> None:
    # Req 8.2: the Config_Loader needs both halves to write a useful message
    with pytest.raises(UnknownStrategyError) as caught:
        _registry().resolve("does_not_exist")
    message = str(caught.value)
    assert "does_not_exist" in message
    assert "identity" in message


def test_unknown_strategy_error_carries_the_parts_separately() -> None:
    # §5: a caller should not have to parse the message to build its own
    with pytest.raises(UnknownStrategyError) as caught:
        _registry().resolve("nope")
    assert caught.value.requested == "nope"
    assert "identity" in caught.value.registered


def test_registering_a_new_strategy_needs_no_change_to_resolution() -> None:
    # §1 open/closed, asserted rather than asserted-in-prose: a strategy the registry
    # has never heard of becomes resolvable purely by registering it
    registry = CalibrationRegistry()
    registry.register(IdentityStrategy(name="passthrough"))
    assert registry.resolve("passthrough").name == "passthrough"


def test_registering_a_duplicate_name_is_refused() -> None:
    # silently replacing would make the resolved strategy depend on import order
    registry = CalibrationRegistry()
    registry.register(IdentityStrategy(name="dup"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(IdentityStrategy(name="dup"))


def test_resolution_holds_no_per_strategy_branching() -> None:
    # the registry must be a lookup, not a disguised if/elif chain (§1)
    import ast
    import inspect

    source = inspect.getsource(CalibrationRegistry.resolve)
    tree = ast.parse(source.strip())
    comparisons = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(
            isinstance(c, ast.Constant) and isinstance(c.value, str)
            for c in node.comparators
        )
    ]
    assert comparisons == [], "resolve compares against a strategy name literal"


# --- Req 8.6 / 8.12 identity ---------------------------------------------

def test_identity_returns_the_reported_value_unchanged() -> None:
    assert _registry().resolve("identity").correct(reported=42.5, rh=55.0) == 42.5


def test_identity_ignores_rh_entirely() -> None:
    identity = _registry().resolve("identity")
    assert identity.correct(reported=10.0, rh=None) == 10.0
    assert identity.correct(reported=10.0, rh=0.0) == 10.0
    assert identity.correct(reported=10.0, rh=100.0) == 10.0


def test_identity_clamps_a_negative_reported_value() -> None:
    # Req 8.9 applies to every strategy: a corrected value is never below zero
    assert _registry().resolve("identity").correct(reported=-3.0, rh=None) == 0.0


def test_identity_is_the_documented_no_humidity_fallback() -> None:
    assert DEFAULT_NO_HUMIDITY_STRATEGY == "identity"


def test_identity_is_the_documented_no2_default() -> None:
    assert DEFAULT_SPECIES_STRATEGY["NO2"] == "identity"


# --- the Calibration_Domain ----------------------------------------------

def test_identity_domain_admits_every_finite_input() -> None:
    # identity applies no humidity correction, so nothing about it is extrapolation
    domain = _registry().resolve("identity").domain
    assert domain.contains(reported=0.0, rh=None)
    assert domain.contains(reported=10_000.0, rh=100.0)


def test_domain_rejects_an_inverted_bound() -> None:
    # Req 8.13: a minimum at or above its maximum is a configuration error
    with pytest.raises(ValueError, match="minimum"):
        CalibrationDomain(
            reported_min=250.0, reported_max=0.0, rh_min=20.0, rh_max=90.0
        )


def test_domain_rejects_a_non_finite_bound() -> None:
    with pytest.raises(ValueError, match="finite"):
        CalibrationDomain(
            reported_min=0.0, reported_max=float("inf"), rh_min=20.0, rh_max=90.0
        )


def test_domain_reports_which_input_is_out_of_bounds() -> None:
    # Req 8.7 wants the warning to name the input AND its bound
    domain = CalibrationDomain(
        reported_min=0.0, reported_max=250.0, rh_min=20.0, rh_max=90.0
    )
    breaches = domain.breaches(reported=300.0, rh=95.0)
    described = " ".join(breaches)
    assert "300" in described
    assert "250" in described
    assert "95" in described
    assert "90" in described


def test_domain_reports_no_breach_for_an_in_domain_input() -> None:
    domain = CalibrationDomain(
        reported_min=0.0, reported_max=250.0, rh_min=20.0, rh_max=90.0
    )
    assert domain.breaches(reported=100.0, rh=50.0) == ()
    assert domain.contains(reported=100.0, rh=50.0)


def test_domain_boundaries_are_inclusive() -> None:
    # a reading exactly at a published bound is within the fitted range, not beyond it
    domain = CalibrationDomain(
        reported_min=0.0, reported_max=250.0, rh_min=20.0, rh_max=90.0
    )
    assert domain.contains(reported=0.0, rh=20.0)
    assert domain.contains(reported=250.0, rh=90.0)


def test_absent_rh_is_not_a_domain_breach() -> None:
    # missing RH is Req 8.6's uncalibrated path, a different condition from Req 8.7's
    # extrapolation — conflating them would mislabel the reading
    domain = CalibrationDomain(
        reported_min=0.0, reported_max=250.0, rh_min=20.0, rh_max=90.0
    )
    assert domain.breaches(reported=100.0, rh=None) == ()
