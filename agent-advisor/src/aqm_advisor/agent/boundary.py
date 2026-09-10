"""Boundary error handling (Requirements 5.4, 21.4, 21.5, 21.6).

**Req 32.5 sets the shape: every handled failure becomes an `AdvisoryResponse`, not a status
code.** An unhandled error becomes an opaque `424 RuntimeClientError` from the container, which
replaces a documented degraded answer with a transport fault and loses the Guardrail_Envelope
and any Escalation with it.

So even a MALFORMED REQUEST gets the emergency guidance. A user in trouble who typed something
the service could not parse still needs to be told to call for help, and Req 10.4 does not
depend on the request being well-formed.

**No provider text ever reaches the caller** (Req 21.4). Every message here is a fixed sentence
chosen by kind; none is built from an exception's own words. A provider message can quote the
prompt, a partial generation, or the claims inside a token, and none of that helps the person
reading the answer.

**The credential's rejection is the most tempting place to leak** (Reqs 5.2, 5.4). Req 5.2 names
it explicitly — "including a message reporting an authentication failure" — because that is
where an author reaches for the token "just for debugging". The message here says the session
needs re-authenticating and nothing else.

**The broad catch lives in exactly one function, and it logs** (Req 21.5). `fault_for` handles
the EXPECTED types and returns `None` for anything else, so the top-level handler is reached
rather than being dead code — and its log is the only record that something unanticipated
happened. An AST test asserts no bare `except:` exists and that `except Exception` appears only
inside `handle_at_top_level`, because a convention about where broad catches live is not
enforceable by review alone.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum

from pydantic import ValidationError

from aqm_advisor.domain.degradation import degraded_response
from aqm_advisor.domain.models import AdvisoryResponse, Escalation, GuardrailEnvelope
from aqm_advisor.observability.logging import EventLogger
from aqm_advisor.ports.protocols import ServingClientError, ServingFailureKind


class BoundaryFaultKind(Enum):
    """What kind of failure the caller is being told about.

    Coarse on purpose: what a caller can DO about a failure has few distinct answers, and a kind
    per exception type would be a taxonomy nobody acts on differently.
    """

    INVALID_REQUEST = "invalid_request"
    NEEDS_REAUTHENTICATION = "needs_reauthentication"
    DATA_UNAVAILABLE = "data_unavailable"
    INTERNAL = "internal"


_MESSAGES: dict[BoundaryFaultKind, str] = {
    BoundaryFaultKind.INVALID_REQUEST: (
        "The request could not be read. Please correct the field named below and send it again."
    ),
    BoundaryFaultKind.NEEDS_REAUTHENTICATION: (
        "This session needs to be re-authenticated before I can look anything up."
    ),
    BoundaryFaultKind.DATA_UNAVAILABLE: (
        "The air-quality service could not be reached, so I have no current readings."
    ),
    BoundaryFaultKind.INTERNAL: (
        "Something went wrong on my side, so I could not complete this answer."
    ),
}
"""Fixed sentences, chosen by kind.

None is built from an exception's own words, which is how Req 21.4 holds by construction rather
than by each call site remembering not to interpolate.
"""

_SERVING_KINDS: dict[ServingFailureKind, BoundaryFaultKind] = {
    ServingFailureKind.UNAUTHORIZED: BoundaryFaultKind.NEEDS_REAUTHENTICATION,
    ServingFailureKind.UNREACHABLE: BoundaryFaultKind.DATA_UNAVAILABLE,
    ServingFailureKind.TIMEOUT: BoundaryFaultKind.DATA_UNAVAILABLE,
    ServingFailureKind.BAD_REQUEST: BoundaryFaultKind.DATA_UNAVAILABLE,
    ServingFailureKind.UNUSABLE_BODY: BoundaryFaultKind.DATA_UNAVAILABLE,
    ServingFailureKind.SERVER_ERROR: BoundaryFaultKind.DATA_UNAVAILABLE,
}
"""Only UNAUTHORIZED asks the user to re-authenticate.

Sending someone to sign in again because Service 2 timed out points them at something that is
not broken.
"""


@dataclass(frozen=True, slots=True)
class BoundaryFault:
    """A failure the caller can be told about safely.

    `fields` is empty except for an invalid request. There is deliberately nowhere to put a
    provider's message: a field able to hold one is an invitation to pass it through.
    """

    kind: BoundaryFaultKind
    message: str
    fields: tuple[str, ...] = ()


def _invalid_request_fields(error: ValidationError) -> tuple[str, ...]:
    """The field names Pydantic rejected, in the order reported.

    Names only. A Pydantic error also carries the offending INPUT, and echoing that back would
    return the user's own utterance in an error message — Req 19.2 keeps that text out of logs,
    and an error body is no better a place for it.
    """
    names: list[str] = []
    for detail in error.errors():
        location = detail.get("loc") or ()
        name = ".".join(str(part) for part in location) or "request"
        if name not in names:
            names.append(name)
    return tuple(names)


def fault_for(error: Exception) -> BoundaryFault | None:
    """Translate an EXPECTED exception into a safe fault, or None if unexpected.

    Returning None for the unexpected is deliberate: it is what routes a surprise to
    `handle_at_top_level`, whose log is the only record that one occurred. A `fault_for` that
    answered everything would make that handler dead code and the log with it.
    """
    if isinstance(error, ValidationError):
        return BoundaryFault(
            kind=BoundaryFaultKind.INVALID_REQUEST,
            message=_MESSAGES[BoundaryFaultKind.INVALID_REQUEST],
            fields=_invalid_request_fields(error),
        )
    if isinstance(error, ServingClientError):
        kind = _SERVING_KINDS.get(error.kind, BoundaryFaultKind.DATA_UNAVAILABLE)
        return BoundaryFault(kind=kind, message=_MESSAGES[kind])
    if isinstance(error, (OSError, ValueError)):
        return BoundaryFault(
            kind=BoundaryFaultKind.DATA_UNAVAILABLE,
            message=_MESSAGES[BoundaryFaultKind.DATA_UNAVAILABLE],
        )
    return None


def fault_response(
    fault: BoundaryFault,
    *,
    envelope: GuardrailEnvelope,
    answered_at: dt.datetime,
    escalation: Escalation | None = None,
) -> AdvisoryResponse:
    """Turn a fault into a degraded response that keeps the envelope (Reqs 21.3, 32.5)."""
    missing = fault.fields or ("current conditions",)
    return degraded_response(
        envelope=envelope,
        answered_at=answered_at,
        missing=missing,
        escalation=escalation,
        available_guidance=fault.message,
    )


def handle_at_top_level(
    error: BaseException,
    *,
    logger: EventLogger,
    envelope: GuardrailEnvelope,
    answered_at: dt.datetime,
    escalation: Escalation | None = None,
) -> AdvisoryResponse:
    """The one broad boundary (Req 21.5). Logs, then returns a response rather than raising.

    Logs the exception TYPE, never its message. An operator needs to know what class of thing
    happened; the exception's words can carry a prompt, a partial generation, or a provider's
    error body.

    Returns a response for the same reason everything else here does: raising would let the
    container answer `424` and take the envelope and escalation with it (Req 32.5).
    """
    logger.error("advisory_turn_internal_error", error_type=type(error).__name__)
    return degraded_response(
        envelope=envelope,
        answered_at=answered_at,
        missing=("current conditions",),
        escalation=escalation,
        available_guidance=_MESSAGES[BoundaryFaultKind.INTERNAL],
    )


__all__ = [
    "BoundaryFault",
    "BoundaryFaultKind",
    "fault_for",
    "fault_response",
    "handle_at_top_level",
]
