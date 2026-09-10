"""Tests for the invocation bounds (task 10.4; Requirements 22.1 to 22.4).

Req 22.1a is unusual in naming its own mechanism: the ceilings must be expressed through
Strands' invocation `limits` rather than a counter of this service's own, because the framework
enforces them inside the agent loop where a hand-rolled counter cannot see a tool round trip.
`test_the_model_bound_is_expressed_as_strands_limits` is that requirement, and
`test_the_module_keeps_no_model_invocation_counter` is the half that stops the mechanism being
quietly reintroduced alongside.

Serving calls are the deliberate exception. Strands cannot see them — they happen inside a tool
body — so Req 22.3's bound IS a counter of this service's own, and a test says so rather than
leaving the inconsistency to look like an oversight.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from pydantic import SecretStr

from aqm_advisor.agent.bounds import (
    DEFAULT_MODEL_INVOCATIONS,
    HARD_LIMIT_KEYS,
    SOFT_LIMIT_KEYS,
    InvocationBounds,
    ServingCallBudget,
    limit_warning,
    recent_prior_turns,
)
from aqm_advisor.domain.models import PriorTurn

# --- Req 22.1 / 22.1a: expressed as Strands limits ----------------------


def test_the_default_model_bound_is_two() -> None:
    # Req 22.1's default, and the reason is worth keeping visible: one generation plus one
    # repair attempt
    # after a guardrail rejection. A default of 1 would make the repair path dead code.
    assert DEFAULT_MODEL_INVOCATIONS == 2
    assert InvocationBounds().model_invocations == 2


def test_the_model_bound_is_expressed_as_strands_limits() -> None:
    # Req 22.1a names the mechanism, not merely the outcome.
    limits = InvocationBounds(model_invocations=3).as_limits()
    assert limits["turns"] == 3


def test_all_three_ceilings_reach_the_limits_mapping() -> None:
    limits = InvocationBounds(
        model_invocations=2, output_tokens=4000, total_tokens=32000
    ).as_limits()
    assert limits == {"turns": 2, "output_tokens": 4000, "total_tokens": 32000}


def test_an_unset_token_ceiling_is_omitted_rather_than_zero() -> None:
    # `Limits` is total=False and the SDK validates each present key as a POSITIVE int, so
    # passing 0 to
    # mean "no limit" would raise a TypeError instead of lifting the cap.
    limits = InvocationBounds(model_invocations=2).as_limits()
    assert "output_tokens" not in limits
    assert "total_tokens" not in limits


def test_the_mapping_keys_are_the_ones_the_sdk_accepts() -> None:
    # A misspelled key would be silently ignored by a total=False TypedDict at runtime, so the
    # ceiling
    # would simply not apply — a bound that looks configured and is not.
    from strands.types.agent import Limits

    limits = InvocationBounds(
        model_invocations=2, output_tokens=1, total_tokens=1
    ).as_limits()
    assert set(limits).issubset(set(Limits.__annotations__))


def test_a_non_positive_bound_is_refused_at_construction() -> None:
    # Fail fast, per the error-handling practice. The SDK raises TypeError at invocation time;
    # catching it
    # here names the offending value while the config is still in view.
    with pytest.raises(ValueError, match="positive"):
        InvocationBounds(model_invocations=0)
    with pytest.raises(ValueError, match="positive"):
        InvocationBounds(output_tokens=-1)


def test_the_module_keeps_no_model_invocation_counter() -> None:
    # Req 22.1a's prohibition, structurally. A counter reintroduced alongside the limits would
    # drift from
    # them and would miss the tool round trips that are the whole reason the framework does
    # this.
    source = pathlib.Path("src/aqm_advisor/agent/bounds.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    increments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add)
    ]
    targets = {
        node.target.attr
        for node in increments
        if isinstance(node.target, ast.Attribute)
    }
    assert "model_invocations" not in targets
    assert not any("model" in name for name in targets), targets


# --- the soft/hard distinction the SDK actually gives -------------------


def test_only_the_turn_ceiling_is_documented_as_hard() -> None:
    # The SDK documents `output_tokens` and `total_tokens` as SOFT: "a single oversized response
    # can
    # overshoot the budget; checked at turn boundaries, not within an individual model call."
    # Recording
    # which is which stops Req 22 being read as a hard guarantee on all three.
    assert frozenset({"turns"}) == HARD_LIMIT_KEYS
    assert frozenset({"output_tokens", "total_tokens"}) == SOFT_LIMIT_KEYS


def test_every_limit_key_is_classified_as_hard_or_soft() -> None:
    from strands.types.agent import Limits

    assert set(Limits.__annotations__) == HARD_LIMIT_KEYS | SOFT_LIMIT_KEYS


def test_the_two_classes_do_not_overlap() -> None:
    assert HARD_LIMIT_KEYS.isdisjoint(SOFT_LIMIT_KEYS)


# --- Req 22.2a: one warning, naming which limit -------------------------


def test_the_warning_names_which_limit_was_reached() -> None:
    # Req 22.2a. "A limit was reached" tells an operator nothing about what to raise.
    message = limit_warning("limit_total_tokens")
    assert "total_tokens" in message


def test_each_limit_reason_produces_a_distinct_warning() -> None:
    messages = {
        limit_warning(reason)
        for reason in ("limit_turns", "limit_output_tokens", "limit_total_tokens")
    }
    assert len(messages) == 3


def test_the_providers_own_cap_is_named_as_such() -> None:
    # `max_tokens` is the provider's per-call cap rather than one of ours, so an operator
    # raising OUR
    # ceiling would not change anything. Saying so prevents a wasted config change.
    assert "provider" in limit_warning("max_tokens").casefold()


def test_a_non_limit_reason_is_refused() -> None:
    # Calling this for `end_turn` would log a bound warning on a healthy turn, which is how a
    # warning
    # channel becomes noise nobody reads.
    with pytest.raises(ValueError, match="limit"):
        limit_warning("end_turn")


# --- Req 22.3: serving calls, which Strands cannot see ------------------


def test_the_serving_budget_permits_up_to_its_bound() -> None:
    budget = ServingCallBudget(maximum=2)
    assert budget.consume("air_quality") is True
    assert budget.consume("history") is True


def test_the_serving_budget_refuses_beyond_its_bound() -> None:
    # Req 22.3. This one IS a counter of ours, because the calls happen inside a tool body where
    # the
    # framework's own limits cannot observe them.
    budget = ServingCallBudget(maximum=1)
    assert budget.consume("air_quality") is True
    assert budget.consume("history") is False


def test_the_serving_budget_reports_what_it_refused() -> None:
    # Diagnosability: which call was dropped decides whether the turn can still answer.
    budget = ServingCallBudget(maximum=1)
    budget.consume("air_quality")
    budget.consume("history")
    assert budget.refused == ("history",)


def test_a_refusal_does_not_consume_further_budget() -> None:
    budget = ServingCallBudget(maximum=1)
    budget.consume("air_quality")
    budget.consume("history")
    budget.consume("profile_get")
    assert budget.used == 1


def test_a_zero_serving_bound_is_refused() -> None:
    # A bound of zero would make every turn degraded, which is a misconfiguration rather than a
    # policy.
    with pytest.raises(ValueError, match="positive"):
        ServingCallBudget(maximum=0)


# --- Req 22.4: the most recent prior turns ------------------------------


def _turns(count: int) -> tuple[PriorTurn, ...]:
    """Build prior turns.

    A `PriorTurn` is a PAIR — the user's utterance and the guidance given back — not a single
    message with a role. So Req 22.4's ceiling bounds exchanges rather than messages, which is
    the more useful unit: truncating between an utterance and its answer would hand the model
    half an exchange.

    Both fields are `SecretStr`, so the assertions below read them explicitly rather than
    comparing rendered text, which would compare the mask.
    """
    return tuple(
        PriorTurn(
            utterance=SecretStr(f"turn {index}"),
            guidance=SecretStr(f"answer {index}"),
        )
        for index in range(count)
    )


def _utterances(turns: tuple[PriorTurn, ...]) -> list[str]:
    return [turn.utterance.get_secret_value() for turn in turns]


def test_a_shorter_sequence_is_returned_whole() -> None:
    supplied = _turns(2)
    assert recent_prior_turns(supplied, maximum=5) == supplied


def test_a_longer_sequence_keeps_the_most_recent() -> None:
    # Req 22.4 says the MOST RECENT, and the direction matters: keeping the oldest would drop
    # the context
    # the current utterance actually follows on from.
    kept = recent_prior_turns(_turns(5), maximum=2)
    assert _utterances(kept) == ["turn 3", "turn 4"]


def test_the_order_is_preserved() -> None:
    # Reversing the kept window would present the conversation backwards to the model.
    kept = recent_prior_turns(_turns(4), maximum=3)
    assert _utterances(kept) == ["turn 1", "turn 2", "turn 3"]


def test_an_exact_length_sequence_is_unchanged() -> None:
    supplied = _turns(3)
    assert recent_prior_turns(supplied, maximum=3) == supplied


def test_an_empty_sequence_stays_empty() -> None:
    assert recent_prior_turns((), maximum=3) == ()


def test_a_non_positive_maximum_is_refused() -> None:
    # A maximum of 0 would silently discard all history, which reads to a user as the agent
    # forgetting
    # what they just said.
    with pytest.raises(ValueError, match="positive"):
        recent_prior_turns(_turns(2), maximum=0)
