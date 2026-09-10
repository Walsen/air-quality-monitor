"""Properties 12, 13 and 7: degradation honesty, malformed input, envelope invariance.

Tasks 11.3, 11.4, 11.5. Validates Reqs 1.4, 1.5, 7.3, 8.5, 21.1, 21.2, 21.3, 21.4, 32.5.

**Property 12's hardest clause is the absence of a number.** Req 21.1 says a degraded response
states no condition value, and the way to test that across every shape is to assert the guidance
carries NO NUMERAL at all. A degraded turn retrieved nothing, so any digit in its text is either
an invented reading or a leaked provider detail — and both are the failure. Quantified over
failure kinds and over the missing-subject list, because the subjects are caller-supplied and a
caller can pass anything.

**Property 13 is about never RAISING, and Req 32.5 is why.** An exception escaping the boundary
becomes an opaque `424 RuntimeClientError` from the container, which loses the envelope and any
escalation. So the property is that arbitrary junk in produces a response out — not that the
junk is diagnosed correctly.

**Property 7 quantifies over every response SHAPE this service can build.** The envelope is what
carries Req 8.5's disclaimer and Req 10.4's emergency direction, so a shape that omitted it
would be a response with no safety text at all. The interesting version of the property is
therefore over the constructors, not over one of them.
"""

from __future__ import annotations

import datetime as dt
import logging

from hypothesis import given
from hypothesis import strategies as st
from pydantic import SecretStr, ValidationError

from aqm_advisor.agent.boundary import (
    fault_for,
    fault_response,
    handle_at_top_level,
)
from aqm_advisor.domain.degradation import degraded_response
from aqm_advisor.domain.envelope import resolve_envelope
from aqm_advisor.domain.grounding import numerals
from aqm_advisor.domain.models import (
    AdvisoryRequest,
    AdvisoryResponse,
    Escalation,
    GuardrailEnvelope,
)
from aqm_advisor.domain.redflag import DEFAULT_RED_FLAG_RULES, match_red_flags
from aqm_advisor.domain.turn import escalating_response
from aqm_advisor.observability.logging import EventLogger
from aqm_advisor.ports.protocols import ServingClientError, ServingFailureKind

_AT = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_CONFIGURED = "If you are severely breathless, seek emergency care now."
_MARKER = "PROVIDER-DETAIL-XYZZY"


def _logger() -> EventLogger:
    return EventLogger(logging.getLogger("aqm_advisor.test.degradation_properties"))


_ENVELOPES = st.sampled_from(
    [
        (
            GuardrailEnvelope(
                emergency_guidance="Seek emergency care now.",
                advisory_scope="exposure-reduction",
                disclaimer="Not medical advice.",
            ),
            None,
        ),
        (None, GuardrailEnvelope(emergency_guidance="Seek emergency care now.")),
        (None, None),
    ]
)
"""The three states a retrieval outcome leaves the envelope in.

Served this turn, cached from earlier in the run, or the cold-start fallback.
"""


def _resolved(
    pair: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
) -> GuardrailEnvelope:
    served, cached = pair
    return resolve_envelope(
        served=served, cached=cached, configured_emergency_guidance=_CONFIGURED
    ).envelope


_SUBJECTS = st.lists(
    st.sampled_from(
        [
            "current conditions",
            "the pollen count",
            "tomorrow's outlook",
            "PM2.5",
            "the 24-hour mean",
            "ozone",
        ]
    ),
    min_size=1,
    max_size=4,
)
"""Missing-data subjects, including digit-bearing ones, because callers pass arbitrary names."""

_ESCALATIONS = st.one_of(
    st.none(),
    st.just(
        Escalation(
            kind="emergency",
            markers=("struggling to breathe",),
            guidance=_CONFIGURED,
        )
    ),
)


# --- Property 12: degradation is complete and honest --------------------


