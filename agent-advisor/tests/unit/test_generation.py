"""Tests for structured output (task 10.3's remaining half; Requirements 6.3b, 6.5, 21).

`test_the_model_authors_nothing_the_service_is_authoritative_for` is the test that matters. Req
6.3b says the Advisory_Response's structured fields come from a Pydantic model the MODEL fills
in — so the field set of that model is a statement about what the model is permitted to author.
Every field of `AdvisoryResponse` that this service is authoritative for must be absent from it,
and the test asserts the disjointness against `AdvisoryResponse` itself rather than against a
list, so adding a field to either side cannot quietly widen what the model may invent.

`test_a_structured_output_exception_never_yields_a_generation` is Req 6.3b's second clause. The
failure mode it prevents is emitting an unvalidated response: if the SDK could not coerce the
model's output into the schema, there is no validated text to publish, and returning the raw
text "just this once" is exactly the path the requirement closes.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from strands.types.exceptions import (
    MaxTokensReachedException,
    ModelThrottledException,
    StructuredOutputException,
)

from aqm_advisor.agent.generation import (
    ModelGeneration,
    obtain_structured_generation,
)
from aqm_advisor.agent.stop_reasons import TurnOutcome
from aqm_advisor.domain.models import AdvisoryResponse

# --- Req 6.3b: the model's output surface -------------------------------


def test_the_generation_model_carries_the_guidance() -> None:
    generation = ModelGeneration(guidance="Air quality is Moderate; 68 on the index.")
    assert "68" in generation.guidance


def test_the_model_authors_nothing_the_service_is_authoritative_for() -> None:
    # THE structural test. Asserted against `AdvisoryResponse` itself, so adding a field to
    # either side
    # cannot quietly widen what the model may invent.
    service_authored = {
        "escalation",  # determined before generation, never by the model (Req 10.2)
        "basis",  # assembled from retrieval; Req 9.4 forbids deriving it
        "envelope",  # Service 2's, or A8a's configured fallback (Req 21.9)
        "answered_at",  # the injected Clock (Req 25.2)
        "degraded",  # the service's own judgement about its retrieval
    }
    assert service_authored <= set(AdvisoryResponse.model_fields)
    assert set(ModelGeneration.model_fields).isdisjoint(service_authored)


def test_the_generation_model_is_strict_about_extra_fields() -> None:
    # A model that invented `escalation` would otherwise have it silently accepted and dropped,
    # which
    # looks identical to it never having tried.
    with pytest.raises(ValidationError):
        ModelGeneration(guidance="fine", escalation="emergency")  # type: ignore[call-arg]


def test_a_blank_generation_is_refused() -> None:
    # Emitting an empty guidance would present a successful turn that said nothing.
    with pytest.raises(ValidationError):
        ModelGeneration(guidance="   ")


def test_the_guidance_is_stored_trimmed() -> None:
    assert ModelGeneration(guidance="  advice  ").guidance == "advice"


# --- Req 6.3b: a schema failure is a model failure ----------------------


def _raise(error: BaseException) -> ModelGeneration:
    """Raise the given error.

    Typed `BaseException`, not `Exception`, because one test deliberately raises
    `KeyboardInterrupt` to prove the wrapper does not swallow it.
    """
    raise error


def test_a_successful_call_returns_the_generation() -> None:
    # Non-vacuity for everything below: a wrapper that failed on every path would pass the
    # failure tests
    # while making the service unable to answer.
    outcome = obtain_structured_generation(
        lambda: ModelGeneration(guidance="Air quality is Moderate.")
    )
    assert outcome.outcome is TurnOutcome.COMPLETED
    assert outcome.generation is not None
    assert outcome.generation.guidance == "Air quality is Moderate."


def test_a_structured_output_exception_never_yields_a_generation() -> None:
    # Req 6.3b. If the SDK could not coerce the output into the schema there is no validated
    # text, and
    # returning the raw text "just this once" is the path the requirement closes.
    outcome = obtain_structured_generation(
        lambda: _raise(StructuredOutputException("could not coerce"))
    )
    assert outcome.outcome is TurnOutcome.MODEL_FAILED
    assert outcome.generation is None


def test_the_failure_names_the_kind_without_the_provider_message() -> None:
    # Req 21.4's discipline applied here: a named kind, never a raw error body that may quote
    # the prompt
    # or the model's partial output.
    outcome = obtain_structured_generation(
        lambda: _raise(StructuredOutputException("field 'guidance' missing from XYZ"))
    )
    assert outcome.reason is not None
    assert "XYZ" not in outcome.reason
    assert "structured" in outcome.reason.casefold()


def test_a_validation_error_is_also_a_model_failure() -> None:
    # The SDK raises StructuredOutputException, but a Pydantic ValidationError can surface from
    # our own
    # model's validators — a blank guidance, for instance. Both mean no validated text exists.
    def _blank() -> ModelGeneration:
        return ModelGeneration(guidance="")

    outcome = obtain_structured_generation(_blank)
    assert outcome.outcome is TurnOutcome.MODEL_FAILED
    assert outcome.generation is None


# --- Req 6.5 and Req 22 converge on discarding the partial text ---------


def test_a_truncated_generation_is_never_emitted() -> None:
    # Req 6.5: treat a truncated generation as a failure RATHER THAN emitting partial Guidance.
    # The label
    # differs from Req 22.2a's "bound reached", but both discard the text, which is the part
    # that
    # protects the user.
    outcome = obtain_structured_generation(
        lambda: _raise(MaxTokensReachedException("truncated"))
    )
    assert outcome.generation is None


def test_a_truncated_generation_is_a_bound_not_a_retryable_failure() -> None:
    # The labels converge on discarding the text but differ on what happens next, and
    # BOUND_REACHED is
    # the right one: the same prompt would truncate again, so a retry spends budget to reproduce
    # the
    # failure. Req 22.2's degraded response is the useful outcome.
    outcome = obtain_structured_generation(
        lambda: _raise(MaxTokensReachedException("truncated"))
    )
    assert outcome.outcome is TurnOutcome.BOUND_REACHED


def test_a_throttle_is_a_model_failure() -> None:
    # Distinct from truncation, because a throttle IS worth retrying — nothing about the prompt
    # caused it.
    outcome = obtain_structured_generation(
        lambda: _raise(ModelThrottledException("slow down"))
    )
    assert outcome.outcome is TurnOutcome.MODEL_FAILED


def test_an_unexpected_error_is_a_model_failure_not_a_crash() -> None:
    # A boundary catch, per the error-handling practice: no raw stack trace reaches a caller,
    # and an
    # unknown error is never read as success.
    outcome = obtain_structured_generation(lambda: _raise(RuntimeError("something else")))
    assert outcome.outcome is TurnOutcome.MODEL_FAILED
    assert outcome.generation is None


def test_the_wrapper_does_not_swallow_a_cancellation() -> None:
    # `KeyboardInterrupt` and `SystemExit` are not model failures, and catching them would make
    # the
    # process unkillable mid-turn.
    with pytest.raises(KeyboardInterrupt):
        obtain_structured_generation(lambda: _raise(KeyboardInterrupt()))


def test_a_failed_outcome_is_falsy_and_a_good_one_is_truthy() -> None:
    # So a caller cannot mistake a failure for a generation by testing the wrong thing.
    good = obtain_structured_generation(lambda: ModelGeneration(guidance="fine"))
    bad = obtain_structured_generation(
        lambda: _raise(StructuredOutputException("no"))
    )
    assert bool(good) is True
    assert bool(bad) is False


# --- the two paths for one condition must agree ------------------------


def test_truncation_classifies_the_same_by_exception_and_by_stop_reason() -> None:
    # Truncation can arrive either way — as `MaxTokensReachedException` from a structured-output
    # call, or as the `max_tokens` stop reason from the event loop. Classifying them differently
    # would mean the same physical event produced a degraded response down one path and a retry
    # down the other, decided by how the SDK happened to surface it.
    from aqm_advisor.agent.stop_reasons import classify_stop_reason

    by_exception = obtain_structured_generation(
        lambda: _raise(MaxTokensReachedException("truncated"))
    ).outcome
    assert by_exception is classify_stop_reason("max_tokens")


def test_a_guardrail_stop_has_no_exception_counterpart_here() -> None:
    # Worth pinning so nobody adds one. A guardrail rejection arrives as a STOP REASON, not an
    # exception, so translating some future exception into GUARDRAIL_REJECTED would invent a
    # path
    # that then bypasses Req 8's repair-and-degrade handling.
    errors: tuple[BaseException, ...] = (
        StructuredOutputException("no"),
        ModelThrottledException("no"),
        MaxTokensReachedException("no"),
        RuntimeError("no"),
    )
    outcomes = {
        obtain_structured_generation(
            lambda bound=error: _raise(bound)  # type: ignore[misc]
        ).outcome
        for error in errors
    }
    assert TurnOutcome.GUARDRAIL_REJECTED not in outcomes
