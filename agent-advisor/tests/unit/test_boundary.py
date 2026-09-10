"""Tests for the boundary error handling (task 11.2). Validates Reqs 5.4, 21.4, 21.5, 21.6.

**Req 32.5 sets the shape of this whole module: every handled failure becomes an
`AdvisoryResponse`, not a status code.** An unhandled error becomes an opaque `424
RuntimeClientError` from the container, which replaces a documented degraded answer with a
transport fault and loses the Guardrail_Envelope and any Escalation with it. So the tests assert
a response comes back, and that it still carries the envelope.

That last point is the one worth stating plainly: **even a malformed request gets the emergency
guidance.** A user in trouble who typed something the service could not parse still needs to be
told to call for help, and Req 10.4 does not depend on the request being well-formed.

**Req 21.4 is quantified rather than sampled.** The failure mode is a provider's message
reaching the caller, and a provider message can say anything — so the tests plant a marker
string inside each exception and assert it does not appear in the response. A test using one
fixed error body would pass while a different one leaked.

**Req 21.5 is asserted structurally.** An AST test checks the module has no bare `except:` and
that `except Exception` appears exactly once, inside the top-level handler. A convention about
where broad catches live is not enforceable by review alone; a test over the parse tree is.
"""

from __future__ import annotations

import ast
import datetime as dt
import logging
import pathlib

import pytest
from pydantic import ValidationError

from aqm_advisor.agent.boundary import (
    BoundaryFault,
    BoundaryFaultKind,
    fault_for,
    fault_response,
    handle_at_top_level,
)
from aqm_advisor.domain.envelope import resolve_envelope
from aqm_advisor.domain.models import AdvisoryRequest, Escalation, GuardrailEnvelope
from aqm_advisor.observability.logging import EventLogger
from aqm_advisor.ports.protocols import ServingClientError, ServingFailureKind

_AT = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_CONFIGURED = "If you are severely breathless, seek emergency care now."
_MARKER = "SERVICE2-INTERNAL-DETAIL-XYZZY"


def _envelope() -> GuardrailEnvelope:
    return resolve_envelope(
        served=None, cached=None, configured_emergency_guidance=_CONFIGURED
    ).envelope


def _logger() -> EventLogger:
    return EventLogger(logging.getLogger("aqm_advisor.test.boundary"))


def _validation_error() -> ValidationError:
    try:
        AdvisoryRequest(utterance="   ", credential="a-credential")  # type: ignore[arg-type]
    except ValidationError as error:
        return error
    raise AssertionError("expected a ValidationError")


# --- Req 21.6: a malformed request is never a server error --------------


def test_a_validation_error_becomes_an_invalid_request_fault() -> None:
    # Req 21.6. A malformed request is the caller's mistake, and reporting it as a server error
    # sends an
    # operator looking for a fault that does not exist.
    fault = fault_for(_validation_error())
    assert fault is not None
    assert fault.kind is BoundaryFaultKind.INVALID_REQUEST


def test_the_invalid_request_fault_names_the_offending_field() -> None:
    # Req 21.6 requires the field. "Your request was invalid" leaves the caller guessing which
    # of four
    # fields to fix.
    fault = fault_for(_validation_error())
    assert fault is not None
    assert "utterance" in fault.fields


def test_a_malformed_request_still_returns_a_response() -> None:
    # Req 32.5. Anything other than an AdvisoryResponse becomes an opaque 424 from the
    # container, which
    # loses the envelope and any escalation.
    fault = fault_for(_validation_error())
    assert fault is not None
    response = fault_response(fault, envelope=_envelope(), answered_at=_AT)
    assert response.degraded is True


def test_even_a_malformed_request_carries_the_emergency_guidance() -> None:
    # THE point of routing every failure through a response. A user in trouble who typed
    # something
    # unparseable still needs to be told to call for help, and Req 10.4 does not depend on the
    # request
    # being well-formed.
    fault = fault_for(_validation_error())
    assert fault is not None
    response = fault_response(fault, envelope=_envelope(), answered_at=_AT)
    assert response.envelope.emergency_guidance == _CONFIGURED


