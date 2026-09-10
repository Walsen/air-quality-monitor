"""Tests for stop-reason classification and the verification hook (tasks 10.1, 10.3).

`test_every_stop_reason_the_sdk_can_return_is_classified` is the test task 10.3 asks for, and
the important thing about it is that the expected set is DERIVED from the SDK's own `StopReason`
literal rather than written out here. A hand-written list of twelve reasons passes forever while
the SDK grows a thirteenth; a derived one fails the moment an upgrade adds one. Probing the
installed SDK found twelve, three of which (`cancelled`, `checkpoint`, `interrupt`) the spec
never anticipated — so this is not a hypothetical.

`test_the_verifier_is_fail_closed` covers the harder half of task 10.1. `AfterInvocationEvent`
carries `result` and `resume` but NO `cancel` field, so a Strands hook can observe a response
and cannot veto it. Bypass-proofing therefore cannot work by the hook blocking; it works by the
hook RECORDING a verdict and the assembly refusing to emit without a positive one. Absence of a
verdict is treated as unverified, which is what makes a deliberately added early return safe
rather than merely unlikely.
"""

from __future__ import annotations

import typing

import pytest
from strands.types.event_loop import StopReason

from aqm_advisor.agent.stop_reasons import (
    ALL_STOP_REASONS,
    TurnOutcome,
    classify_stop_reason,
)
from aqm_advisor.agent.verification import (
    VerificationLedger,
    VerificationVerdict,
)

# --- Req 31.7 / 6.5a: every stop reason is handled ----------------------


def test_the_reason_set_is_derived_from_the_sdk_not_written_out() -> None:
    # The whole value of the exhaustiveness test below rests on this. A literal list here would
    # pass
    # forever while the SDK grew a thirteenth reason.
    assert frozenset(typing.get_args(StopReason)) == ALL_STOP_REASONS


def test_every_stop_reason_the_sdk_can_return_is_classified() -> None:
    # Task 10.3's named check. An unclassified reason means a turn ends in a state no branch
    # handles,
    # which in this service means a response reaching a user without the verifiers deciding on
    # it.
    for reason in ALL_STOP_REASONS:
        assert isinstance(classify_stop_reason(reason), TurnOutcome), reason


def test_an_unknown_stop_reason_is_treated_as_a_model_failure() -> None:
    # Fail closed on the dimension the SDK might grow. A new reason must not be read as success.
    assert classify_stop_reason("some_future_reason") is TurnOutcome.MODEL_FAILED


def test_a_guardrail_stop_is_a_rejection_not_a_failure() -> None:
    # Req 6.5a. The distinction is load-bearing: a rejection means the guardrail did its job and
    # the turn
    # degrades to safe wording, whereas a failure would be retried — retrying a blocked
    # generation just
    # blocks again, and logs it as an outage.
    assert classify_stop_reason("content_filtered") is TurnOutcome.GUARDRAIL_REJECTED
    assert classify_stop_reason("guardrail_intervened") is TurnOutcome.GUARDRAIL_REJECTED


def test_the_three_limit_reasons_are_bounds_not_failures() -> None:
    # Req 22.2a: reaching a configured ceiling is an expected outcome with one warning, not an
    # error.
    for reason in ("limit_turns", "limit_output_tokens", "limit_total_tokens"):
        assert classify_stop_reason(reason) is TurnOutcome.BOUND_REACHED, reason


def test_the_providers_own_token_cap_is_also_a_bound() -> None:
    # `max_tokens` is the provider's per-call cap rather than one of our `limits`, but the turn
    # is
    # truncated either way and the user-visible consequence is identical.
    assert classify_stop_reason("max_tokens") is TurnOutcome.BOUND_REACHED


def test_a_completed_turn_is_completed() -> None:
    assert classify_stop_reason("end_turn") is TurnOutcome.COMPLETED
    assert classify_stop_reason("stop_sequence") is TurnOutcome.COMPLETED


def test_a_reason_that_means_the_loop_continues_is_not_completion() -> None:
    # Reading `tool_use` as completion would emit a half-finished turn — the model asked for a
    # tool and
    # has not produced its answer yet.
    for reason in ("tool_use", "interrupt", "checkpoint"):
        assert classify_stop_reason(reason) is TurnOutcome.NEEDS_CONTINUATION, reason