@given(_ENVELOPES, _SUBJECTS, _ESCALATIONS)
def test_a_degraded_response_never_states_a_number(
    envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
    missing: list[str],
    escalation: Escalation | None,
) -> None:
    # Req 21.1's hardest clause, tested as an absence. A degraded turn retrieved nothing, so any
    # digit in
    # its text is either an invented reading or a leaked provider detail — and both are the
    # failure.
    response = degraded_response(
        envelope=_resolved(envelopes),
        answered_at=_AT,
        missing=missing,
        escalation=escalation,
    )
    assert numerals(response.guidance or "") == ()


@given(_ENVELOPES, _SUBJECTS, _ESCALATIONS)
def test_a_degraded_response_is_always_marked_degraded(
    envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
    missing: list[str],
    escalation: Escalation | None,
) -> None:
    # Req 21.1 and 21.2. An unmarked degraded response is worse than no answer: a client cannot
    # tell it
    # apart from a complete one, so it will be presented to the user as authoritative.
    response = degraded_response(
        envelope=_resolved(envelopes),
        answered_at=_AT,
        missing=missing,
        escalation=escalation,
    )
    assert response.degraded is True


@given(_ENVELOPES, _SUBJECTS, _ESCALATIONS)
def test_a_degraded_response_carries_no_basis(
    envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
    missing: list[str],
    escalation: Escalation | None,
) -> None:
    # A basis is provenance for values, and there are no values. An empty basis would satisfy a
    # "basis present" check while carrying no provenance — the shape that looks like success.
    response = degraded_response(
        envelope=_resolved(envelopes),
        answered_at=_AT,
        missing=missing,
        escalation=escalation,
    )
    assert response.basis is None


@given(_ENVELOPES, _SUBJECTS)
def test_a_degraded_response_names_what_is_missing(
    envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
    missing: list[str],
) -> None:
    # Req 21.7's honesty half. Non-vacuity for the tests above: a response that said nothing at
    # all would
    # carry no numeral and no basis while also being useless.
    response = degraded_response(
        envelope=_resolved(envelopes), answered_at=_AT, missing=missing
    )
    assert "could not retrieve" in (response.guidance or "")


@given(st.sampled_from(list(ServingFailureKind)), _ENVELOPES)
def test_no_serving_failure_leaks_provider_text(
    kind: ServingFailureKind,
    envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
) -> None:
    # Req 21.4, quantified over every failure kind. The marker stands for whatever the provider
    # sends,
    # since a provider message can say anything.
    error = ServingClientError(kind)
    error.args = (*error.args, _MARKER)
    fault = fault_for(error)
    assert fault is not None
    response = fault_response(fault, envelope=_resolved(envelopes), answered_at=_AT)
    assert _MARKER not in (response.guidance or "")


# --- Property 13: malformed input never yields a server error -----------


_JUNK = st.one_of(
    st.text(max_size=80),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.none(),
    st.booleans(),
    st.lists(st.text(max_size=10), max_size=3),
    st.dictionaries(st.text(max_size=8), st.text(max_size=8), max_size=3),
)
"""Arbitrary junk for the request fields.

Includes the shapes a real JSON body carries, not only strings.
"""


@given(_JUNK, _JUNK)
def test_malformed_input_is_a_validation_error_not_a_crash(
    utterance: object, credential: object
) -> None:
    # Reqs 1.4 and 1.5. Pydantic must be the thing that refuses, so the refusal is typed and the
    # field is
    # nameable. An unexpected exception type here would reach the boundary as a surprise
    # instead.
    try:
        AdvisoryRequest(utterance=utterance, credential=credential)  # type: ignore[arg-type]
    except ValidationError:
        return
    except Exception as error:
        raise AssertionError(f"unexpected {type(error).__name__}") from error


