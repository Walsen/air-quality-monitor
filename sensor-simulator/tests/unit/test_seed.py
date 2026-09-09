"""Unit tests for seed selection and disclosure (task 6.4).

Requirement 11.7: when no seed is supplied, select a seed within
0..4,294,967,295 and record it in the diagnostic output before the first
record, so the run can be replayed. A supplied seed is used as-is.
"""

from __future__ import annotations

import pytest

from aqm_simulator.config.seed import select_seed


def test_supplied_seed_used_as_is() -> None:
    chosen, was_selected = select_seed(configured_seed=42, entropy=lambda: 999)
    assert chosen == 42
    assert was_selected is False


def test_absent_seed_selected_from_entropy() -> None:
    chosen, was_selected = select_seed(configured_seed=None, entropy=lambda: 123456)
    assert chosen == 123456
    assert was_selected is True


def test_selected_seed_within_range() -> None:
    # An entropy source returning a huge number is folded into range.
    chosen, _ = select_seed(configured_seed=None, entropy=lambda: 10**30)
    assert 0 <= chosen <= 4_294_967_295


def test_selected_seed_is_deterministic_given_entropy() -> None:
    a, _ = select_seed(configured_seed=None, entropy=lambda: 7)
    b, _ = select_seed(configured_seed=None, entropy=lambda: 7)
    assert a == b


def test_default_entropy_source_produces_in_range_seed() -> None:
    # With no entropy injected, the real source still yields an in-range seed.
    chosen, was_selected = select_seed(configured_seed=None)
    assert 0 <= chosen <= 4_294_967_295
    assert was_selected is True


@pytest.mark.parametrize("bad", [-1, 4_294_967_296])
def test_supplied_out_of_range_seed_is_passed_through_for_validation(bad: int) -> None:
    # select_seed does not validate; it returns the configured value so the
    # Config_Loader's validation pass (task 6.2) reports the range violation.
    chosen, was_selected = select_seed(configured_seed=bad, entropy=lambda: 5)
    assert chosen == bad
    assert was_selected is False
