"""Tests for the advisory turn contract (task 3.1).

Two of these are structural rather than behavioural, and deliberately so.

Req 1.1 and 1.2 say the request and response carry **exactly** these fields. An assertion over
the field
SET is what makes that enforceable: it fails when a field is added as well as when one is
removed, so a
future turn cannot quietly widen the contract. Service 2 used the same technique to make a
medication
entry structurally unable to hold a dose.

The credential tests check every rendering path rather than one, because Req 5.2 forbids the
credential
reaching *any* log entry, record, response or error message — and a model that hides it in
`repr` while
`model_dump()` returns it plainly satisfies the wording and fails the intent.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import SecretStr, ValidationError

from aqm_advisor.domain.models import (
    DEFAULT_MAX_UTTERANCE_LENGTH,
    AdvisoryRequest,
    AdvisoryResponse,
    BlankUtteranceError,
    GuardrailEnvelope,
    PriorTurn,
    UtteranceTooLongError,
    validate_utterance_length,
)

_SECRET = "eyJhbGciOi.THIS-IS-THE-CREDENTIAL.signature"
_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _request(**kwargs: object) -> AdvisoryRequest:
    defaults: dict[str, object] = {
        "utterance": "Is it safe to run this evening?",
        "credential": SecretStr(_SECRET),
    }
    return AdvisoryRequest(**{**defaults, **kwargs})  # type: ignore[arg-type]


def _envelope() -> GuardrailEnvelope:
    return GuardrailEnvelope(
        advisory_scope="exposure-reduction",
        emergency_guidance="Seek emergency care now.",
        disclaimer="This is not medical advice.",
    )


# --- Req 1.1 / 1.2: exactly these fields --------------------------------

def test_the_request_carries_exactly_the_contracted_fields() -> None:
    assert set(AdvisoryRequest.model_fields) == {
        "utterance",
        "credential",
        "prior_turns",
        "locale",
    }


def test_the_response_carries_exactly_the_contracted_fields() -> None:
    assert set(AdvisoryResponse.model_fields) == {
        "guidance",
        "basis",
        "envelope",
        "escalation",
        "degraded",
        "answered_at",
    }


def test_the_response_has_nowhere_to_put_a_credential() -> None:
    # Req 1.6 made structural: there is no field to leak into, so no code path can add one by
    # accident.
    assert not any(
        "credential" in name or "token" in name for name in AdvisoryResponse.model_fields
    )


# --- Req 1.4: a blank utterance is refused ------------------------------

@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  \n", " \u00a0 "])
def test_a_blank_utterance_is_refused(blank: str) -> None:
    with pytest.raises((ValidationError, BlankUtteranceError)) as caught:
        _request(utterance=blank)
    assert "utterance" in str(caught.value)


def test_a_whitespace_only_utterance_is_refused_not_merely_an_empty_one() -> None:
    # `min_length=1` alone would accept " ", which Req 1.4 names explicitly. This is the case a
    # length-only constraint silently lets through.
    with pytest.raises((ValidationError, BlankUtteranceError)):
        _request(utterance="     ")


def test_surrounding_whitespace_is_trimmed_so_one_value_is_stored() -> None:
    assert _request(utterance="  hello  ").utterance == "hello"


# --- Req 1.5: the configured maximum ------------------------------------

def test_the_default_maximum_is_four_thousand() -> None:
    assert DEFAULT_MAX_UTTERANCE_LENGTH == 4000


def test_an_over_long_utterance_is_refused_naming_the_field_and_the_limit() -> None:
    with pytest.raises(UtteranceTooLongError) as caught:
        validate_utterance_length("x" * 51, max_length=50)
    message = str(caught.value)
    assert "utterance" in message
    assert "50" in message


def test_the_rejection_message_does_not_echo_the_utterance() -> None:
    # Req 19.2 forbids an utterance substring reaching a log, and a rejection message is logged.
    # An
    # error that quoted the offending text would carry the user's words into the log by the back
    # door.
    secret_words = "my chest has been tight since tuesday"
    padded = secret_words + "x" * 5000
    with pytest.raises(UtteranceTooLongError) as caught:
        validate_utterance_length(padded, max_length=DEFAULT_MAX_UTTERANCE_LENGTH)
    assert "chest" not in str(caught.value)
    assert "tight" not in str(caught.value)


def test_a_length_equal_to_the_limit_is_accepted() -> None:
    # The boundary in both directions, so an off-by-one cannot hide.
    validate_utterance_length("x" * 50, max_length=50)
    with pytest.raises(UtteranceTooLongError):
        validate_utterance_length("x" * 51, max_length=50)


def test_the_model_itself_declares_no_competing_maximum() -> None:
    # ONE authority on length. A `max_length` pinned on the field would be a second limit that a
    # configured value could disagree with — exactly what Req 15.6 forbids for measurement
    # weakness,
    # and what made Service 2's "configured" profile limits fiction until they were actually
    # wired.
    constraints = repr(AdvisoryRequest.model_fields["utterance"])
    assert "max_length" not in constraints


def test_a_long_utterance_is_accepted_by_the_model_and_refused_by_the_check() -> None:
    # Proves the previous test is not vacuous: the model really does admit a value the
    # configured
    # check rejects, so the check is doing the work rather than duplicating a field constraint.
    long_utterance = "x" * (DEFAULT_MAX_UTTERANCE_LENGTH + 1)
    assert _request(utterance=long_utterance).utterance == long_utterance
    with pytest.raises(UtteranceTooLongError):
        validate_utterance_length(long_utterance, max_length=DEFAULT_MAX_UTTERANCE_LENGTH)


# --- Req 5.2 / 1.6: the credential renders nowhere ----------------------

def test_the_credential_is_absent_from_every_rendering_of_the_request() -> None:
    request = _request()
    for rendering in (
        repr(request),
        str(request),
        f"{request}",
        f"{request!r}",
        f"{request!s}",
        str(request.model_dump()),
        request.model_dump_json(),
        str(list(request.model_dump().items())),
    ):
        assert _SECRET not in rendering, "the credential reached a rendering"
        assert "THIS-IS-THE-CREDENTIAL" not in rendering


def test_the_credential_is_excluded_from_serialisation_entirely() -> None:
    # Not merely masked: absent. A masked placeholder in a dump still tells a reader a
    # credential was
    # there and invites a caller to look for the real one.
    assert "credential" not in _request().model_dump()
    assert "credential" not in _request().model_dump_json()


def test_the_credential_is_still_reachable_at_its_one_intended_call_site() -> None:
    # The exclusion must not make the value unusable — Req 5.1 forwards it to the
    # Serving_Client.
    assert _request().credential.get_secret_value() == _SECRET


def test_a_prior_turn_does_not_render_its_prose() -> None:
    # prior_turns carries the USER'S OWN WORDS and the guidance given. Req 19.2 forbids an
    # utterance
    # substring reaching a log, so this model needs the same discipline as the credential.
    turn = PriorTurn(
        utterance=SecretStr("my chest was tight"),
        guidance=SecretStr("Consider staying indoors."),
    )
    for rendering in (repr(turn), str(turn), f"{turn}"):
        assert "chest" not in rendering
        assert "tight" not in rendering
        assert "indoors" not in rendering


def test_a_request_carrying_prior_turns_still_renders_no_prose() -> None:
    request = _request(
        prior_turns=(
            PriorTurn(
                utterance=SecretStr("my chest was tight"),
                guidance=SecretStr("Stay indoors."),
            ),
        )
    )
    for rendering in (repr(request), str(request), request.model_dump_json()):
        assert "chest" not in rendering
        assert "indoors" not in rendering


def test_the_prose_is_reachable_for_the_prompt_that_needs_it() -> None:
    # Non-vacuity: hiding it from rendering must not hide it from the model call that uses it.
    turn = PriorTurn(
        utterance=SecretStr("my chest was tight"), guidance=SecretStr("Stay indoors.")
    )
    assert turn.utterance.get_secret_value() == "my chest was tight"
    assert turn.guidance.get_secret_value() == "Stay indoors."


# --- ordering and defaults ----------------------------------------------

def test_prior_turns_preserve_their_order() -> None:
    # Req 1.1 says an ORDERED sequence; a set or an unordered container would lose the
    # conversation.
    turns = tuple(
        PriorTurn(
            utterance=SecretStr(f"question {index}"), guidance=SecretStr(f"answer {index}")
        )
        for index in range(4)
    )
    request = _request(prior_turns=turns)
    assert [t.utterance.get_secret_value() for t in request.prior_turns] == [
        "question 0",
        "question 1",
        "question 2",
        "question 3",
    ]


def test_prior_turns_and_locale_are_optional() -> None:
    request = _request()
    assert request.prior_turns == ()
    assert request.locale is None


def test_a_response_is_not_degraded_by_default() -> None:
    # The safe default is the ordinary answer; a response is degraded only when something made
    # it so.
    response = AdvisoryResponse(
        guidance="Air quality is moderate.",
        basis=None,
        envelope=_envelope(),
        escalation=None,
        answered_at=_NOW,
    )
    assert response.degraded is False


def test_a_response_instant_must_be_aware() -> None:
    # Req 1.3 takes the instant from the injected Clock, which yields aware instants. A naive
    # one here
    # would mean something read a wall clock instead.
    with pytest.raises(ValidationError):
        AdvisoryResponse(
            guidance="ok",
            basis=None,
            envelope=_envelope(),
            escalation=None,
            answered_at=dt.datetime(2026, 7, 1, 12),
        )


def test_the_models_reject_an_unknown_field() -> None:
    # "Exactly these fields" has to hold at construction too, or a typo becomes a silently
    # ignored
    # value and a caller believes it set something.
    with pytest.raises(ValidationError):
        _request(unexpected="value")