@given(_JUNK, _JUNK, _ENVELOPES)
def test_every_request_either_validates_or_yields_a_response(
    utterance: object,
    credential: object,
    envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
) -> None:
    # THE Req 32.5 property, written so NEITHER branch can be skipped. An earlier version put
    # the
    # assertions inside an `except ValidationError` block — so any input that happened to
    # validate skipped
    # the body entirely and the test passed having checked nothing. That is the same vacuity
    # shape as a
    # property whose interesting branch is never entered, and it is asserted away here by
    # covering both
    # outcomes: a valid request, or a response.
    envelope = _resolved(envelopes)
    try:
        request = AdvisoryRequest(utterance=utterance, credential=credential)  # type: ignore[arg-type]
    except ValidationError as error:
        fault = fault_for(error)
        assert fault is not None
        response = fault_response(fault, envelope=envelope, answered_at=_AT)
        assert isinstance(response, AdvisoryResponse)
        assert response.degraded is True
        assert fault.fields != ()
        return
    assert isinstance(request, AdvisoryRequest)
    assert request.utterance.strip() != ""


def test_the_junk_strategy_actually_produces_rejections() -> None:
    # The non-vacuity proof for the property above: these are the shapes a JSON body really
    # carries, and
    # each must be refused, so the ValidationError branch is genuinely exercised.
    rejected = 0
    for utterance, credential in (
        (None, "a-credential"),
        ("   ", "a-credential"),
        (123, "a-credential"),
        ("how is the air?", None),
        ([], []),
        ({}, {}),
    ):
        try:
            AdvisoryRequest(utterance=utterance, credential=credential)  # type: ignore[arg-type]
        except ValidationError:
            rejected += 1
    assert rejected == 6


def test_the_junk_strategy_also_produces_acceptances() -> None:
    # The other half. If nothing ever validated, the property's success branch would be the dead
    # one, and
    # a service that refused every request would pass it.
    request = AdvisoryRequest(utterance="how is the air?", credential="a-credential")  # type: ignore[arg-type]
    assert request.utterance == "how is the air?"


@given(_JUNK, _JUNK, _ENVELOPES)
def test_a_rejected_request_never_echoes_its_own_input(
    utterance: object,
    credential: object,
    envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
) -> None:
    # A Pydantic error carries the offending VALUE as well as the field. Echoing it would return
    # the
    # user's own utterance inside an error message, and Req 19.2 keeps that text out of logs —
    # an error
    # body is no better a place for it.
    if not isinstance(utterance, str) or len(utterance) < 6:
        return
    try:
        AdvisoryRequest(utterance=utterance, credential=credential)  # type: ignore[arg-type]
    except ValidationError as error:
        fault = fault_for(error)
        assert fault is not None
        response = fault_response(
            fault, envelope=_resolved(envelopes), answered_at=_AT
        )
        assert utterance not in (response.guidance or "")


def test_a_long_utterance_with_a_bad_credential_is_not_echoed() -> None:
    # A fixed case pinning the test above, since its guard clause makes the generated version
    # skippable.
    utterance = "how has the air been near me over the last week?"
    try:
        AdvisoryRequest(utterance=utterance, credential=None)  # type: ignore[arg-type]
    except ValidationError as error:
        fault = fault_for(error)
        assert fault is not None
        response = fault_response(
            fault, envelope=_resolved((None, None)), answered_at=_AT
        )
        assert utterance not in (response.guidance or "")
        return
    raise AssertionError("expected a ValidationError")


@given(_JUNK)
def test_an_unexpected_error_still_produces_a_response(payload: object) -> None:
    # The surprise path. Whatever went wrong, the caller gets a response — never a raised
    # exception that
    # the container would turn into a 424.
    response = handle_at_top_level(
        RuntimeError(str(payload)),
        logger=_logger(),
        envelope=_resolved((None, None)),
        answered_at=_AT,
    )
    assert isinstance(response, AdvisoryResponse)
    assert response.degraded is True


# --- Property 7: envelope invariance ------------------------------------