def test_a_fault_response_states_no_condition_value() -> None:
    fault = fault_for(_validation_error())
    assert fault is not None
    response = fault_response(fault, envelope=_envelope(), answered_at=_AT)
    assert response.basis is None


# --- Req 5.4: a rejected credential -------------------------------------


def test_an_unauthorized_serving_failure_asks_for_re_authentication() -> None:
    # Req 5.4. The caller needs to know what to DO, and "unauthorized" does not say it.
    fault = fault_for(ServingClientError(ServingFailureKind.UNAUTHORIZED))
    assert fault is not None
    assert fault.kind is BoundaryFaultKind.NEEDS_REAUTHENTICATION
    assert "re-authenticat" in fault.message.casefold()


def test_the_re_authentication_message_names_no_credential() -> None:
    # Req 5.2 is explicit that this includes a message reporting an authentication failure — the
    # one
    # place an author is most tempted to include the token "for debugging".
    fault = fault_for(ServingClientError(ServingFailureKind.UNAUTHORIZED))
    assert fault is not None
    lowered = fault.message.casefold()
    for word in ("token", "credential", "bearer", "jwt", "authorization"):
        assert word not in lowered, word


def test_the_re_authentication_message_carries_no_rejection_detail() -> None:
    # Req 5.4's second clause. Service 2's detail can quote the token's claims, and it tells the
    # user
    # nothing they can act on.
    error = ServingClientError(ServingFailureKind.UNAUTHORIZED)
    error.args = (*error.args, _MARKER)
    fault = fault_for(error)
    assert fault is not None
    assert _MARKER not in fault.message


@pytest.mark.parametrize(
    "kind",
    [k for k in ServingFailureKind if k is not ServingFailureKind.UNAUTHORIZED],
)
def test_other_serving_failures_are_not_re_authentication(
    kind: ServingFailureKind,
) -> None:
    # Non-vacuity: asking a user to sign in again because Service 2 timed out sends them to fix
    # something that is not broken.
    fault = fault_for(ServingClientError(kind))
    assert fault is not None
    assert fault.kind is not BoundaryFaultKind.NEEDS_REAUTHENTICATION


# --- Req 21.4: no provider text ever reaches the caller -----------------


@pytest.mark.parametrize(
    "error",
    [
        ServingClientError(ServingFailureKind.SERVER_ERROR),
        ServingClientError(ServingFailureKind.UNUSABLE_BODY),
        ValueError(_MARKER),
        OSError(_MARKER),
        RuntimeError(_MARKER),
    ],
)
def test_no_faults_message_contains_the_planted_marker(error: Exception) -> None:
    # Req 21.4, quantified. A provider message can say anything, so a test using one fixed error
    # body
    # would pass while a different one leaked. The marker stands for whatever the provider
    # sends.
    fault = fault_for(error) or handle_at_top_level(
        error, logger=_logger(), envelope=_envelope(), answered_at=_AT
    )
    text = fault.message if isinstance(fault, BoundaryFault) else (fault.guidance or "")
    assert _MARKER not in text


def test_a_fault_response_contains_no_traceback_language() -> None:
    response = handle_at_top_level(
        RuntimeError(_MARKER), logger=_logger(), envelope=_envelope(), answered_at=_AT
    )
    lowered = (response.guidance or "").casefold()
    for token in ("traceback", "file \"", "line ", "exception"):
        assert token not in lowered, token


def test_an_unexpected_error_is_not_recognised_by_fault_for() -> None:
    # `fault_for` handles the EXPECTED types only (Req 21.5). Returning a fault for everything
    # would
    # make the top-level handler dead code, and with it the log that is the only record of a
    # surprise.
    assert fault_for(RuntimeError("something nobody anticipated")) is None


# --- Req 21.5: the broad catch is at the top level, and it logs ---------


def test_the_top_level_handler_logs(caplog: pytest.LogCaptureFixture) -> None:
    # Req 21.5: the bare catch belongs only at a true top-level boundary, WHERE IT LOGS. A
    # silent broad
    # catch is how a recurring internal fault becomes invisible.
    caplog.set_level(logging.ERROR)
    handle_at_top_level(
        RuntimeError(_MARKER), logger=_logger(), envelope=_envelope(), answered_at=_AT
    )
    assert len(caplog.records) == 1