def test_cancellation_is_its_own_outcome() -> None:
    # Neither a failure to report nor a turn to degrade. Nobody is waiting for the answer.
    assert classify_stop_reason("cancelled") is TurnOutcome.CANCELLED


def test_no_outcome_is_unreachable() -> None:
    # A dead outcome is a branch nobody tested. Every member must be reachable — but
    # MODEL_FAILED is
    # deliberately NOT among those a known reason produces: the SDK has no stop reason meaning
    # "the model
    # failed", because a model failure arrives as an EXCEPTION (ModelThrottledException,
    # EventLoopException) rather than as a stop reason. So it is reachable only through the
    # unknown-reason fallback and the exception path, both covered above.
    produced = {classify_stop_reason(reason) for reason in ALL_STOP_REASONS}
    assert produced == set(TurnOutcome) - {TurnOutcome.MODEL_FAILED}


def test_model_failure_is_reachable_even_though_no_sdk_reason_names_it() -> None:
    # The other half of the assertion above: MODEL_FAILED is not dead code.
    assert classify_stop_reason("anything the sdk never returns") is TurnOutcome.MODEL_FAILED


# --- Req 31.5 / 34.7: the verifier cannot be bypassed -------------------


def test_the_verifier_is_fail_closed() -> None:
    # THE bypass test. A hook cannot veto (AfterInvocationEvent has no `cancel`), so safety
    # rests on the
    # ledger starting out unverified and assembly refusing to emit without a positive verdict.
    ledger = VerificationLedger()
    assert ledger.verdict() is None
    assert ledger.is_publishable() is False


def test_an_unverified_ledger_refuses_to_release_the_text() -> None:
    ledger = VerificationLedger()
    with pytest.raises(RuntimeError, match="verified"):
        ledger.release("the sub-index is 68")


def test_a_clean_generation_becomes_publishable() -> None:
    # Non-vacuity: a ledger that refused everything would make the service incapable of
    # answering.
    ledger = VerificationLedger()
    ledger.record(VerificationVerdict(passed=True, checks=("grounding", "forbidden")))
    assert ledger.is_publishable() is True
    assert ledger.release("the sub-index is 68") == "the sub-index is 68"


def test_a_failed_verdict_stays_unpublishable() -> None:
    ledger = VerificationLedger()
    ledger.record(
        VerificationVerdict(passed=False, checks=("grounding",), failures=("4242",))
    )
    assert ledger.is_publishable() is False
    with pytest.raises(RuntimeError, match="verified"):
        ledger.release("the sub-index is 4242")


def test_a_second_verdict_cannot_overwrite_a_failure() -> None:
    # Otherwise a retry loop could launder a rejected generation into a published one by
    # verifying a
    # DIFFERENT text and leaving the ledger positive.
    ledger = VerificationLedger()
    ledger.record(VerificationVerdict(passed=False, checks=("grounding",)))
    ledger.record(VerificationVerdict(passed=True, checks=("grounding",)))
    assert ledger.is_publishable() is False


def test_a_new_turn_starts_unverified() -> None:
    # A ledger reused across turns with a stale positive verdict would publish turn N+1 on turn
    # N's
    # verification — the most dangerous shape of all, because it looks verified.
    ledger = VerificationLedger()
    ledger.record(VerificationVerdict(passed=True, checks=("grounding",)))
    ledger.reset()
    assert ledger.is_publishable() is False
    assert ledger.verdict() is None


def test_the_ledger_names_which_checks_ran() -> None:
    # Req 20.4's audit needs the checks that ran, not merely that something passed. "Verified"
    # with no
    # named check is indistinguishable from a verifier that did nothing.
    ledger = VerificationLedger()
    ledger.record(
        VerificationVerdict(passed=True, checks=("grounding", "forbidden", "medication"))
    )
    recorded = ledger.verdict()
    assert recorded is not None
    assert "grounding" in recorded.checks


def test_a_verdict_with_no_checks_is_refused() -> None:
    # A pass that ran nothing is not a pass. This is the failure that would make every other
    # test here
    # vacuous, so it is rejected at construction.
    with pytest.raises(ValueError, match="check"):
        VerificationVerdict(passed=True, checks=())