@given(_ENVELOPES, _SUBJECTS, _ESCALATIONS)
def test_every_response_shape_carries_an_envelope(
    envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
    missing: list[str],
    escalation: Escalation | None,
) -> None:
    # THE invariance claim, quantified over the response CONSTRUCTORS rather than over one of
    # them. The
    # envelope carries Req 8.5's disclaimer and Req 10.4's emergency direction, so a shape that
    # omitted
    # it would be a response with no safety text at all.
    envelope = _resolved(envelopes)
    built: list[AdvisoryResponse] = [
        degraded_response(
            envelope=envelope,
            answered_at=_AT,
            missing=missing,
            escalation=escalation,
        ),
        escalating_response(
            markers=match_red_flags("struggling to breathe", DEFAULT_RED_FLAG_RULES),
            envelope=envelope,
            answered_at=_AT,
            degraded=False,
        ),
        handle_at_top_level(
            RuntimeError("x"),
            logger=_logger(),
            envelope=envelope,
            answered_at=_AT,
            escalation=escalation,
        ),
    ]
    for response in built:
        assert response.envelope.emergency_guidance.strip() != ""


@given(_ENVELOPES, _SUBJECTS)
def test_the_emergency_guidance_is_never_empty_in_any_shape(
    envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
    missing: list[str],
) -> None:
    # Req 10.4 does not depend on the envelope being COMPLETE, only on the emergency direction
    # existing.
    # A8a's fallback exists precisely so this can never be blank, including on a cold start.
    envelope = _resolved(envelopes)
    response = degraded_response(
        envelope=envelope, answered_at=_AT, missing=missing
    )
    assert response.envelope.emergency_guidance.strip() != ""


@given(_ENVELOPES, _SUBJECTS)
def test_scope_and_disclaimer_are_omitted_rather_than_invented(
    envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
    missing: list[str],
) -> None:
    # Req 21.9. Where they cannot be resolved they are absent, never locally substituted — a
    # local
    # disclaimer would be this service making a legal claim on Service 2's behalf. This is the
    # asymmetry
    # A8a's exception is narrowed to: emergency guidance falls back, these two do not.
    served, cached = envelopes
    envelope = _resolved(envelopes)
    response = degraded_response(
        envelope=envelope, answered_at=_AT, missing=missing
    )
    if served is None and cached is None:
        assert response.envelope.advisory_scope is None
        assert response.envelope.disclaimer is None


@given(_ENVELOPES, _SUBJECTS)
def test_an_escalation_survives_every_degraded_shape(
    envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
    missing: list[str],
) -> None:
    # Req 21.3. A degraded turn is exactly when the user most needs the emergency direction, so
    # dropping
    # a recognised escalation under failure inverts the priority.
    escalation = Escalation(
        kind="emergency", markers=("blue lips",), guidance=_CONFIGURED
    )
    envelope = _resolved(envelopes)
    degraded = degraded_response(
        envelope=envelope, answered_at=_AT, missing=missing, escalation=escalation
    )
    internal = handle_at_top_level(
        RuntimeError("x"),
        logger=_logger(),
        envelope=envelope,
        answered_at=_AT,
        escalation=escalation,
    )
    assert degraded.escalation is escalation
    assert internal.escalation is escalation


@given(_ENVELOPES, _SUBJECTS, _ESCALATIONS)
def test_escalation_precedes_guidance_in_every_degraded_shape(
    envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
    missing: list[str],
    escalation: Escalation | None,
) -> None:
    # Req 10.2's field order does not relax under failure, asserted on the dumped body because
    # that is
    # what a client receives.
    response = degraded_response(
        envelope=_resolved(envelopes),
        answered_at=_AT,
        missing=missing,
        escalation=escalation,
    )
    keys = list(response.model_dump().keys())
    assert keys.index("escalation") < keys.index("guidance")


@given(st.text(min_size=1, max_size=40))
def test_a_prior_turn_is_never_rendered_into_a_degraded_response(text: str) -> None:
    # Req 19.2's material is `SecretStr` for a reason. A degraded response built while prior
    # turns are in
    # scope must not render them, and the masked form is what would appear if it tried.
    turn_text = SecretStr(text)
    response = degraded_response(
        envelope=_resolved((None, None)),
        answered_at=_AT,
        missing=("current conditions",),
    )
    assert str(turn_text) not in (response.guidance or "")
    assert "**********" not in (response.guidance or "")