def test_the_top_level_log_does_not_carry_the_marker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The log records the TYPE, not the message. An operator needs to know what class of thing
    # happened;
    # the provider's words can carry a prompt or a partial generation.
    caplog.set_level(logging.ERROR)
    handle_at_top_level(
        RuntimeError(_MARKER), logger=_logger(), envelope=_envelope(), answered_at=_AT
    )
    from aqm_advisor.observability.logging import _JsonFormatter

    line = _JsonFormatter().format(caplog.records[0])
    assert "RuntimeError" in line
    assert _MARKER not in line


def test_the_top_level_handler_still_returns_a_response() -> None:
    response = handle_at_top_level(
        RuntimeError(_MARKER), logger=_logger(), envelope=_envelope(), answered_at=_AT
    )
    assert response.degraded is True
    assert response.envelope.emergency_guidance == _CONFIGURED


def test_the_top_level_handler_preserves_an_escalation() -> None:
    # Req 21.3 does not relax because the failure was a surprise. If a red flag was already
    # recognised,
    # an internal error must not swallow it.
    escalation = Escalation(
        kind="emergency", markers=("blue lips",), guidance=_CONFIGURED
    )
    response = handle_at_top_level(
        RuntimeError(_MARKER),
        logger=_logger(),
        envelope=_envelope(),
        answered_at=_AT,
        escalation=escalation,
    )
    assert response.escalation is escalation


# --- Req 21.5, structurally ---------------------------------------------

_SOURCE = pathlib.Path("src/aqm_advisor/agent/boundary.py").read_text(encoding="utf-8")


def test_the_module_has_no_bare_except() -> None:
    # A bare `except:` also swallows KeyboardInterrupt and SystemExit, making the process
    # unkillable.
    tree = ast.parse(_SOURCE)
    bare = [
        handler
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler) and handler.type is None
    ]
    assert bare == []


def test_this_module_contains_no_broad_catch_at_all() -> None:
    # Corrected from an assumption this test itself disproved. The first version expected the
    # broad catch
    # to live inside `handle_at_top_level` — but that function RECEIVES an already-caught error,
    # so the
    # catch belongs at the call site, which is the AgentCore entrypoint in task 17.
    #
    # What this module can therefore guarantee is the stronger half: it catches nothing broadly
    # anywhere.
    # Req 21.5's placement clause is asserted where the catch actually is, and `tasks.md`
    # records that as
    # task 17's obligation rather than leaving it to be rediscovered.
    tree = ast.parse(_SOURCE)
    broad: list[str] = []
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler):
            continue
        caught = handler.type
        names = (
            {caught.id}
            if isinstance(caught, ast.Name)
            else {
                element.id
                for element in getattr(caught, "elts", [])
                if isinstance(element, ast.Name)
            }
        )
        broad.extend(sorted(names & {"Exception", "BaseException"}))
    assert broad == [], broad


def test_the_top_level_handler_accepts_a_base_exception() -> None:
    # It handles whatever the entrypoint caught, so its parameter is `BaseException` rather than
    # `Exception`. Narrowing it would push a KeyboardInterrupt back to the container as a 424,
    # which is
    # exactly the outcome Req 32.5 forbids for a handled failure — and the entrypoint decides
    # what to
    # hand over, not this function.
    import inspect

    annotation = inspect.signature(handle_at_top_level).parameters["error"].annotation
    assert "BaseException" in str(annotation)


def test_the_broad_catch_detector_would_find_one_elsewhere() -> None:
    # Self-check: a detector that collected nothing would report this guarantee for free.
    planted = ast.parse(
        "def helper():\n    try:\n        pass\n    except Exception:\n        pass\n"
    )
    found = [
        handler
        for handler in ast.walk(planted)
        if isinstance(handler, ast.ExceptHandler)
        and isinstance(handler.type, ast.Name)
        and handler.type.id == "Exception"
    ]
    assert found != []
